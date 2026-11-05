# -*- coding: utf-8 -*-

"""Energies, optimizations and Hessians from a Model Chemistry, shared by steps.

The Model Chemistry step upstream publishes ``_model_chemistry``; the program
behind it is driven through :class:`seamm_exec.Evaluator`, either as a warm MDI
engine (``seamm_mdi.MDIEngine``) or as separate calculations (tasks) where the
job runs them. :class:`HessianDriver` chooses the route (design of 2026-10-03,
from the Normal Mode Sampling and Thermochemistry steps, which now share it):

* on a queue target the finite difference runs as tasks on the cluster, with
  no local engine started even to ask;
* otherwise the analytic Hessian over MDI when the program offers one (the
  engine advertises the custom ``<HESSIAN`` command only when the method has
  a genuine analytic Hessian), else central finite differences of the forces
  over the warm engine, or as tasks when the evaluator chose that path;
* with no MDI engine, the finite difference as tasks.

An :class:`EngineSession` keeps one warm engine for a structure, so a step can
optimize it, take its Hessian and then the Hessians at displaced geometries
from the same engine and settings, as anharmonic (VPT2) analyses need.

Moved here from thermochemistry_step.hessian (2026-11-05), where it was noted to
belong in a shared library once a third consumer appeared: the Vibrational
Analysis step.
"""

import contextlib
import logging
import os
from pathlib import Path
import subprocess

import numpy as np

from seamm_util import Q_
import seamm_util.printing as printing
from seamm_util.printing import FormattedText as __

logger = logging.getLogger(__name__)
printer = printing.getPrinter("seamm-exec")


def _accepts_keyword(function, name):
    """Whether `function` takes the keyword argument `name` (or **kwargs)."""
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False
    if name in parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def parse_cpu_ids(text):
    """The CPU ids in a Slurm ``CPU_IDs=`` list such as ``16-31`` or
    ``0-3,8,10-11`` (``scontrol show job -d``), as a set of ints."""
    cpus = set()
    for part in text.replace("CPU_IDs=", "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(part))
    return cpus


def physical_cores_allowed(cpus=None, sysfs="/sys/devices/system/cpu"):
    """The number of distinct physical cores among the CPUs (hardware
    threads) this process may run on, from the kernel's topology; None where
    that is unknown (macOS, no sysfs)."""
    if cpus is None:
        try:
            cpus = os.sched_getaffinity(0)
        except AttributeError:
            return None
    cores = set()
    for cpu in cpus:
        path = os.path.join(sysfs, f"cpu{cpu}", "topology", "core_id")
        try:
            with open(path) as fd:
                core_id = int(fd.read().strip())
        except (OSError, ValueError):
            return None
        # core ids repeat across sockets
        spath = os.path.join(sysfs, f"cpu{cpu}", "topology", "physical_package_id")
        try:
            with open(spath) as fd:
                socket_id = int(fd.read().strip())
        except (OSError, ValueError):
            socket_id = 0
        cores.add((socket_id, core_id))
    return len(cores) or None


def slurm_allocated_cpus(environ=None, run=None):
    """The CPU ids Slurm allocated to this job on this node, from
    ``scontrol show job -d``, or None outside Slurm or when unknown."""
    import socket
    import subprocess

    environ = os.environ if environ is None else environ
    job_id = environ.get("SLURM_JOB_ID")
    if not job_id:
        return None
    try:
        if run is None:
            text = subprocess.run(
                ["scontrol", "show", "job", "-d", job_id],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        else:
            text = run(job_id)
    except Exception:
        return None
    host = socket.gethostname().split(".")[0]
    chosen = None
    for line in text.splitlines():
        if "CPU_IDs=" not in line:
            continue
        ids = line.split("CPU_IDs=")[1].split()[0]
        nodes = line.split("Nodes=")[1].split()[0] if "Nodes=" in line else ""
        if chosen is None or host in nodes:
            chosen = ids
        if host in nodes:
            break
    return parse_cpu_ids(chosen) if chosen else None


_MODULE_INIT = (
    "/etc/profile.d/lmod.sh",
    "$LMOD_PKG/init/bash",
    "/etc/profile.d/modules.sh",
    "/usr/share/lmod/lmod/init/bash",
    "/usr/share/Modules/init/bash",
)
_IGNORED_VARIABLES = {"_", "SHLVL", "PWD", "OLDPWD"}
_SEPARATOR = b"\0__SEAMM_MODULE_LOAD__\0"


def modules_to_load(config, ce=None):
    """The modules for this run, by the rule seamm_exec's Local.exec uses: the
    ``gpu_modules`` when the computational environment has GPUs and they are
    given, otherwise ``modules``; '' unless ``installation = modules``."""
    if (config.get("installation") or "").strip() != "modules":
        return ""
    modules = ""
    if ce is not None and "NGPUS" in ce:
        modules = config.get("gpu_modules") or ""
    if not modules:
        modules = config.get("modules") or ""
    return " ".join(str(modules).split())


def module_environment(config, ce=None, environ=None):
    """The variables that ``module load`` changes, for a program whose
    executor config says ``installation = modules``; {} otherwise.

    A normal run of the program gets them because seamm_exec puts
    ``module load`` in front of the command, but the MDI engine is started
    directly, so without them a module-installed code cannot even load its
    own libraries (ORCA on TinkerCliffs: "liborca_tools_6_1_1_avx2.so.6:
    cannot open shared object file"). The modules are loaded in a child
    shell (``config["shell"]``, default bash), sourcing the module system's
    init script if the ``module`` function is not defined (it is not under
    ``--export=NONE``), and its environment before and after the load is
    compared. Exported shell functions and the variables bash itself sets
    are left out.

    Raises RuntimeError naming the modules if they cannot be loaded.
    """
    modules = modules_to_load(config, ce)
    if not modules:
        return {}
    environ = dict(os.environ if environ is None else environ)
    init = "; ".join(
        f'[ -r "{path}" ] && . "{path}" && type module >/dev/null 2>&1 && return 0'
        for path in _MODULE_INIT
    )
    script = (
        "_init() { " + init + "; return 1; }\n"
        "type module >/dev/null 2>&1 || _init || exit 4\n"
        "env -0\n"
        "printf '__SEAMM_MODULE_LOAD__\\0'\n"
        f"module load {modules} >&2 || exit 3\n"
        "env -0\n"
    )
    shell = config.get("shell") or "/bin/bash"
    tried = ", ".join(_MODULE_INIT)
    try:
        result = subprocess.run(
            [shell, "-c", script], capture_output=True, env=environ, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not load the modules '{modules}' with {shell}: {e}")
    if result.returncode != 0:
        why = (
            "no 'module' command, and none of these init scripts defined it: " + tried
            if result.returncode == 4
            else result.stderr.decode(errors="replace").strip()[-500:]
        )
        raise RuntimeError(
            f"'module load {modules}' failed (return code {result.returncode}), so the "
            f"MDI engine cannot be started with the program's environment: {why}"
        )
    before, _, after = (b"\0" + result.stdout).partition(_SEPARATOR)

    def parse(blob):
        variables = {}
        for item in blob.split(b"\0"):
            name, sep, value = item.decode(errors="replace").partition("=")
            if sep and name:
                variables[name] = value
        return variables

    before, after = parse(before), parse(after)
    return {
        name: value
        for name, value in after.items()
        if before.get(name) != value
        and name not in _IGNORED_VARIABLES
        and not name.startswith("BASH_FUNC_")
    }


class _MDICalculator:
    """A minimal ASE calculator over a warm MDI engine, for the optimization."""

    implemented_properties = ["energy", "forces"]

    def __init__(self, engine):
        self.engine = engine
        self.results = {}
        self.n_calls = 0
        self._positions = None

    def _update(self, atoms):
        if self._positions is None or not np.array_equal(
            self._positions, atoms.positions
        ):
            self.engine.set_coordinates(atoms.positions, units="Å")
            self.results = {
                "energy": self.engine.energy(units="eV"),
                "forces": np.asarray(self.engine.forces(units="eV/Å"), dtype=float),
            }
            self._positions = np.array(atoms.positions)
            self.n_calls += 1

    def get_potential_energy(self, atoms=None, force_consistent=False):
        self._update(atoms)
        return self.results["energy"]

    def get_forces(self, atoms=None):
        self._update(atoms)
        return self.results["forces"]

    def calculation_required(self, atoms, properties):
        return self._positions is None or not np.array_equal(
            self._positions, atoms.positions
        )


class EngineSession:
    """One warm MDI engine for one structure (see :meth:`HessianDriver.session`).

    Attributes
    ----------
    engine : seamm_mdi.MDIEngine
    configuration : molsystem._Configuration
        Updated by :meth:`optimize`.
    coordinates : numpy.ndarray (N, 3)
        The current reference coordinates, Å.
    """

    def __init__(self, driver, engine, configuration, evaluator, elements, coords_ang):
        self.driver = driver
        self.engine = engine
        self.configuration = configuration
        self.evaluator = evaluator
        self.elements = list(elements)
        self.coordinates = np.asarray(coords_ang, dtype=float).reshape(-1, 3)

    @property
    def n_atoms(self):
        return len(self.elements)

    @property
    def analytic(self):
        """Whether the engine gives an analytic Hessian. It advertises the
        custom ``<HESSIAN`` command only when the method has a genuine one."""
        return bool(self.engine.supports("<HESSIAN"))

    def optimize(self, fmax=0.005, max_steps=200):
        """ASE BFGS over the warm engine; updates the configuration and the
        reference coordinates. fmax in eV/Å. Returns whether it converged."""
        from ase import Atoms
        from ase.optimize import BFGS

        if self.n_atoms < 2:
            return True
        atoms = Atoms(numbers=self.elements, positions=self.coordinates)
        atoms.calc = _MDICalculator(self.engine)
        converged = BFGS(atoms, logfile=None).run(fmax=fmax, steps=max_steps)
        self.coordinates = np.asarray(atoms.positions, dtype=float)
        self.configuration.atoms.set_coordinates(self.coordinates, fractionals=False)
        self.driver.say(
            f"The structure was optimized in {atoms.calc.n_calls} energy evaluations"
            + ("." if converged else f" without converging in {max_steps} steps.")
        )
        return bool(converged)

    def energy(self, coords_ang=None, units="kJ/mol"):
        """The energy at the reference (or the given) coordinates."""
        x = self.coordinates if coords_ang is None else coords_ang
        self.engine.set_coordinates(np.asarray(x, dtype=float), units="Å")
        return self.engine.energy(units=units)

    def gradient(self, coords_ang=None):
        """The Cartesian gradient, E_h/bohr, (3N,), at the reference (or the
        given) coordinates."""
        x = self.coordinates if coords_ang is None else coords_ang
        self.engine.set_coordinates(np.asarray(x, dtype=float), units="Å")
        forces = self.engine.forces(units="hartree/bohr")
        return -np.asarray(forces, dtype=float).reshape(-1)

    def hessian(self, coords_ang=None, delta_bohr=0.01, finite_differences=False):
        """The Cartesian Hessian (E_h/bohr², (3N, 3N)) at the reference (or the
        given) coordinates: analytic when the engine has it, else central
        differences of its forces. The engine is left at the reference.

        Returns
        -------
        (numpy.ndarray, str)
            The Hessian, and "analytic" or "finite differences over MDI".
        """
        x = self.coordinates if coords_ang is None else coords_ang
        x = np.asarray(x, dtype=float).reshape(-1, 3)
        n = self.n_atoms
        try:
            if self.analytic and not finite_differences:
                self.engine.set_coordinates(x, units="Å")
                H = np.asarray(self.engine.hessian(), dtype=float).reshape(3 * n, 3 * n)
                return 0.5 * (H + H.T), "analytic"
            return (
                self.driver.fd_hessian_engine(self.engine, x, n, delta_bohr),
                "finite differences over MDI",
            )
        finally:
            self.engine.set_coordinates(self.coordinates, units="Å")

    def fingerprints(self):
        """The engine's settings fingerprints, {"energy": record, "hessian":
        record} (seamm_mdi 2026.10.28, ORCA's engine first), or None."""
        get = getattr(self.engine, "fingerprints", None)
        if get is None:
            return None
        try:
            return get()
        except Exception as e:  # pragma: no cover
            logger.warning(f"The engine gave no fingerprints: {e}")
            return None


class HessianDriver:
    """Energies, optimizations and Hessians from the flowchart's Model Chemistry.

    Parameters
    ----------
    node : seamm.Node
        The step: its flowchart (plug-ins, executor), options, indent and logger.
    model_chemistry : dict, optional
        The ``_model_chemistry`` wrapper. Default: the node's variable.
    route : str
        "automatic"; "warm engine ..." to insist on the MDI engine; "separate
        tasks ..." to run the finite differences as tasks.
    who : str, optional
        The step's name for messages. Default: the node's title.
    hint : str, optional
        Added to the message when there is no Model Chemistry.
    print_to : seamm_util.printing printer, optional
        Where notes for the user go. Default: seamm-exec's.

    Attributes
    ----------
    fingerprints : dict or None
        The engine's settings fingerprints from the last engine session.
    step : seamm plug-in
        The program's step (``_model_chemistry["step"]``), once a route is
        planned.
    """

    def __init__(
        self,
        node,
        model_chemistry=None,
        *,
        route="automatic",
        who=None,
        hint="",
        print_to=None,
    ):
        self.node = node
        self._model_chemistry = model_chemistry
        self.route = route or "automatic"
        self.who = who or getattr(node, "title", None) or type(node).__name__
        self.hint = hint
        self.printer = print_to or printer
        self.fingerprints = None
        self.step = None
        self.last_plan = None

    # ------------------------------------------------------------------
    def say(self, text):
        """A note for the user, indented under the step."""
        indent = getattr(self.node, "indent", "") + 4 * " "
        self.printer.important(__(text, indent=indent))

    def model_chemistry(self):
        """The ``_model_chemistry`` wrapper, or None."""
        if self._model_chemistry is not None:
            return self._model_chemistry
        if not self.node.variable_exists("_model_chemistry"):
            return None
        return self.node.get_variable("_model_chemistry")

    def plan(self, configuration):
        """How to drive the Model Chemistry for this configuration.

        Returns
        -------
        (evaluator, build_argv or None, elements, coords_ang)
            ``build_argv`` is None when the finite difference must run as
            tasks (no MDI engine, a queue target, or the program not here).
        """
        mc = self.model_chemistry()
        if mc is None:
            raise ValueError(
                f"{self.who} needs a Model Chemistry: add a 'Model Chemistry' step "
                "before it to choose the method"
                + (f", {self.hint}" if self.hint else "")
                + "."
            )
        if configuration.periodicity != 0:
            raise ValueError(f"{self.who} of a periodic system is not supported.")

        options = mc["options"]
        from seamm_exec import Evaluator

        try:
            evaluator = Evaluator(self.node, mc, properties=("energy", "gradients"))
        except ValueError:
            raise ValueError(
                f"The model chemistry '{mc['level']}' can be evaluated neither over "
                "MDI nor as separate calculations, so it cannot give the Hessian. "
                "Choose a model chemistry with an MDI engine or with tasks (e.g. "
                "MOPAC, xTB or ORCA)."
            )

        flowchart = self.node.flowchart
        step = flowchart.plugin_manager.get(mc["step"])
        self.step = step
        method = options.get("mdi_method_arg") or mc["method"]
        basis = options.get("mdi_basis_arg")

        ncores = self.engine_ncores()
        takes_ncores = hasattr(step, "get_mdi_engine_command") and _accepts_keyword(
            step.get_mdi_engine_command, "ncores"
        )

        def build_argv(hostname, port):
            kwargs = {}
            if takes_ncores:  # ORCA does; MOPAC's engine is single-threaded
                kwargs["ncores"] = ncores
            if basis is not None:
                kwargs["basis"] = basis
            return step.get_mdi_engine_command(
                flowchart.executor,
                self.node.global_options,
                method=method,
                port=port,
                hostname=hostname,
                charge=configuration.charge,
                multiplicity=configuration.spin_multiplicity,
                n_atoms=configuration.n_atoms,
                **kwargs,
            )

        elements = list(configuration.atoms.atomic_numbers)
        coords_ang = np.asarray(
            configuration.atoms.get_coordinates(fractionals=False, as_array=True),
            dtype=float,
        )

        has_tasks = hasattr(evaluator.provider, "get_task")
        target = evaluator.target
        on_queue = target is not None and getattr(target, "tasks", None) in (
            "queue",
            "taskserver",
        )
        route = self.route
        has_mdi = bool(options.get("mdi_capable", False)) and hasattr(
            step, "get_mdi_engine_command"
        )
        use_tasks = False
        if route.startswith("warm engine"):
            if not has_mdi:
                if not has_tasks:
                    raise ValueError(
                        f"{mc['level']} has no MDI engine, so it cannot be driven as a "
                        "warm engine; choose another Hessian route or model chemistry."
                    )
                self.say(
                    f"{mc['level']} has no MDI engine: the Hessian is computed by "
                    "finite differences as separate tasks, and the structure is "
                    "not optimized."
                )
                use_tasks = True
            elif has_tasks and not self.code_is_here(step):
                self.say(
                    f"{mc['level']} is not installed on this machine, so the "
                    "Hessian is computed by finite differences, as separate "
                    "calculations where the job runs its tasks, and the structure "
                    "is not optimized."
                )
                use_tasks = True
        elif route.startswith("separate tasks") and has_tasks:
            use_tasks = True
        elif on_queue and has_tasks:
            use_tasks = True
        elif not has_mdi:
            use_tasks = True
        elif options.get("analytic_hessian") is False and evaluator.path == "batch":
            use_tasks = True
        elif has_tasks and not self.code_is_here(step):
            self.say(
                f"{mc['level']} is not installed on this machine, so the "
                "Hessian is computed by finite differences, as separate "
                "calculations where the job runs its tasks."
            )
            use_tasks = True
        return evaluator, (None if use_tasks else build_argv), elements, coords_ang

    def code_is_here(self, step):
        """Whether the program is installed where this step runs."""
        check = getattr(step, "get_executor_config", None)
        if check is None:
            return True
        try:
            check(self.node.flowchart.executor, self.node.global_options)
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def engine_environment(self, ncores):
        """The environment the MDI engine's own runs need, set for the time
        the engine lives and restored afterwards: what the program's step
        gives (``mdi_engine_environment(executor, options, ncores)`` when it
        has one; for ORCA its ``mpi_env``: the OpenMPI runtime matching its
        libraries, with its ``mpirun`` first on the PATH, and the transport
        settings), and no MPI core binding, since the ranks of several jobs
        on a node without cgroups would otherwise pile onto the same cores.
        """
        step = self.step
        executor = self.node.flowchart.executor
        options = self.node.global_options
        saved = dict(os.environ)
        env = {}
        try:
            # A module installation: what `module load` gives a normal run
            if step is not None and hasattr(step, "get_executor_config"):
                try:
                    config = step.get_executor_config(executor, options)
                except Exception as e:
                    logger.warning(
                        f"Could not read the executor configuration of "
                        f"{type(step).__name__} ({e}); its MDI engine starts without "
                        "the program's modules, if it has any."
                    )
                    config = {}
                ce = None
                try:
                    from seamm_exec import computational_environment

                    ce = computational_environment()
                except Exception:
                    pass
                loaded = module_environment(config or {}, ce)
                if loaded:
                    env.update(loaded)
                    os.environ.update(loaded)
            hook = getattr(step, "mdi_engine_environment", None)
            if hook is not None:
                env.update(hook(executor, options, ncores) or {})
            elif step is not None and type(step).__module__.startswith("orca_step"):
                try:
                    from orca_step.orca_base import mpi_env
                except ImportError:  # pragma: no cover
                    mpi_env = None
                if mpi_env is not None:
                    config = step.get_executor_config(executor, options)
                    extra, _ = mpi_env(ncores, config)
                    env.update(extra)
                    library_path = config.get("library-path", "") or ""
                    if ncores > 1 and library_path:
                        bindir = Path(library_path).expanduser().parent / "bin"
                        if bindir.is_dir():
                            env["PATH"] = (
                                f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
                            )
            env["OMPI_MCA_hwloc_base_binding_policy"] = "none"
            os.environ.update(env)
            yield env
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def engine_ncores(self):
        """The cores for the MDI engine: the job's allocation under Slurm
        (``SLURM_CPUS_ON_NODE``, else ``SLURM_NTASKS``, since without cgroups
        the affinity mask is the whole node), otherwise the CPUs this process
        may use; capped by the job's ``ncores`` option when that is a number.
        """
        n = None
        for variable in ("SLURM_CPUS_ON_NODE", "SLURM_NTASKS"):
            value = os.environ.get(variable)
            if value:
                try:
                    n = int(value)
                    break
                except ValueError:
                    pass
        if n is None:
            try:
                n = len(os.sched_getaffinity(0))
            except AttributeError:  # macOS
                n = os.cpu_count() or 1
        # Slurm counts hardware threads as CPUs where hyperthreading is on,
        # and confines the job to those threads: the engine wants one rank
        # per PHYSICAL core, so count the distinct cores in the allowed set
        cores = physical_cores_allowed()
        if cores:
            n = min(n, cores)
        wanted = None
        try:
            wanted = self.node.global_options.get("ncores", "available")
        except Exception:
            pass
        if wanted not in (None, "", "available"):
            try:
                n = max(1, min(n, int(wanted)))
            except (TypeError, ValueError):
                pass
        return max(1, int(n))

    @contextlib.contextmanager
    def session(self, configuration, announce=True):
        """A warm engine for the configuration, as an :class:`EngineSession`,
        or None when the Model Chemistry must run as tasks."""
        self.fingerprints = None
        self.last_plan = self.plan(configuration)
        evaluator, build_argv, elements, coords_ang = self.last_plan
        if build_argv is None:
            yield None
            return

        from seamm_mdi import MDIEngine  # only for the MDI routes

        ncores = self.engine_ncores()
        if announce:
            mc = self.model_chemistry()
            self.say(
                f"The {mc['level']} engine runs on {ncores} core"
                + ("s." if ncores != 1 else ".")
            )
        with (
            self.engine_environment(ncores),
            MDIEngine(
                build_argv, elements, name="SEAMM", logger=self.node.logger
            ) as eng,
        ):
            session = EngineSession(
                self, eng, configuration, evaluator, elements, coords_ang
            )
            try:
                yield session
            finally:
                self.fingerprints = session.fingerprints()

    # ------------------------------------------------------------------
    def energy(self, configuration):
        """The energy (kJ/mol) of the configuration with the Model Chemistry."""
        from seamm_exec import Evaluator

        mc = self.model_chemistry()
        if mc is None:
            raise ValueError(f"{self.who} needs a Model Chemistry step before it.")
        with Evaluator(self.node, mc, properties=("energy",)) as evaluator:
            evaluator.submit(configuration, key="energy")
            for result in evaluator.results():
                if not result.ok:
                    raise RuntimeError(f"The energy failed: {result.reason}")
                return float(result.energy)
        raise RuntimeError("No energy was returned by the Model Chemistry")

    def optimize_and_energy(
        self, configuration, optimize=True, fmax=0.005, max_steps=200
    ):
        """Optionally optimize the structure over the warm engine, then its
        energy (kJ/mol). With no engine (tasks) the energy only."""
        self.fingerprints = None
        if not optimize or configuration.n_atoms == 1:
            return self.energy(configuration)
        with self.session(configuration, announce=False) as session:
            if session is None:
                return self.energy(configuration)
            session.optimize(fmax, max_steps)
            return session.energy(units="kJ/mol")

    def energy_and_hessian(self, configuration, delta_bohr=0.01):
        """The energy (kJ/mol), the Cartesian Hessian (hartree/bohr², (3N,
        3N)) and how it was obtained ("analytic", "finite differences over
        MDI" or "finite differences as tasks")."""
        return self.optimize_and_hessian(configuration, delta_bohr, optimize=False)

    def optimize_and_hessian(
        self, configuration, delta_bohr=0.01, optimize=True, fmax=0.005, max_steps=200
    ):
        """Optionally optimize the structure (ASE BFGS over the warm engine),
        then the energy and Hessian, in one engine session.

        Returns
        -------
        (float, numpy.ndarray, str)
            As :meth:`energy_and_hessian`.
        """
        self.fingerprints = None
        with self.session(configuration) as session:
            if session is None:
                if optimize:
                    self.say(
                        "The structure is not optimized: the Model Chemistry runs as "
                        "separate tasks, with no engine to drive the optimization."
                    )
                evaluator, _, _, coords_ang = self.last_plan
                return self.fd_hessian_tasks(
                    evaluator, configuration, coords_ang, delta_bohr
                )
            if optimize and session.n_atoms > 1:
                session.optimize(fmax, max_steps)
            E0 = session.energy(units="kJ/mol")
            if session.analytic:
                H, how = session.hessian()
                return E0, H, how
            if session.evaluator.path != "batch" or self.route.startswith(
                "warm engine"
            ):
                # The engine was asked for: the finite differences run over it
                # too (its fingerprints then describe the Hessian as well)
                H, how = session.hessian(delta_bohr=delta_bohr)
                return E0, H, how
            evaluator, coords_ang = session.evaluator, session.coordinates
        return self.fd_hessian_tasks(evaluator, configuration, coords_ang, delta_bohr)

    # ------------------------------------------------------------------
    def fd_hessian_tasks(self, evaluator, configuration, coords_ang, delta_bohr=0.01):
        """The energy and the central-difference Hessian (hartree/bohr²) from
        the gradients of the 6N displaced structures, computed together as tasks
        (concurrently, or on the job's queue; a rerun keeps the finished ones)."""
        x0 = np.asarray(coords_ang, dtype=float).reshape(-1)
        delta = Q_(delta_bohr, "bohr").m_as("Å")
        ndof = x0.size
        prefix = f"c{getattr(configuration, 'id', 0)}-fd"
        geometries = {f"{prefix}eq": x0.reshape(-1, 3)}
        for j in range(ndof):
            for sign, tag in ((1.0, "p"), (-1.0, "m")):
                x = x0.copy()
                x[j] += sign * delta
                geometries[f"{prefix}{j:04d}{tag}"] = x.reshape(-1, 3)
        results = self.evaluate(evaluator, configuration, geometries, "the Hessian")
        E0 = results[f"{prefix}eq"][0]
        H = np.zeros((ndof, ndof))
        for j in range(ndof):
            gp = results[f"{prefix}{j:04d}p"][1]
            gm = results[f"{prefix}{j:04d}m"][1]
            H[:, j] = (gp - gm) / (2.0 * delta_bohr)
        return E0, 0.5 * (H + H.T), "finite differences as tasks"

    def evaluate(self, evaluator, configuration, geometries, what="the calculation"):
        """Energies and gradients at many geometries through an Evaluator,
        whatever path it chose (warm MDI engine or tasks).

        Parameters
        ----------
        evaluator : seamm_exec.Evaluator
        configuration : molsystem._Configuration
            For the elements, charge and multiplicity.
        geometries : {key: array (N, 3)}
            Coordinates in Å. Keys must be unique strings.
        what : str
            For the message if one fails.

        Returns
        -------
        {key: (float, numpy.ndarray)}
            The energy, kJ/mol, and the gradient, E_h/bohr, (3N,).
        """
        from seamm_exec import Geometry

        elements = list(configuration.atoms.atomic_numbers)
        for key, x in geometries.items():
            evaluator.submit(
                Geometry(
                    elements,
                    np.asarray(x, dtype=float).reshape(-1, 3),
                    configuration.charge,
                    configuration.spin_multiplicity,
                ),
                key=key,
            )
        results = {}
        for result in evaluator.results():
            if not result.ok:
                raise RuntimeError(
                    f"A structure for {what} failed ({result.key}): {result.reason}"
                )
            gradient = (
                Q_(np.asarray(result.gradients, dtype=float), "kJ/mol/Å")
                .m_as("hartree/bohr")
                .reshape(-1)
            )
            results[result.key] = (float(result.energy), gradient)
        return results

    @staticmethod
    def fd_hessian_engine(engine, coords_ang, n_atoms, delta_bohr=0.01):
        """Central-difference Hessian (hartree/bohr²) from MDI forces. The engine
        is left at the undisplaced structure."""
        x0 = Q_(np.asarray(coords_ang, dtype=float), "Å").m_as("bohr").reshape(-1)
        ndof = 3 * n_atoms
        H = np.zeros((ndof, ndof))
        for j in range(ndof):
            xp = x0.copy()
            xp[j] += delta_bohr
            engine.set_coordinates(xp, units="bohr")
            fp = np.asarray(engine.forces(units="hartree/bohr"), dtype=float).reshape(
                -1
            )
            xm = x0.copy()
            xm[j] -= delta_bohr
            engine.set_coordinates(xm, units="bohr")
            fm = np.asarray(engine.forces(units="hartree/bohr"), dtype=float).reshape(
                -1
            )
            # Force = -dE/dx, so d²E/dx_i dx_j = -(f_i(+) - f_i(-)) / 2 delta.
            H[:, j] = -(fp - fm) / (2.0 * delta_bohr)
        engine.set_coordinates(x0, units="bohr")
        return 0.5 * (H + H.T)
