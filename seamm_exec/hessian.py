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
import json
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


def _declares_keyword(function, name):
    """Whether `function` names the keyword argument `name` itself (not just
    **kwargs, which may pass it on to something that does not know it)."""
    import inspect

    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


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


def model_chemistry_for(level, data_path=None, periodic=False):
    """The ``_model_chemistry`` wrapper for a level string, e.g.
    ``xTB:SQM@GFN2-xTB`` or ``MOPAC:SQM@PM7``, as the Model Chemistry step would
    publish it: for a second, cheap level next to the flowchart's own (the
    optimizer's cheap Hessians)."""
    from model_chemistry_step.model_chemistry import (
        discover_model_chemistries,
        match_model_chemistry,
        renamed_level,
    )

    level = renamed_level(level) or level
    available = discover_model_chemistries(periodic_only=periodic, data_path=data_path)
    if level in available:
        return available[level]
    mc = match_model_chemistry(level, available)
    if mc is None:
        raise ValueError(f"The model chemistry '{level}' is not available here.")
    return mc


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


#: Checks of every point of a finite-difference Hessian (2026-11-26): its <S**2>
#: may not move from the reference structure's by more than this ...
SPIN_TOLERANCE = 0.05
#: ... nor its energy fall below E0 - |g_j| delta (first order; the second order
#: only raises it at a minimum) by more than this margin, hartree (0.026 kJ/mol)
ENERGY_MARGIN = 1.0e-5
#: The gradient-difference check of a Hessian (Training Structures, recipe §3)
CHECK_DISPLACEMENTS = 3
CHECK_RMS = 0.005  # Å
CHECK_TOLERANCE = 0.02
CHECK_MAX_FREQUENCY = 5000.0  # cm^-1
#: The seed of the check's random displacements in the driver, so a rerun repeats
#: the same check
CHECK_SEED = 20261126
#: sqrt(hartree / (bohr^2 amu)) in cm^-1
_CM_PER_AU = 5140.4871
_BOHR = 0.529177210903  # Å


def masses_of(configuration):
    """The atomic masses (amu) of a configuration: its own, else the standard
    atomic weights of its elements."""
    atoms = configuration.atoms
    masses = getattr(atoms, "atomic_masses", None)
    if masses is not None:
        return np.asarray(masses, dtype=float)
    from seamm_util import element_data

    weight = {
        int(v["atomic number"]): float(v["atomic weight"])
        for v in element_data.values()
        if v.get("atomic number") is not None and v.get("atomic weight")
    }
    return np.asarray([weight[int(z)] for z in atoms.atomic_numbers], dtype=float)


def _optional(engine, name, *args):
    """Call the engine's optional method ``name`` if it has one; its result, or
    False."""
    method = getattr(engine, name, None)
    if method is None:
        return False
    try:
        return method(*args)
    except AttributeError:
        return False


class HessianRefused(RuntimeError):
    """A Hessian that failed a check: a state change at a displaced point of a
    finite difference, or disagreement with differences of the gradient."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


#: The version of the Hessian file's layout (save_hessian / load_hessian)
HESSIAN_FILE_VERSION = 1


def save_hessian(path, hessian, masses_amu, xyz_ang, symbols, **info):
    """Write a molecule's Hessian, and what it belongs to, as a compressed NumPy
    archive (.npz) that reads back without SEAMM (``numpy.load``).

    Arrays: ``hessian`` (3N, 3N) in E_h/bohr^2, Cartesian, not mass-weighted;
    ``masses`` (N,) in amu; ``xyz`` (N, 3) in Å, the geometry the Hessian was
    computed at; ``symbols`` (N,). ``info`` is a JSON string: the units, the
    file version, and the caller's keywords, e.g. route ("analytic over MDI",
    "finite differences over MDI", "finite differences as tasks"), settings
    (the engine's settings key), model chemistry, charge, multiplicity, the
    finite-difference details and the check.
    """
    import json

    hessian = np.asarray(hessian, dtype=float)
    xyz = np.asarray(xyz_ang, dtype=float).reshape(-1, 3)
    n = xyz.shape[0]
    if hessian.shape != (3 * n, 3 * n):
        raise ValueError(f"A Hessian of shape {hessian.shape} for {n} atoms.")
    meta = {
        "version": HESSIAN_FILE_VERSION,
        "units": {"hessian": "E_h/bohr^2", "masses": "amu", "xyz": "Å"},
        **info,
    }
    np.savez_compressed(
        path,
        hessian=hessian,
        masses=np.asarray(masses_amu, dtype=float),
        xyz=xyz,
        symbols=np.asarray([str(s) for s in symbols]),
        info=np.asarray(json.dumps(meta, default=str)),
    )


def load_hessian(path):
    """{"hessian", "masses", "xyz", "symbols", "info"} from save_hessian's file;
    "info" as a dict."""
    import json

    with np.load(path, allow_pickle=False) as data:
        return {
            "hessian": data["hessian"],
            "masses": data["masses"],
            "xyz": data["xyz"],
            "symbols": [str(s) for s in data["symbols"]],
            "info": json.loads(str(data["info"])),
        }


def frequencies(hessian, masses_amu):
    """The frequencies (cm^-1) of a Cartesian Hessian (hartree/bohr^2), from the
    mass-weighted matrix without projecting out the translations and rotations;
    imaginary ones are negative. Ascending."""
    H = np.asarray(hessian, dtype=float)
    w = 1.0 / np.sqrt(np.repeat(np.asarray(masses_amu, dtype=float), 3))
    values = np.linalg.eigvalsh(H * np.outer(w, w))
    return np.sign(values) * np.sqrt(np.abs(values)) * _CM_PER_AU


def zero_modes(hessian, masses_amu, coords_ang):
    """The magnitudes (cm^-1) of the modes that should be zero, the 6 (5 for a
    linear molecule) smallest in magnitude, largest first; information about a
    finite-difference Hessian's noise, not a test (sound ones reach 65 cm^-1 for
    small radicals: SEAMM jobs 6075, 6083, 2026-11-26)."""
    x = np.asarray(coords_ang, dtype=float).reshape(-1, 3)
    m = np.asarray(masses_amu, dtype=float)
    n_zero = 6
    if len(m) == 1:
        n_zero = 3
    else:
        centre = (m[:, None] * x).sum(0) / m.sum()
        r = x - centre
        inertia = (m * (r**2).sum(1)).sum() * np.eye(3) - (
            m[:, None, None] * (r[:, :, None] * r[:, None, :])
        ).sum(0)
        if np.min(np.linalg.eigvalsh(inertia)) < 1.0e-6 * max(
            1.0, np.max(np.linalg.eigvalsh(inertia))
        ):
            n_zero = 5
    nu = np.abs(frequencies(hessian, m))
    return sorted((float(v) for v in np.sort(nu)[:n_zero]), reverse=True)


def check_hessian(
    hessian,
    gradient,
    coords_ang,
    masses_amu,
    rng,
    n_displacements=CHECK_DISPLACEMENTS,
    rms=CHECK_RMS,
    tolerance=CHECK_TOLERANCE,
    max_frequency=CHECK_MAX_FREQUENCY,
    batch=None,
):
    """Test a Hessian against central differences of the gradient.

    For each of ``n_displacements`` random displacements d with an RMS of ``rms``
    Å, e = |H d - [g(x0 + d) - g(x0 - d)] / 2| / |H d|, with vector norms (2
    gradients each). The Hessian is refused if any e exceeds ``tolerance`` or any
    frequency magnitude exceeds ``max_frequency`` cm^-1.

    Parameters
    ----------
    hessian : array (3N, 3N)
        hartree/bohr^2.
    gradient : callable
        gradient(xyz (N, 3) Å) -> (3N,) hartree/bohr. Over a warm engine, give
        the gradient of :meth:`EngineSession.reference_guess`, so every gradient
        starts from the reference orbitals.
    batch : callable, optional
        batch([xyz, ...]) -> [gradient, ...], used instead of ``gradient`` to
        compute all the displaced gradients together (e.g. as tasks). The
        displacements drawn are the same.

    Returns
    -------
    dict
        "errors": [e], "max frequency": the largest frequency magnitude,
        "refused": bool and "reason": text or None.
    """
    x0 = np.asarray(coords_ang, dtype=float).reshape(-1)
    H = np.asarray(hessian, dtype=float)
    biggest = float(np.max(np.abs(frequencies(H, masses_amu))))
    displacements = []
    for _ in range(n_displacements):
        d = rng.standard_normal(x0.size)
        displacements.append(d * rms / np.sqrt(np.mean(d**2)))  # Å
    structures = []
    for d in displacements:
        structures += [(x0 + d).reshape(-1, 3), (x0 - d).reshape(-1, 3)]
    if batch is not None:
        gradients = [np.asarray(g, dtype=float) for g in batch(structures)]
    else:
        gradients = [np.asarray(gradient(x), dtype=float) for x in structures]
    errors = []
    for i, d in enumerate(displacements):
        Hd = H @ (d / _BOHR)
        dg = 0.5 * (gradients[2 * i] - gradients[2 * i + 1])
        errors.append(float(np.linalg.norm(Hd - dg) / np.linalg.norm(Hd)))
    reason = None
    if biggest > max_frequency:
        reason = f"a frequency of {biggest:.0f} cm⁻¹ (> {max_frequency:.0f})"
    elif max(errors) > tolerance:
        reason = f"it disagrees with the gradients by {max(errors):.3f} (> {tolerance})"
    return {
        "errors": [round(e, 6) for e in errors],
        "max frequency": round(biggest, 1),
        "refused": reason is not None,
        "reason": reason,
    }


def check_fd_points(
    e0,
    g0,
    spin0,
    points,
    delta_bohr,
    hessian=None,
    spin_tolerance=SPIN_TOLERANCE,
    energy_margin=ENERGY_MARGIN,
):
    """What is wrong with the displaced points of a finite difference: a state
    change, seen as an <S**2> that moved from the reference's, or an energy
    below what the reference's gradient allows.

    Parameters
    ----------
    e0 : float
        The reference energy, hartree.
    g0 : array (3N,)
        The reference gradient, hartree/bohr.
    spin0 : (float, float) or None
        The reference (<S**2>, S(S+1)); None or (None, None) for a closed shell.
    points : [{"j": int, "sign": +1 or -1, "energy": float, "spin": (s2, ideal)}]
        Each displaced point: coordinate j moved by sign * delta_bohr.
    hessian : array (3N, 3N), optional
        Lowers the bound by 1/2 H_jj delta^2 where H_jj is negative.

    Returns
    -------
    [str]
        The problems, empty if none.
    """
    problems = []
    g0 = np.asarray(g0, dtype=float).reshape(-1)
    s0 = None if spin0 is None else spin0[0]
    for p in points:
        j, sign = p["j"], p["sign"]
        where = f"coordinate {j} {'+' if sign > 0 else '-'}{delta_bohr} bohr"
        bound = e0 - abs(g0[j]) * delta_bohr - energy_margin
        if hessian is not None:
            bound += 0.5 * min(float(hessian[j, j]), 0.0) * delta_bohr**2
        energy = p.get("energy")
        if energy is not None and energy < bound:
            problems.append(
                f"{where}: the energy is {(e0 - energy) * 2625.4996:.3f} kJ/mol below "
                "the reference, more than its gradient allows (a different state?)"
            )
        spin = p.get("spin")
        s2 = None if spin is None else spin[0]
        if s0 is not None and s2 is not None and abs(s2 - s0) > spin_tolerance:
            problems.append(
                f"{where}: <S**2> {s2:.4f}, the reference's {s0:.4f} (more than "
                f"{spin_tolerance} apart)"
            )
    return problems


class _RestartingEngine:
    """A warm MDI engine that is started again when it dies.

    An engine process can die in the middle of a session -- MOPAC's library
    aborts at random in some open-shell calculations, for one -- and the driver
    sees only a broken connection ("MDI_Recv failed"). Then a new engine is
    started, given the last coordinates, and asked again, at most
    ``restarts`` times per session; a calculation that fails on a fresh engine
    too is a real failure and is raised. Everything else is passed to the
    engine.

    Parameters
    ----------
    make : callable() -> engine
        A new, not yet started engine (a context manager, as
        ``seamm_mdi.MDIEngine`` is).
    restarts : int
        How many times the engine may be started again in this session.
    say : callable(str), optional
        Where to tell the user.
    """

    #: The engine's calls that run a calculation, retried on a new engine
    _CALCULATIONS = ("energy", "forces", "hessian", "stress")

    def __init__(self, make, restarts=2, say=None):
        self._make = make
        self.restarts = restarts
        self.n_restarts = 0
        self._say = say
        self._coordinates = None
        # Replayed on a new engine: the tight SCF, and where the guess was saved
        self._tight = False
        self._guess_at = None
        self.engine = make().__enter__()

    def close(self):
        engine, self.engine = self.engine, None
        if engine is not None:
            engine.__exit__(None, None, None)

    def set_coordinates(self, xyz, units=None):
        self._coordinates = (np.array(xyz, dtype=float), units)
        if units is None:
            return self.engine.set_coordinates(xyz)
        return self.engine.set_coordinates(xyz, units=units)

    def tight_scf(self, tight=True):
        """As the engine's (seamm_mdi.MDIEngine.tight_scf); False if it has none."""
        method = getattr(self.engine, "tight_scf", None)
        done = bool(method(tight)) if method is not None else False
        self._tight = tight and done
        return done

    def save_guess(self):
        """As the engine's save_guess, remembering where, so a new engine can
        make the same reference guess. False if the engine cannot."""
        method = getattr(self.engine, "save_guess", None)
        done = bool(method()) if method is not None else False
        self._guess_at = self._coordinates if done else None
        return done

    def restore_guess(self):
        method = getattr(self.engine, "restore_guess", None)
        return bool(method()) if method is not None else False

    def __getattr__(self, name):
        attribute = getattr(self.engine, name)
        if name not in self._CALCULATIONS or not callable(attribute):
            return attribute

        def calculation(*args, **kwargs):
            while True:
                try:
                    return getattr(self.engine, name)(*args, **kwargs)
                except Exception as e:
                    if self.n_restarts >= self.restarts:
                        raise
                    self._restart(e)

        return calculation

    def _restart(self, error):
        self.n_restarts += 1
        message = (
            f"The MDI engine stopped ({error}); it was started again and the "
            f"calculation repeated ({self.n_restarts} of at most {self.restarts} "
            "restarts)."
        )
        logger.warning(message)
        if self._say is not None:
            self._say(message)
        try:
            self.close()
        except Exception as e:  # the old engine is gone; nothing to tidy up
            logger.debug(f"Closing the stopped MDI engine: {e}")
        self.engine = self._make().__enter__()
        if self._tight:
            self.engine.tight_scf(True)
        if self._guess_at is not None:
            # The reference guess again: an SCF at the reference structure
            xyz, units = self._guess_at
            if units is None:
                self.engine.set_coordinates(xyz)
            else:
                self.engine.set_coordinates(xyz, units=units)
            self.engine.forces()
            self.engine.save_guess()
        if self._coordinates is not None:
            xyz, units = self._coordinates
            self.set_coordinates(xyz, units)


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
        # (<S**2>, S(S+1)) at the end of an optimization (see optimize_in_blocks)
        self.final_spin = None
        # How the last finite-difference Hessian was made: the guess, the SCF, the
        # points' checks and its zero modes (see fd_hessian)
        self.fd_info = None

    @property
    def masses(self):
        """The atomic masses, amu."""
        return masses_of(self.configuration)

    @contextlib.contextmanager
    def reference_guess(self, coords_ang=None, tight=False):
        """The engine set up for precise differences around a structure.

        With ``tight`` the engine's SCF is tightened (if it can be). One SCF is
        run at the structure (the reference's or the given) and its orbitals saved
        as the guess (if the engine can), and every gradient of the yielded state
        starts from them: otherwise each SCF starts from the previous one's, and a
        finite difference depends on the order of its points (4-hydroxy-TEMPO,
        SEAMM job 6074). Afterwards the SCF is the engine's own again and the
        engine is at the session's reference coordinates.

        Yields
        ------
        dict
            "e0" (hartree), "g0" (3N, hartree/bohr) and "spin0" at the
            structure; "guess" and "tight", whether the engine could; and
            "gradient", a callable(xyz Å) -> (3N,) hartree/bohr that starts each
            SCF from the saved orbitals.
        """
        engine = self.engine
        x0 = np.asarray(
            self.coordinates if coords_ang is None else coords_ang, dtype=float
        ).reshape(-1, 3)
        done_tight = bool(_optional(engine, "tight_scf", True)) if tight else False
        try:
            engine.set_coordinates(x0, units="Å")
            g0 = -np.asarray(engine.forces(units="hartree/bohr"), dtype=float).reshape(
                -1
            )
            e0 = float(engine.energy(units="hartree"))
            spin0 = self.spin()
            guess = bool(_optional(engine, "save_guess"))

            def gradient(xyz):
                if guess:
                    engine.restore_guess()
                return self.gradient(xyz)

            yield {
                "e0": e0,
                "g0": g0,
                "spin0": spin0,
                "guess": guess,
                "tight": done_tight,
                "gradient": gradient,
            }
        finally:
            if done_tight:
                _optional(engine, "tight_scf", False)
            engine.set_coordinates(self.coordinates, units="Å")

    def fd_hessian(self, coords_ang=None, delta_bohr=0.01):
        """The central-difference Hessian (hartree/bohr^2) of the engine's forces,
        made as reliably as the engine allows: a tight SCF, every point started
        from the reference structure's orbitals, and every point checked for a
        state change (its <S**2> and its energy; :func:`check_fd_points`).

        Raises
        ------
        HessianRefused
            If a point changed state.
        """
        x = np.asarray(
            self.coordinates if coords_ang is None else coords_ang, dtype=float
        ).reshape(-1, 3)
        with self.reference_guess(x, tight=True) as ref:
            points = []

            def before(j, sign):
                if ref["guess"]:
                    self.engine.restore_guess()

            def after(j, sign):
                points.append(
                    {
                        "j": j,
                        "sign": sign,
                        "energy": float(self.engine.energy(units="hartree")),
                        "spin": self.spin(),
                    }
                )

            H = HessianDriver.fd_hessian_engine(
                self.engine,
                x,
                self.n_atoms,
                delta_bohr,
                before_each=before,
                after_each=after,
            )
        problems = check_fd_points(
            ref["e0"], ref["g0"], ref["spin0"], points, delta_bohr, hessian=H
        )
        try:
            zero = zero_modes(H, self.masses, x)
        except Exception:  # pragma: no cover - information only
            zero = None
        self.fd_info = {
            "reference guess": ref["guess"],
            "tight SCF": ref["tight"],
            "points": len(points),
            "problems": problems,
            "zero modes": None if zero is None else [round(v, 1) for v in zero],
        }
        if problems:
            raise HessianRefused(
                "The finite-difference Hessian is refused: "
                + "; ".join(problems[:3])
                + (f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""),
                self.fd_info,
            )
        return H

    def check_hessian(self, hessian, rng, coords_ang=None, **kwargs):
        """:func:`check_hessian` with gradients that each start from the
        reference structure's orbitals, with a tight SCF. kwargs: n_displacements,
        rms, tolerance, max_frequency."""
        x = np.asarray(
            self.coordinates if coords_ang is None else coords_ang, dtype=float
        ).reshape(-1, 3)
        with self.reference_guess(x, tight=True) as ref:
            result = check_hessian(
                hessian, ref["gradient"], x, self.masses, rng, **kwargs
            )
        result["reference guess"] = ref["guess"]
        result["tight SCF"] = ref["tight"]
        return result

    @property
    def n_atoms(self):
        return len(self.elements)

    @property
    def analytic(self):
        """Whether the engine gives an analytic Hessian. It advertises the
        custom ``<HESSIAN`` command only when the method has a genuine one."""
        return bool(self.engine.supports("<HESSIAN"))

    def optimize(
        self,
        fmax=0.005,
        max_steps=200,
        optimizer="ASE BFGS",
        optimizer_options=None,
        cheap=None,
    ):
        """Optimize over the warm engine; updates the configuration and the
        reference coordinates. fmax in eV/Å. Returns whether it converged.

        optimizer: "ASE BFGS" (the default) or "SEAMM" (seamm_optimizer: internal
        coordinates, a model or cheap-level Hessian, a minimum check); with
        "SEAMM", `optimizer_options` go to seamm_optimizer.Optimizer and `cheap`,
        an EngineSession at a cheap level, gives its Hessians."""
        if optimizer == "SEAMM" and self.n_atoms >= 2:
            return self._optimize_seamm(fmax, max_steps, optimizer_options or {}, cheap)
        from ase import Atoms
        from ase.optimize import BFGS

        if self.n_atoms < 2:
            self.driver.last_optimization = {"converged": True, "evaluations": 0}
            return True
        atoms = Atoms(numbers=self.elements, positions=self.coordinates)
        atoms.calc = _MDICalculator(self.engine)
        converged = BFGS(atoms, logfile=None).run(fmax=fmax, steps=max_steps)
        self.coordinates = np.asarray(atoms.positions, dtype=float)
        self.configuration.atoms.set_coordinates(self.coordinates, fractionals=False)
        self.driver.last_optimization = {
            "converged": bool(converged),
            "evaluations": atoms.calc.n_calls,
            "max steps": max_steps,
            "fmax": fmax,
        }
        self.driver.say(
            f"The structure was optimized in {atoms.calc.n_calls} energy evaluations"
            + ("." if converged else f" without converging in {max_steps} steps.")
        )
        return bool(converged)

    def _optimize_seamm(self, fmax, max_steps, options, cheap):
        """seamm_optimizer over the warm engine (see :meth:`optimize`)."""
        try:
            from seamm_optimizer import Optimizer
        except ImportError:
            raise RuntimeError(
                "The SEAMM optimizer needs the seamm_optimizer package."
            ) from None
        from ase.data import chemical_symbols

        calls = [0]

        def engine(xyz):
            calls[0] += 1
            self.engine.set_coordinates(np.asarray(xyz, dtype=float), units="Å")
            energy = self.engine.energy(units="eV")
            forces = np.asarray(self.engine.forces(units="eV/Å"), dtype=float)
            return energy, -forces.reshape(-1, 3)

        cheap_hessian = None
        if cheap is not None:
            factor = Q_(1.0, "E_h/bohr^2").m_as("eV/Å^2")

            def _cheap_hessian(xyz):
                H, _ = cheap.hessian(coords_ang=np.asarray(xyz, dtype=float))
                return np.asarray(H) * factor

            cheap_hessian = _cheap_hessian

        options = dict(options)
        options.setdefault("hessian", "cheap" if cheap is not None else "lindh")
        options.setdefault("check_minimum", False)
        symbols = [chemical_symbols[z] for z in self.elements]
        result = Optimizer(
            engine,
            symbols,
            self.coordinates,
            fmax=fmax,
            max_steps=max_steps,
            cheap_hessian_engine=cheap_hessian,
            **options,
        ).run()
        self.coordinates = np.asarray(result.xyz, dtype=float)
        self.configuration.atoms.set_coordinates(self.coordinates, fractionals=False)
        self.driver.last_optimization = {
            "converged": bool(result.converged),
            "evaluations": calls[0],
            "max steps": max_steps,
            "fmax": fmax,
            "optimizer": "SEAMM",
            "coordinates": result.coordinates,
            "cheap hessians": result.n_cheap_hessians,
            "refreshes": result.n_refreshes,
            "fallbacks": result.n_fallbacks,
            "minimum": result.minimum,
            "lowest frequency": result.lowest_frequency,
            "escapes": len(result.escapes),
            "confirmations": len(getattr(result, "confirmations", []) or []),
            "energy (eV)": float(result.energy),
            "max force (eV/Å)": float(result.max_force),
            "rms force (eV/Å)": float(
                np.sqrt(np.mean(np.sum(np.asarray(result.gradient) ** 2, axis=1)))
            ),
            "max step (Å)": float(result.last_step),
        }
        self.driver.say(
            f"The structure was optimized (SEAMM optimizer, {result.coordinates}) in "
            f"{calls[0]} energy evaluations"
            + (f" and {result.n_cheap_hessians} cheap-level Hessians" if cheap else "")
            + (
                "."
                if result.converged
                else f" without converging in {max_steps} steps."
            )
        )
        return bool(result.converged)

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
            return self.fd_hessian(x, delta_bohr), "finite differences over MDI"
        finally:
            self.engine.set_coordinates(self.coordinates, units="Å")

    def spin(self):
        """(<S**2>, S(S+1)) of the engine's last energy/gradient, (None, None) for a
        closed shell, or None when the engine does not give them (seamm_mdi
        2026.11.23, ORCA's engine first)."""
        get = getattr(self.engine, "spin", None)
        if get is None:
            return None
        try:
            return get()
        except Exception as e:  # pragma: no cover
            logger.warning(f"The engine gave no <S**2>: {e}")
            return None

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
    precision : str, optional
        "tight" asks the MDI engine for tight convergence (e.g. for finite
        differences of gradients), from programs whose
        ``get_mdi_engine_command`` takes a ``precision`` argument. Default:
        the program's own settings.

    Attributes
    ----------
    fingerprints : dict or None
        The engine's settings fingerprints from the last engine session.
    engine_restarts : int
        How many times an engine that dies in a session is started again and
        the calculation repeated (2).
    last_optimization : dict or None
        The last optimization over an engine: ``converged`` (bool),
        ``evaluations``, ``max steps`` and ``fmax`` (eV/Å). None when the last
        call optimized nothing.
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
        precision=None,
    ):
        self.node = node
        self.precision = precision
        self._model_chemistry = model_chemistry
        self.route = route or "automatic"
        self.who = who or getattr(node, "title", None) or type(node).__name__
        self.hint = hint
        self.printer = print_to or printer
        self.fingerprints = None
        # (<S**2>, S(S+1)) at the end of the last optimization over a warm engine
        self.spin = None
        self.engine_restarts = 2
        self.last_optimization = None
        # How the last finite-difference Hessian was made (EngineSession.fd_info,
        # or the tasks route's), and the last gradient-difference check
        self.fd_info = None
        self.last_check = None
        self.last_spins = {}
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
        has_command = hasattr(step, "get_mdi_engine_command")
        takes_ncores = has_command and _accepts_keyword(
            step.get_mdi_engine_command, "ncores"
        )
        # for the announcement of the engine's cores (see engine_cores_text)
        self.engine_takes_ncores = takes_ncores
        precision = self.precision
        if precision and has_command:
            if not _declares_keyword(step.get_mdi_engine_command, "precision"):
                self.say(
                    f"The {mc['level']} engine cannot be asked for {precision} "
                    "convergence, so finite differences of its results may be noisy."
                )
                precision = None

        def build_argv(hostname, port):
            kwargs = {}
            if takes_ncores:  # ORCA does; MOPAC's engine is single-threaded
                kwargs["ncores"] = ncores
            if basis is not None:
                kwargs["basis"] = basis
            if precision:
                kwargs["precision"] = precision
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

    def engine_cores_text(self, argv, ncores):
        """What the engine runs on, for the output: the job's cores when its
        launch command takes a core count (ORCA); else the OpenMP threads its
        command sets (``OMP_NUM_THREADS=n``, as xTB and xnn do); else the count
        its step declares (``mdi_engine_threads``; MOPAC's engine is
        single-threaded); else that the program sets its own threads."""
        level = (self.model_chemistry() or {}).get("level", "model chemistry")

        def cores(n):
            return f"{n} core" + ("s" if n != 1 else "")

        if getattr(self, "engine_takes_ncores", False):
            return f"The {level} engine runs on {cores(ncores)}."
        for token in argv or []:
            name, equals, value = str(token).partition("=")
            if not equals or not name.replace("_", "").isalnum():
                break  # past the VAR=value prefix
            if name == "OMP_NUM_THREADS" and value.isdigit():
                return f"The {level} engine runs on {cores(int(value))}."
        declared = getattr(getattr(self, "step", None), "mdi_engine_threads", None)
        if isinstance(declared, int) and declared > 0:
            return f"The {level} engine runs on {cores(declared)}" + (
                " (it is single-threaded)." if declared == 1 else "."
            )
        return (
            f"The {level} engine sets its own threads; the job has " f"{cores(ncores)}."
        )

    def engine_ncores(self):
        """The cores for the MDI engine: in an iteration of a parallel loop its
        share of the job (``SEAMM_CE``, ``NTASKS`` × ``CPUS_PER_TASK``), else the
        job's allocation under Slurm (``SLURM_CPUS_ON_NODE``, else
        ``SLURM_NTASKS``, since without cgroups the affinity mask is the whole
        node), otherwise the CPUs this process may use; capped by the job's
        ``ncores`` option when that is a number.

        The iteration inherits the job's Slurm variables, so reading them there
        gave every iteration's engine all of the job's cores: six iterations of
        4 cores in a 24-core job ran 6 × 24 ranks on 24 cores (ChemAI 5687).
        """
        n = None
        from seamm_exec.iteration import CE_ENVIRONMENT
        from seamm_exec.local_pool import pool_cores

        share = os.environ.get(CE_ENVIRONMENT)
        if share:
            try:

                n = pool_cores(json.loads(share))
            except (TypeError, ValueError):
                n = None
        if n is None:
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
        self.spin = None
        self.last_plan = self.plan(configuration)
        evaluator, build_argv, elements, coords_ang = self.last_plan
        if build_argv is None:
            yield None
            return

        from seamm_mdi import MDIEngine  # only for the MDI routes

        ncores = self.engine_ncores()
        # Say how many cores the engine really uses, from its launch command, the
        # first time it starts (not again on a restart)
        announced = [] if announce else [True]

        def launch(hostname, port):
            argv = build_argv(hostname, port)
            if not announced:
                announced.append(True)
                self.say(self.engine_cores_text(argv, ncores))
            return argv

        with self.engine_environment(ncores):
            eng = _RestartingEngine(
                lambda: MDIEngine(
                    launch, elements, name="SEAMM", logger=self.node.logger
                ),
                restarts=self.engine_restarts,
                say=self.say,
            )
            try:
                session = EngineSession(
                    self, eng, configuration, evaluator, elements, coords_ang
                )
                try:
                    yield session
                finally:
                    self.fingerprints = session.fingerprints()
                    self.spin = session.final_spin
            finally:
                eng.close()

    @contextlib.contextmanager
    def cheap_session(self, configuration, level):
        """A warm engine at a second, cheap level (a level string such as
        ``xTB:SQM@GFN2-xTB``) for the same configuration, for the optimizer's
        Hessians; None when that level cannot run as a warm engine here."""
        data_path = getattr(self.node, "data_path", None)
        cheap = HessianDriver(
            self.node,
            model_chemistry_for(level, data_path=data_path),
            who=f"{self.who} (cheap level {level})",
            print_to=self.printer,
        )
        with cheap.session(configuration, announce=False) as session:
            yield session

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

    def optimize_in_blocks(
        self,
        session,
        fmax=0.005,
        max_steps=200,
        continuations=0,
        on_continue=None,
        optimizer="ASE BFGS",
        optimizer_options=None,
        cheap_level=None,
    ):
        """Optimize over the session's warm engine in blocks of `max_steps`:
        when a block does not converge, up to `continuations` more, each a fresh
        BFGS from where the last stopped. ``on_continue(max_steps)`` is called
        before each continuation (e.g. to say so). Everything after the
        optimization -- the energy, the Hessian -- is then done once, at the
        final structure. ``last_optimization`` covers all the blocks: the
        ``evaluations`` summed and the number of ``blocks``. Returns whether it
        converged. `optimizer`, `optimizer_options` and `cheap_level` (a level
        string for the SEAMM optimizer's Hessians) are as in
        :meth:`EngineSession.optimize`."""
        if optimizer == "SEAMM" and cheap_level:
            with self.cheap_session(session.configuration, cheap_level) as cheap:
                return self._optimize_blocks(
                    session,
                    fmax,
                    max_steps,
                    continuations,
                    on_continue,
                    optimizer,
                    optimizer_options,
                    cheap,
                )
        return self._optimize_blocks(
            session,
            fmax,
            max_steps,
            continuations,
            on_continue,
            optimizer,
            optimizer_options,
            None,
        )

    def _optimize_blocks(
        self,
        session,
        fmax,
        max_steps,
        continuations,
        on_continue,
        optimizer,
        optimizer_options,
        cheap,
    ):
        evaluations = 0
        blocks = 0
        while True:
            if optimizer == "ASE BFGS" and not optimizer_options and cheap is None:
                # the long-standing call, so sessions with the old signature work
                converged = session.optimize(fmax, max_steps)
            else:
                converged = session.optimize(
                    fmax,
                    max_steps,
                    optimizer=optimizer,
                    optimizer_options=optimizer_options,
                    cheap=cheap,
                )
            blocks += 1
            evaluations += (self.last_optimization or {}).get("evaluations", 0)
            if converged or blocks > continuations:
                break
            if on_continue is not None:
                on_continue(max_steps)
        if self.last_optimization is not None:
            self.last_optimization.update(evaluations=evaluations, blocks=blocks)
        # <S**2> at the final structure, now, before a finite-difference Hessian
        # moves the engine to displaced ones; no extra calculation
        session.final_spin = session.spin()
        return converged

    def optimize_and_energy(
        self,
        configuration,
        optimize=True,
        fmax=0.005,
        max_steps=200,
        continuations=0,
        on_continue=None,
        optimizer="ASE BFGS",
        optimizer_options=None,
        cheap_level=None,
    ):
        """Optionally optimize the structure over the warm engine, then its
        energy (kJ/mol). With no engine (tasks) the energy only.
        `continuations` and `on_continue` are as in :meth:`optimize_in_blocks`."""
        self.fingerprints = None
        self.spin = None
        self.last_optimization = None
        if not optimize or configuration.n_atoms == 1:
            return self.energy(configuration)
        with self.session(configuration, announce=False) as session:
            if session is None:
                return self.energy(configuration)
            self.optimize_in_blocks(
                session,
                fmax,
                max_steps,
                continuations,
                on_continue,
                optimizer,
                optimizer_options,
                cheap_level,
            )
            return session.energy(units="kJ/mol")

    def energy_and_hessian(self, configuration, delta_bohr=0.01):
        """The energy (kJ/mol), the Cartesian Hessian (hartree/bohr², (3N,
        3N)) and how it was obtained ("analytic", "finite differences over
        MDI" or "finite differences as tasks")."""
        return self.optimize_and_hessian(configuration, delta_bohr, optimize=False)

    def optimize_and_hessian(
        self,
        configuration,
        delta_bohr=0.01,
        optimize=True,
        fmax=0.005,
        max_steps=200,
        continuations=0,
        on_continue=None,
        optimizer="ASE BFGS",
        optimizer_options=None,
        cheap_level=None,
        finite_differences=False,
        check=False,
    ):
        """Optionally optimize the structure (ASE BFGS, or the SEAMM optimizer, over
        the warm engine), then the energy and Hessian, in one engine session. With
        `continuations` an optimization that does not converge in `max_steps`
        continues in the same session (see :meth:`optimize_in_blocks`), so the
        Hessian is computed once, at the final structure. With
        `finite_differences` the Hessian is central differences of the engine's
        forces even when the engine has an analytic one (e.g. to check or replace
        a broken analytic Hessian).

        A finite-difference Hessian is made with a tight SCF, each point starting
        from the reference structure's orbitals, and each point checked for a
        state change (see :meth:`EngineSession.fd_hessian`). With `check` it is
        then tested against differences of the gradient (:func:`check_hessian`,
        as for a replacement Hessian). Either failure raises
        :class:`HessianRefused`; ``self.fd_info`` and ``self.last_check`` say how
        it was made and checked.

        Over a warm engine an analytic Hessian is checked the same way when
        `check` is given (from the reference structure's orbitals, with a tight
        SCF): `check` True refuses a failing one (HessianRefused); `check`
        "report" only records the result in ``self.last_check``, so the caller
        can list it.

        Returns
        -------
        (float, numpy.ndarray, str)
            As :meth:`energy_and_hessian`.
        """
        self.fingerprints = None
        self.spin = None
        self.last_optimization = None
        self.fd_info = None
        self.last_check = None
        with self.session(configuration) as session:
            if session is None:
                if optimize:
                    self.say(
                        "The structure is not optimized: the Model Chemistry runs as "
                        "separate tasks, with no engine to drive the optimization."
                    )
                evaluator, _, _, coords_ang = self.last_plan
                return self.fd_hessian_tasks(
                    evaluator, configuration, coords_ang, delta_bohr, check=check
                )
            if optimize and session.n_atoms > 1:
                self.optimize_in_blocks(
                    session,
                    fmax,
                    max_steps,
                    continuations,
                    on_continue,
                    optimizer,
                    optimizer_options,
                    cheap_level,
                )
            E0 = session.energy(units="kJ/mol")
            if session.analytic and not finite_differences:
                H, how = session.hessian()
                if check:
                    self.last_check = session.check_hessian(
                        H, np.random.default_rng(CHECK_SEED)
                    )
                    if self.last_check["refused"] and check != "report":
                        raise HessianRefused(
                            "The analytic Hessian is refused: "
                            + self.last_check["reason"],
                            self.last_check,
                        )
                return E0, H, how
            if (
                finite_differences
                or session.evaluator.path != "batch"
                or self.route.startswith("warm engine")
            ):
                # The engine was asked for: the finite differences run over it
                # too (its fingerprints then describe the Hessian as well)
                try:
                    H, how = session.hessian(
                        delta_bohr=delta_bohr, finite_differences=True
                    )
                finally:
                    self.fd_info = session.fd_info
                if check:
                    self.last_check = session.check_hessian(
                        H, np.random.default_rng(CHECK_SEED)
                    )
                    if self.last_check["refused"]:
                        raise HessianRefused(
                            "The finite-difference Hessian is refused: "
                            + self.last_check["reason"],
                            self.last_check,
                        )
                return E0, H, how
            evaluator, coords_ang = session.evaluator, session.coordinates
        return self.fd_hessian_tasks(
            evaluator, configuration, coords_ang, delta_bohr, check=check
        )

    # ------------------------------------------------------------------
    def fd_hessian_tasks(
        self, evaluator, configuration, coords_ang, delta_bohr=0.01, check=False
    ):
        """The energy and the central-difference Hessian (hartree/bohr²) from
        the gradients of the 6N displaced structures, computed together as tasks
        (concurrently, or on the job's queue; a rerun keeps the finished ones).

        Each task starts from its own guess. They run with
        ``precision="tight"``, every point is checked for a state change (its
        <S**2> and energy, :func:`check_fd_points`), and with `check` the Hessian
        is tested against gradient differences, as tasks too. Either failure
        raises :class:`HessianRefused`."""
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
        results = self.evaluate(
            evaluator, configuration, geometries, "the Hessian", precision="tight"
        )
        spins = self.last_spins
        E0 = results[f"{prefix}eq"][0]
        H = np.zeros((ndof, ndof))
        for j in range(ndof):
            gp = results[f"{prefix}{j:04d}p"][1]
            gm = results[f"{prefix}{j:04d}m"][1]
            H[:, j] = (gp - gm) / (2.0 * delta_bohr)
        H = 0.5 * (H + H.T)
        kj_per_hartree = Q_(1.0, "hartree").m_as("kJ/mol")
        points = [
            {
                "j": j,
                "sign": sign,
                "energy": results[f"{prefix}{j:04d}{tag}"][0] / kj_per_hartree,
                "spin": spins.get(f"{prefix}{j:04d}{tag}"),
            }
            for j in range(ndof)
            for sign, tag in ((1, "p"), (-1, "m"))
        ]
        problems = check_fd_points(
            E0 / kj_per_hartree,
            results[f"{prefix}eq"][1],
            spins.get(f"{prefix}eq"),
            points,
            delta_bohr,
            hessian=H,
        )
        masses = masses_of(configuration)
        try:
            zero = [round(v, 1) for v in zero_modes(H, masses, x0)]
        except Exception:  # pragma: no cover - information only
            zero = None
        self.fd_info = {
            "reference guess": None,  # each task starts from its own guess
            "tight SCF": True,
            "points": len(points),
            "problems": problems,
            "zero modes": zero,
        }
        if problems:
            raise HessianRefused(
                "The finite-difference Hessian is refused: " + "; ".join(problems[:3]),
                self.fd_info,
            )
        if check:

            def batch(structures):
                keyed = {f"{prefix}chk{i:02d}": x for i, x in enumerate(structures)}
                done = self.evaluate(
                    evaluator, configuration, keyed, "the check", precision="tight"
                )
                return [done[k][1] for k in keyed]

            self.last_check = check_hessian(
                H,
                None,
                x0,
                masses,
                np.random.default_rng(CHECK_SEED),
                batch=batch,
            )
            if self.last_check["refused"]:
                raise HessianRefused(
                    "The finite-difference Hessian is refused: "
                    + self.last_check["reason"],
                    self.last_check,
                )
        return E0, H, "finite differences as tasks"

    def evaluate(
        self,
        evaluator,
        configuration,
        geometries,
        what="the calculation",
        precision=None,
    ):
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
        precision : str, optional
            Overrides the driver's precision (e.g. "tight" for finite
            differences).

        Returns
        -------
        {key: (float, numpy.ndarray)}
            The energy, kJ/mol, and the gradient, E_h/bohr, (3N,). The
            (<S**2>, S(S+1)) of each, where the program gives them, are in
            ``self.last_spins`` by key.
        """
        from seamm_exec import Geometry

        elements = list(configuration.atoms.atomic_numbers)
        # The requested precision goes to the tasks too (providers that know it,
        # e.g. ORCA, tighten the SCF; others ignore the option)
        precision = precision or self.precision
        options = {"precision": precision} if precision else None
        for key, x in geometries.items():
            evaluator.submit(
                Geometry(
                    elements,
                    np.asarray(x, dtype=float).reshape(-1, 3),
                    configuration.charge,
                    configuration.spin_multiplicity,
                ),
                key=key,
                options=options,
            )
        results = {}
        spins = {}
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
            spin = (getattr(result, "data", None) or {}).get("spin")
            if isinstance(spin, dict) and spin.get("S**2") is not None:
                spins[result.key] = (spin["S**2"], spin.get("S(S+1)"))
        self.last_spins = spins
        return results

    @staticmethod
    def fd_hessian_engine(
        engine, coords_ang, n_atoms, delta_bohr=0.01, before_each=None, after_each=None
    ):
        """Central-difference Hessian (hartree/bohr²) from MDI forces. The engine
        is left at the undisplaced structure. ``before_each(j, sign)`` is called
        before each displaced point's forces (e.g. to restore the reference
        guess), ``after_each(j, sign)`` after them (e.g. to read its energy and
        <S**2>)."""
        x0 = Q_(np.asarray(coords_ang, dtype=float), "Å").m_as("bohr").reshape(-1)
        ndof = 3 * n_atoms
        H = np.zeros((ndof, ndof))

        def forces_at(x, j, sign):
            engine.set_coordinates(x, units="bohr")
            if before_each is not None:
                before_each(j, sign)
            f = np.asarray(engine.forces(units="hartree/bohr"), dtype=float).reshape(-1)
            if after_each is not None:
                after_each(j, sign)
            return f

        for j in range(ndof):
            xp = x0.copy()
            xp[j] += delta_bohr
            fp = forces_at(xp, j, 1)
            xm = x0.copy()
            xm[j] -= delta_bohr
            fm = forces_at(xm, j, -1)
            # Force = -dE/dx, so d²E/dx_i dx_j = -(f_i(+) - f_i(-)) / 2 delta.
            H[:, j] = -(fp - fm) / (2.0 * delta_bohr)
        engine.set_coordinates(x0, units="bohr")
        return 0.5 * (H + H.T)
