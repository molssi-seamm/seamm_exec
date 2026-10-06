# -*- coding: utf-8 -*-

"""Timing records of the calculations steps run, shared by the plug-ins.

Each plug-in appends one row per calculation to
``~/.seamm.d/timing/<program>.csv`` (the files Gaussian, LAMMPS, VASP and MOPAC
already keep), which are fitted to estimate the cost of later calculations.
Many runs append to the same file at once -- on a cluster from many nodes, on a
network file system -- so rows are written under a lock and in one write, and the
file is set aside when it grows past a size instead of growing without bound
(vasp-step#18). Rows should hold numbers that characterize the calculation
(atoms, electrons, basis or cutoff, k-points, cores, node, wall time), not its
input files.

A code step writes its row with :func:`record_task_timing` once it has run a
:class:`seamm_exec.Task` and parsed the output: the common columns (the machine
class, the task's resources, the wall time from the task manifest, the outcome)
come from here, the descriptors of the calculation from the step. The design,
the model fitted to the rows and the descriptors each code records are in
``docs/developer_guide/campaigns/2026-10-05``.
"""

from datetime import datetime, timezone
import platform
import socket
import subprocess

import csv
import io
import logging
import os
from pathlib import Path
import time

logger = logging.getLogger("seamm-exec")

#: Where the timing files are
DEFAULT_DIRECTORY = Path("~/.seamm.d/timing")
#: A file larger than this is set aside (renamed with the date) and begun again
MAX_BYTES = 50 * 1024 * 1024
#: The version of the record written by :func:`record_task_timing`
SCHEMA = 1
#: The common columns of a record, before the step's descriptors
COMMON_COLUMNS = (
    "schema",
    "date",
    "machine",
    "cluster",
    "partition",
    "cpu_model",
    "cpu_cores",
    "gpu_model",
    "host",
    "program",
    "ntasks",
    "cpus_per_task",
    "mem_per_cpu",
    "ngpus",
    "wall",
    "estimated",
    "state",
    "timed_out",
    "attempts",
    "in_situ",
)


def timing_path(program, directory=None):
    """The timing file of ``program``."""
    directory = Path(directory or DEFAULT_DIRECTORY).expanduser()
    return directory / f"{program}.csv"


def append_timing(program, row, fieldnames=None, directory=None, max_bytes=MAX_BYTES):
    """Append one timing row for ``program``, safely with concurrent writers.

    Parameters
    ----------
    program : str
        Names the file, ``<program>.csv``.
    row : dict
        The row. Values are written as text; newlines in them are replaced by
        spaces, so a row is always one line.
    fieldnames : [str], optional
        The columns, in order, for a new file. Default: the row's keys. A row is
        written with the file's existing header, missing columns left empty. A
        row with columns the header lacks sets the file aside and begins a new
        one whose header is the old columns followed by the new, so a change of
        schema never loses columns and never mixes two schemas in one file.
    directory : str or Path, optional
        Default ``~/.seamm.d/timing``.
    max_bytes : int
        Set the file aside, as ``<program>-<YYYYmmdd-HHMMSS>[-n].csv``, when it is
        larger than this.

    Returns
    -------
    pathlib.Path
        The file written.

    Notes
    -----
    The lock is a POSIX ``lockf`` lock, which network file systems honour through
    their lock daemon; on a file system whose lock daemon has hung, the call
    waits for it.
    """
    path = timing_path(program, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fieldnames or row.keys())
    clean = {
        k: ("" if v is None else str(v).replace("\r", " ").replace("\n", " "))
        for k, v in row.items()
    }
    for _ in range(10):
        with open(path, "a+", newline="") as fd:
            _lock(fd)
            try:
                # Another writer may have set the file aside while this one
                # waited for the lock: then this is the old file, not the file
                # at the path. Start again with the new one.
                try:
                    if os.fstat(fd.fileno()).st_ino != os.stat(path).st_ino:
                        continue
                except FileNotFoundError:
                    continue
                fd.seek(0, os.SEEK_END)
                if fd.tell() > max_bytes:
                    aside = _aside_name(path)
                    os.replace(path, aside)
                    logger.info(f"Set the timing file aside as {aside}")
                    continue  # and write to a new file
                if fd.tell() == 0:
                    header = fieldnames
                    text = io.StringIO()
                    csv.writer(text).writerow(header)
                    fd.write(text.getvalue())
                else:
                    fd.seek(0)
                    header = next(csv.reader([fd.readline()]), fieldnames)
                    fd.seek(0, os.SEEK_END)
                extra = [k for k in clean if k not in header]
                if extra:
                    aside = _aside_name(path)
                    os.replace(path, aside)
                    logger.info(
                        f"Set the timing file aside as {aside}: new columns {extra}"
                    )
                    fieldnames = list(header) + extra
                    continue  # and write to a new file with the wider header
                text = io.StringIO()
                csv.DictWriter(text, fieldnames=header, extrasaction="ignore").writerow(
                    clean
                )
                fd.write(text.getvalue())  # one write: one line
                fd.flush()
                os.fsync(fd.fileno())
                return path
            finally:
                _unlock(fd)
    raise RuntimeError(f"Could not write to the timing file {path}")


def _aside_name(path):
    """A name not yet used for setting ``path`` aside (it is held locked)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.stem}-{stamp}-{n}{path.suffix}")
        n += 1
    return aside


def read_timings(program, directory=None, all_files=False):
    """The rows of ``program``'s timing file, as dicts.

    ``all_files=True`` also reads the files set aside
    (``<program>-<stamp>.csv``), oldest first, as a fit does; their columns may
    differ, so a row only has the keys of its own file.
    """
    path = timing_path(program, directory)
    paths = []
    if all_files:
        paths = sorted(
            p
            for p in path.parent.glob(f"{path.stem}-*{path.suffix}")
            if p.stem[len(path.stem) + 1 :].replace("-", "").isdigit()
        )
    if path.exists():
        paths.append(path)
    rows = []
    for p in paths:
        with open(p, newline="") as fd:
            rows.extend(csv.DictReader(fd))
    return rows


# ----------------------------------------------------------------------
# The record of a task
# ----------------------------------------------------------------------
_machine = None


def machine_class(gpu_model=None):
    """The class of machine this process runs on: the columns that separate
    machines of different speed in the timing records.

    Returns
    -------
    dict
        ``machine`` -- the key, ``cluster:partition:cpu_model`` with empty parts
        omitted (and ``:gpu_model`` appended when given); plus ``cluster``,
        ``partition``, ``cpu_model``, ``cpu_cores`` (physical cores of the node),
        ``gpu_model`` and ``host``.

    The cluster and partition come from the scheduler's environment; a laptop has
    neither, and its key is just the CPU model. The result is cached per process
    (except for the GPU, which depends on the task).
    """
    global _machine
    if _machine is None:
        env = os.environ
        cluster = env.get("SLURM_CLUSTER_NAME") or env.get("PBS_SERVER") or ""
        partition = env.get("SLURM_JOB_PARTITION") or env.get("PBS_QUEUE") or ""
        cores = None
        try:
            import psutil

            cores = psutil.cpu_count(logical=False)
        except Exception:
            pass
        _machine = {
            "cluster": cluster.split(".")[0] if cluster else "",
            "partition": partition,
            "cpu_model": _cpu_model(),
            "cpu_cores": cores or os.cpu_count() or "",
            "host": socket.gethostname(),
        }
    result = dict(_machine)
    result["gpu_model"] = gpu_model or ""
    parts = [result["cluster"], result["partition"], result["cpu_model"]]
    if gpu_model:
        parts.append(gpu_model)
    result["machine"] = ":".join(p for p in parts if p)
    return result


def _cpu_model():
    """The CPU's model name, e.g. 'AMD EPYC 7702 64-Core Processor' or
    'Apple M3 Pro'."""
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return " ".join(out.stdout.split())
        elif Path("/proc/cpuinfo").exists():
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith(("model name", "hardware", "cpu model")):
                    return " ".join(line.split(":", 1)[1].split())
    except Exception as e:
        logger.debug(f"Could not get the CPU model: {e}")
    return " ".join(x for x in (platform.machine(), platform.processor()) if x)


def task_wall_seconds(result):
    """The wall time of a task's last attempt, from its history, or None."""
    history = getattr(result, "history", None) or []
    for attempt in reversed(history):
        started = attempt.get("started")
        finished = attempt.get("finished")
        if started is not None and finished is not None:
            return float(finished) - float(started)
    return None


def structure_descriptors(configuration, atom_indices=None, ghost_atoms=None):
    """The descriptors of a structure every code's record shares.

    ``n_atoms`` and ``n_heavy`` (atomic number > 1) of the atoms the code was
    given (``atom_indices``, default all; ``ghost_atoms`` excluded and counted
    as ``n_ghosts``), ``n_electrons`` (the atoms' electrons less the charge),
    ``charge``, ``multiplicity``, ``periodicity`` and, for a periodic system,
    ``volume`` (Å^3). Never raises: a missing attribute leaves its key out.
    """
    d = {}
    try:
        numbers = list(configuration.atoms.atomic_numbers)
        indices = range(len(numbers)) if atom_indices is None else atom_indices
        ghosts = set(ghost_atoms or ())
        real = [numbers[i] for i in indices if i not in ghosts]
        d["n_atoms"] = len(real)
        d["n_heavy"] = sum(1 for z in real if z > 1)
        d["n_ghosts"] = len(ghosts)
        charge = getattr(configuration, "charge", None)
        d["charge"] = charge
        d["multiplicity"] = getattr(configuration, "spin_multiplicity", None)
        if atom_indices is None and charge is not None:
            d["n_electrons"] = int(sum(real) - charge)
        periodicity = getattr(configuration, "periodicity", 0)
        d["periodicity"] = periodicity
        if periodicity:
            d["volume"] = float(configuration.volume)
    except Exception as e:
        logger.debug(f"Structure descriptors incomplete: {e}")
    return d


def record_timing(
    program,
    wall,
    descriptors=None,
    *,
    ntasks=None,
    cpus_per_task=None,
    mem_per_cpu=None,
    ngpus=0,
    gpu_model=None,
    estimated=None,
    state="finished",
    timed_out=False,
    attempts=1,
    in_situ=None,
    directory=None,
):
    """Append the timing record of a run made outside the task layer.

    For a step that still runs its code through ``executor.run`` and times it
    itself. The arguments are the common columns (see the module docstring);
    ``descriptors`` as for :func:`record_task_timing`. Never raises.
    """
    try:
        machine = machine_class(gpu_model=gpu_model or ("gpu" if ngpus else None))
        row = {
            "schema": SCHEMA,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": machine["machine"],
            "cluster": machine["cluster"],
            "partition": machine["partition"],
            "cpu_model": machine["cpu_model"],
            "cpu_cores": machine["cpu_cores"],
            "gpu_model": machine["gpu_model"],
            "host": machine["host"],
            "program": program,
            "ntasks": ntasks,
            "cpus_per_task": cpus_per_task,
            "mem_per_cpu": mem_per_cpu,
            "ngpus": ngpus,
            "wall": None if wall is None else f"{float(wall):.3f}",
            "estimated": _number(estimated),
            "state": state,
            "timed_out": int(bool(timed_out)),
            "attempts": attempts,
            "in_situ": _flag(in_situ),
        }
        for key, value in (descriptors or {}).items():
            if key not in row:
                row[key] = _number(value)
        return append_timing(program, row, directory=directory)
    except Exception as e:
        logger.warning(f"Could not record the timing of a {program} run: {e}")
        return None


def record_task_timing(task, result, descriptors=None, directory=None):
    """Append the timing record of a task that has run.

    Parameters
    ----------
    task : seamm_exec.Task
        The task as it was run: its program, resources and estimate.
    result : seamm_exec.TaskResult
        Its result: the wall time comes from the attempts' history, the outcome
        from the state. A restored result (an earlier run's, found again) is not
        recorded, and the function returns None.
    descriptors : dict, optional
        The step's description of the calculation: numbers and short categorical
        values (see the campaign document for each code's), written after the
        common columns. Keys that clash with a common column are ignored.
    directory : str or Path, optional
        The timing directory; default ``~/.seamm.d/timing``.

    Returns
    -------
    pathlib.Path or None
        The file written, or None if nothing was recorded.

    Never raises: a problem writing a timing record is logged, since it must not
    stop the calculation that produced it.
    """
    try:
        if getattr(result, "restored", False):
            return None
        resources = getattr(task, "resources", None)
        ngpus = getattr(resources, "ngpus", 0) or 0
        gpu = os.environ.get("SLURM_JOB_GPUS") or os.environ.get("CUDA_VISIBLE_DEVICES")
        return record_timing(
            getattr(task, "program", ""),
            task_wall_seconds(result),
            descriptors,
            ntasks=getattr(resources, "ntasks", None),
            cpus_per_task=getattr(resources, "cpus_per_task", None),
            mem_per_cpu=getattr(resources, "mem_per_cpu", None),
            ngpus=ngpus,
            gpu_model=(descriptors or {}).get("gpu_model")
            or ("gpu" if ngpus and gpu else None),
            estimated=getattr(task, "estimated_seconds", None),
            state=getattr(result, "state", ""),
            timed_out=getattr(result, "timed_out", False),
            attempts=getattr(result, "attempts", None),
            in_situ=getattr(result, "in_situ", None),
            directory=directory,
        )
    except Exception as e:
        logger.warning(
            f"Could not record the timing of task {getattr(task, 'key', '?')}: {e}"
        )
        return None


def _number(value):
    """Floats to a short text; bools to 0/1; anything else as is."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    return value


def _flag(value):
    return "" if value is None else int(bool(value))


def _lock(fd):
    """An exclusive POSIX lock (``lockf``, which network file systems honour
    through their lock daemon, unlike ``flock``)."""
    try:
        import fcntl

        fcntl.lockf(fd.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError) as e:
        logger.debug(f"Could not lock the timing file: {e}")


def _unlock(fd):
    try:
        import fcntl

        fcntl.lockf(fd.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError, ValueError):
        pass
