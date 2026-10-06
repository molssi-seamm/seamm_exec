# -*- coding: utf-8 -*-

"""Evaluate a model chemistry at many structures, over MDI or as tasks.

A step that needs energies (and gradients, and stress) for many structures
submits them to an :class:`Evaluator` and iterates over the results. The
evaluator, not the step or the user, chooses how they are computed:

- the **batch path**: each structure is a :class:`~seamm_exec.tasks.Task` from
  the program's ``get_task``, run by a :class:`~seamm_exec.tasks.TaskSet` (on
  the job's target: the local pool or a queue, with restart), and read back by
  the program's ``analyze_task``;
- the **MDI path**: one warm MDI engine per group of structures with the same
  elements, charge, multiplicity and periodicity, from the program's
  ``get_mdi_engine_command``.

The rule (see :func:`choose_path`): the batch path when the job's tasks go to a
queue and the program has ``get_task``; else MDI when the program has an MDI
engine, unless it prefers the batch path (``options["prefers_batch"]``, set by
ORCA, whose engine runs a subprocess per structure); else the batch path in the
local pool. Both paths return the same numbers for the same model chemistry.

The program's contract (classmethods beside ``get_model_chemistry_options``)::

    get_task(configuration, model_chemistry, *, key, properties, options, resources)
        -> seamm_exec.Task
    analyze_task(result, model_chemistry, configuration, *, properties, options)
        -> {"energy": kJ/mol, "gradients": (n, 3) kJ/mol/Å, "stress": GPa, ...}
    can_run_task(configuration, model_chemistry, *, options) -> bool   (optional)

A structure for which ``can_run_task`` is False (e.g. a periodic system for
ORCA or MOPAC) goes to the program's MDI engine, if it has one, even when the
others run as tasks. If there is no engine, or it cannot start here (a queue
target with the code only on the cluster), that structure gets a failed result;
so does one whose ``get_task`` raises. Neither stops the rest.

``analyze_task`` raises :class:`AnalysisError` when a required property is
missing; it never returns partial numbers. ``options`` passes what a consumer
needs for a fragment: ``atom_indices``, ``ghost_atoms``, ``charge``,
``multiplicity``, an initial guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time

import numpy as np

logger = logging.getLogger("seamm-exec")

E_UNITS = "kJ/mol"
G_UNITS = "kJ/mol/Å"
S_UNITS = "GPa"

#: Properties a result must have when requested. Stress is required only for a
#: periodic structure (``check_properties(..., periodic=True)``, and the
#: Evaluator checks it for every task); a molecule has none.
REQUIRED = ("energy", "gradients")


class AnalysisError(RuntimeError):
    """A task's results lack a requested property."""


@dataclass
class EvaluatorResult:
    """One structure's results.

    Attributes
    ----------
    key : str
    ok : bool
    energy : float or None
        kJ/mol
    gradients : numpy.ndarray or None
        (n, 3), kJ/mol/Å
    stress : list or None
        GPa, as the program gives it
    reason : str or None
        Why it failed.
    restored : bool
        From an earlier run (batch path).
    path : str
        "mdi" or "batch"
    data : dict
        Everything the program returned.
    elapsed : float
        Seconds spent on it in this run (0 if restored).
    """

    key: str
    ok: bool
    energy: float | None = None
    gradients: object = None
    stress: object = None
    reason: str | None = None
    restored: bool = False
    path: str = "batch"
    data: dict = field(default_factory=dict)
    elapsed: float = 0.0


class _Atoms:
    def __init__(self, atomic_numbers, coordinates):
        self.atomic_numbers = [int(z) for z in atomic_numbers]
        self._coordinates = np.asarray(coordinates, dtype=float).reshape(-1, 3)

    @property
    def symbols(self):
        return [_SYMBOLS[z] for z in self.atomic_numbers]

    def get_coordinates(self, fractionals=False, as_array=False):
        if fractionals:
            raise NotImplementedError("Geometry holds Cartesian coordinates only")
        return self._coordinates.copy() if as_array else self._coordinates.tolist()

    def __len__(self):
        return len(self.atomic_numbers)


class _Cell:
    def __init__(self, vectors):
        self._vectors = np.asarray(vectors, dtype=float).reshape(3, 3)

    def vectors(self, as_array=False):
        return self._vectors.copy() if as_array else self._vectors.tolist()


class Geometry:
    """A light stand-in for a molsystem configuration: elements, Cartesian
    coordinates (Å), charge, multiplicity and an optional cell. For structures
    a step makes on the fly, such as finite-difference displacements."""

    def __init__(
        self,
        atomic_numbers,
        coordinates,
        charge=0,
        multiplicity=1,
        cell=None,
        name=None,
    ):
        self.atoms = _Atoms(atomic_numbers, coordinates)
        self.charge = int(charge)
        self.spin_multiplicity = int(multiplicity)
        self.cell = _Cell(cell) if cell is not None else None
        self.periodicity = 3 if cell is not None else 0
        self.name = name

    @property
    def n_atoms(self):
        return len(self.atoms)


def structure_data(configuration):
    """What a program needs from a configuration (or a :class:`Geometry`).

    Returns
    -------
    dict
        ``atomic_numbers``, ``symbols``, ``coordinates`` ((n, 3) Å), ``charge``,
        ``multiplicity``, ``periodicity`` and ``cell`` ((3, 3) Å or None).
    """
    atomic_numbers = [int(z) for z in configuration.atoms.atomic_numbers]
    coordinates = np.asarray(
        configuration.atoms.get_coordinates(fractionals=False, as_array=True),
        dtype=float,
    ).reshape(-1, 3)
    periodicity = int(getattr(configuration, "periodicity", 0) or 0)
    cell = None
    if periodicity != 0:
        cell = np.asarray(configuration.cell.vectors(as_array=True), dtype=float)
    return {
        "atomic_numbers": atomic_numbers,
        "symbols": [_SYMBOLS[z] for z in atomic_numbers],
        "coordinates": coordinates,
        "charge": int(configuration.charge),
        "multiplicity": int(configuration.spin_multiplicity),
        "periodicity": periodicity,
        "cell": cell,
    }


def mdi_method_and_basis(model_chemistry):
    """The (method, basis) an MDI engine is launched with.

    ``method`` is the program's own keyword (``options["mdi_method_arg"]``),
    falling back to the model chemistry's method; ``basis`` is
    ``options["mdi_basis_arg"]`` (the user's basis, for programs that take one)
    or the model chemistry's basis, or None for programs that take a method
    alone (MOPAC, xTB, an MLFF).
    """
    options = model_chemistry.get("options") or {}
    method = options.get("mdi_method_arg") or model_chemistry.get("method")
    basis = options.get("mdi_basis_arg") or model_chemistry.get("basis")
    return method, basis


def choose_path(model_chemistry, provider, target=None):
    """ "mdi" or "batch" for a model chemistry, a program and the job's target.

    Raises
    ------
    ValueError
        If the program offers neither path.
    """
    options = model_chemistry.get("options") or {}
    has_batch = hasattr(provider, "get_task") and hasattr(provider, "analyze_task")
    has_mdi = bool(options.get("mdi_capable", False)) and hasattr(
        provider, "get_mdi_engine_command"
    )
    on_queue = target is not None and getattr(target, "tasks", None) in (
        "queue",
        "taskserver",
    )
    if on_queue and has_batch:
        return "batch"
    if has_mdi and not (options.get("prefers_batch") and has_batch):
        return "mdi"
    if has_batch:
        return "batch"
    raise ValueError(
        f"The model chemistry '{model_chemistry.get('level')}' can be evaluated "
        "neither over MDI nor as tasks."
    )


class Evaluator:
    """Evaluate a model chemistry at many structures.

    Parameters
    ----------
    node : seamm.Node
        The step: its directory, flowchart (plug-ins, executor) and options.
    model_chemistry : dict, optional
        The ``_model_chemistry`` wrapper. Default: the node's variable.
    properties : [str]
        "energy", "gradients", "stress".
    path : str, optional
        "mdi" or "batch", for tests only; the evaluator chooses otherwise.
    directory : str or Path, optional
        The batch path's step directory (``<directory>/tasks/...``). Default:
        the node's.
    target : seamm_scheduler.TargetSection, optional
        The job's target. Default: found for the job.
    task_set_options : dict, optional
        Extra arguments for the ``TaskSet`` (``bundle_tasks``, ``archive``, ...).
    resources : seamm_exec.Resources, optional
        The resources of each calculation on the batch path (ranks, memory per
        rank), passed to the provider's ``get_task``. Default: the provider's.
    name : str
        A name for the MDI engine.
    """

    def __init__(
        self,
        node,
        model_chemistry=None,
        *,
        properties=("energy", "gradients"),
        path=None,
        directory=None,
        target=None,
        task_set_options=None,
        resources=None,
        name="SEAMM",
    ):
        self.node = node
        if model_chemistry is None:
            if not node.variable_exists("_model_chemistry"):
                raise ValueError(
                    "No model chemistry: add a 'Model Chemistry' step to the "
                    "flowchart before this step."
                )
            model_chemistry = node.get_variable("_model_chemistry")
        self.model_chemistry = model_chemistry
        self.options = model_chemistry.get("options") or {}
        self.properties = tuple(properties)
        self.provider = node.flowchart.plugin_manager.get(model_chemistry["step"])
        self.directory = directory
        self.task_set_options = dict(task_set_options or {})
        self.resources = resources
        self.name = name

        if target is None and path is None:
            from .targets import find_target

            try:
                job_directory = node.flowchart.root_directory
            except Exception:
                job_directory = None
            from .tasks import node_root

            root = node_root(node) if node is not None else None
            target = find_target(job_directory=job_directory, root=root)
        self.target = target
        self.path = path or choose_path(model_chemistry, self.provider, target)

        self._submitted = {}  # key -> (configuration, options), in order
        self._done = set()

    # ------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def close(self):
        pass

    def submit(self, configuration, key=None, *, options=None):
        """Add a structure; returns its key (unique, filesystem-safe)."""
        if key is None:
            key = f"s{len(self._submitted) + 1:06d}"
        key = str(key)
        if key in self._submitted:
            raise ValueError(f"Duplicate key '{key}'")
        self._submitted[key] = (configuration, dict(options or {}))
        return key

    def cancel(self):
        """Stop this evaluator's work, from any thread: the running
        :class:`~seamm_exec.TaskSet` is cancelled (its tasks in flight killed,
        the held ones dropped, all marked ``cancelled`` for a fresh start
        later), and the MDI path stops before its next structure. Results
        already produced stand."""
        self._cancelled = True
        task_set = getattr(self, "_task_set", None)
        if task_set is not None:
            task_set.cancel()

    @property
    def cancelled(self):
        return bool(getattr(self, "_cancelled", False))

    def results(self):
        """Compute what has been submitted; yield an :class:`EvaluatorResult`
        for each, as it is ready."""
        pending = {k: v for k, v in self._submitted.items() if k not in self._done}
        if not pending:
            return
        if self.path == "mdi":
            iterators = [self._mdi_results(pending)]
        else:
            # A structure the program cannot run as a task (e.g. periodic MOPAC)
            # goes to its MDI engine, if it has one, whatever the target.
            batch, mdi = {}, {}
            for key, (configuration, options) in pending.items():
                if self._can_run_task(configuration, options):
                    batch[key] = (configuration, options)
                else:
                    mdi[key] = (configuration, options)
            iterators = []
            if batch:
                iterators.append(self._batch_results(batch))
            if mdi:
                if self.mdi_capable:
                    iterators.append(self._mdi_results(mdi, fallback=True))
                else:
                    iterators.append(self._cannot_run(mdi))
        for iterator in iterators:
            for result in iterator:
                self._done.add(result.key)
                yield result

    @property
    def mdi_capable(self):
        """Whether the program has an MDI engine for this model chemistry."""
        return bool(self.options.get("mdi_capable", False)) and hasattr(
            self.provider, "get_mdi_engine_command"
        )

    def _can_run_task(self, configuration, options):
        """The program's ``can_run_task`` hook: True if it has none."""
        check = getattr(self.provider, "can_run_task", None)
        if check is None:
            return True
        try:
            return bool(check(configuration, self.model_chemistry, options=options))
        except Exception:
            # A broken hook must not reroute silently
            logger.exception(
                f"can_run_task of '{self.model_chemistry.get('step')}' failed; "
                "treating the structure as one it cannot run as a task"
            )
            return False

    def _cannot_run(self, pending):
        for key in pending:
            yield EvaluatorResult(
                key=key,
                ok=False,
                reason=(
                    f"'{self.model_chemistry.get('level')}' cannot evaluate this "
                    "structure as a task and has no MDI engine"
                ),
                path="batch",
            )

    # ------------------------------------------------------------------
    # MDI
    # ------------------------------------------------------------------
    @staticmethod
    def topology_key(configuration, options=None):
        """What must stay fixed for one MDI engine session."""
        options = options or {}
        return (
            tuple(int(z) for z in configuration.atoms.atomic_numbers),
            int(options.get("charge", configuration.charge)),
            int(options.get("multiplicity", configuration.spin_multiplicity)),
            int(getattr(configuration, "periodicity", 0) or 0),
        )

    def _mdi_results(self, pending, fallback=False):
        """The MDI path. With ``fallback`` (structures the program cannot run as
        tasks while the rest do), a structure the local engine cannot take, or an
        engine that cannot start here (the code may live only on the job's
        cluster), gives failed results instead of stopping the others."""
        pending = dict(pending)
        for key, (configuration, options) in list(pending.items()):
            for name in ("atom_indices", "ghost_atoms", "guess"):
                if options.get(name) is not None:
                    if not fallback:
                        raise ValueError(
                            f"The MDI path cannot take '{name}'; this model "
                            "chemistry must run as tasks."
                        )
                    del pending[key]
                    yield EvaluatorResult(
                        key=key,
                        ok=False,
                        reason=f"cannot run here: the MDI engine cannot take '{name}'",
                        path="mdi",
                    )
                    break
        groups = {}
        for key, (configuration, options) in pending.items():
            groups.setdefault(self.topology_key(configuration, options), []).append(
                (key, configuration)
            )
        want_gradients = "gradients" in self.properties
        want_stress = "stress" in self.properties
        for topology, members in groups.items():
            if self.cancelled:
                for key, _ in members:
                    yield EvaluatorResult(
                        key=key, ok=False, reason="cancelled", path="mdi"
                    )
                continue
            elements, charge, multiplicity, periodicity = topology
            periodic = periodicity != 0
            try:
                engine = self._open_engine(members[0][1], charge, multiplicity)
            except Exception as e:
                if not fallback:
                    raise
                for key, _ in members:
                    yield EvaluatorResult(
                        key=key,
                        ok=False,
                        reason=f"cannot run here: no MDI engine on this machine ({e})",
                        path="mdi",
                    )
                continue
            with engine:
                if periodic and not engine.supports(">CELL"):
                    raise ValueError(
                        f"The model chemistry '{self.model_chemistry['level']}' MDI "
                        "engine does not accept a periodic cell (>CELL), so it "
                        "cannot evaluate periodic structures."
                    )
                do_stress = periodic and want_stress and engine.supports("<STRESS")
                for key, configuration in members:
                    t0 = time.perf_counter()
                    if periodic:
                        engine.set_cell(
                            configuration.cell.vectors(as_array=True), units="Å"
                        )
                    xyz = configuration.atoms.get_coordinates(
                        fractionals=False, as_array=True
                    )
                    engine.set_coordinates(np.asarray(xyz, dtype=float), units="Å")
                    data = {"energy": float(engine.energy(units=E_UNITS))}
                    gradients = stress = None
                    if want_gradients:
                        forces = np.asarray(engine.forces(units=G_UNITS), dtype=float)
                        gradients = -forces
                        data["gradients"] = gradients.tolist()
                    if do_stress:
                        stress = np.asarray(
                            engine.stress(units=S_UNITS), dtype=float
                        ).tolist()
                        data["stress"] = stress
                    yield EvaluatorResult(
                        key=key,
                        ok=True,
                        energy=data["energy"],
                        gradients=gradients,
                        stress=stress,
                        path="mdi",
                        data=data,
                        elapsed=time.perf_counter() - t0,
                    )

    def _open_engine(self, configuration, charge, multiplicity):
        """A started ``seamm_mdi.MDIEngine`` for this topology."""
        from seamm_mdi import MDIEngine  # only here: batch needs no pymdi

        node = self.node
        provider = self.provider
        executor = node.flowchart.executor
        seamm_options = node.global_options
        method, basis = mdi_method_and_basis(self.model_chemistry)
        n_atoms = configuration.n_atoms

        def build_argv(hostname, port):
            kwargs = {
                "method": method,
                "port": port,
                "hostname": hostname,
                "charge": charge,
                "multiplicity": multiplicity,
                "n_atoms": n_atoms,
            }
            if basis is not None:
                kwargs["basis"] = basis
            return provider.get_mdi_engine_command(executor, seamm_options, **kwargs)

        engine = MDIEngine(
            build_argv,
            elements=list(configuration.atoms.atomic_numbers),
            name=self.name,
            logger=getattr(node, "logger", logger),
        )
        engine.start()
        return engine

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------
    def _batch_results(self, pending):
        from .tasks import TaskSet

        task_set = TaskSet(
            self.node,
            target=self.target,
            directory=self.directory,
            **self.task_set_options,
        )
        self._task_set = task_set
        if self.cancelled:
            task_set.cancel()
        refused = []
        for key, (configuration, options) in pending.items():
            try:
                extra = {}
                if self.resources is not None:
                    extra["resources"] = self.resources
                task = self.provider.get_task(
                    configuration,
                    self.model_chemistry,
                    key=key,
                    properties=self.properties,
                    options=options,
                    **extra,
                )
            except Exception as e:
                # One structure the program refuses must not stop the others.
                refused.append(
                    EvaluatorResult(
                        key=key, ok=False, reason=f"no task: {e}", path="batch"
                    )
                )
                continue
            task_set.add(task)
        yield from refused
        if not task_set.tasks:
            return
        for result in task_set.run():
            configuration, options = pending[result.key]
            if not result.ok:
                yield EvaluatorResult(
                    key=result.key,
                    ok=False,
                    reason=result.reason or result.state,
                    restored=result.restored,
                    path="batch",
                )
                continue
            try:
                data = self.provider.analyze_task(
                    result,
                    self.model_chemistry,
                    configuration,
                    properties=self.properties,
                    options=options,
                )
            except AnalysisError as e:
                yield EvaluatorResult(
                    key=result.key,
                    ok=False,
                    reason=str(e),
                    restored=result.restored,
                    path="batch",
                )
                continue
            periodic = int(getattr(configuration, "periodicity", 0) or 0) != 0
            if periodic and "stress" in self.properties and data.get("stress") is None:
                yield EvaluatorResult(
                    key=result.key,
                    ok=False,
                    reason="the calculation of a periodic structure returned no stress",
                    restored=result.restored,
                    path="batch",
                )
                continue
            gradients = data.get("gradients")
            if gradients is not None:
                gradients = np.asarray(gradients, dtype=float).reshape(-1, 3)
            elapsed = 0.0
            if not result.restored and result.history:
                last = result.history[-1]
                if last.get("started") and last.get("finished"):
                    elapsed = last["finished"] - last["started"]
                elif last.get("submitted") and last.get("finished"):
                    elapsed = last["finished"] - last["submitted"]
            yield EvaluatorResult(
                key=result.key,
                ok=True,
                energy=data.get("energy"),
                gradients=gradients,
                stress=data.get("stress"),
                restored=result.restored,
                path="batch",
                data=data,
                elapsed=elapsed,
            )


def check_properties(data, properties, what, periodic=False):
    """Raise :class:`AnalysisError` unless ``data`` has every required property
    in ``properties`` (and the stress, if requested, for a periodic structure).
    For programs' ``analyze_task``."""
    required = REQUIRED + (("stress",) if periodic else ())
    missing = [p for p in properties if p in required and data.get(p) is None]
    if missing:
        raise AnalysisError(f"{what} has no {', '.join(missing)}")


_SYMBOLS = [
    "X",
    "H", "He",
    "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba",
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm",
    "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn",
    "Fr", "Ra",
    "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md",
    "No", "Lr",
    "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc", "Lv",
    "Ts", "Og",
]  # fmt: skip
