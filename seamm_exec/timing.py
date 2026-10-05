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
"""

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
        written with the file's existing header; keys it does not have are
        dropped (with a warning once per call), missing ones left empty.
    directory : str or Path, optional
        Default ``~/.seamm.d/timing``.
    max_bytes : int
        Set the file aside, as ``<program>-<YYYYmmdd-HHMMSS>.csv``, when it is
        larger than this.

    Returns
    -------
    pathlib.Path
        The file written.
    """
    path = timing_path(program, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fieldnames or row.keys())
    clean = {
        k: ("" if v is None else str(v).replace("\r", " ").replace("\n", " "))
        for k, v in row.items()
    }
    with open(path, "a+", newline="") as fd:
        _lock(fd)
        try:
            fd.seek(0, os.SEEK_END)
            if fd.tell() > max_bytes:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                aside = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
                os.replace(path, aside)
                logger.info(f"Set the timing file aside as {aside}")
                _unlock(fd)
                fd.close()
                return append_timing(program, row, fieldnames, directory, max_bytes)
            if fd.tell() == 0:
                header = fieldnames
                text = io.StringIO()
                csv.writer(text).writerow(header)
                fd.write(text.getvalue())
            else:
                fd.seek(0)
                header = next(csv.reader([fd.readline()]), fieldnames)
                fd.seek(0, os.SEEK_END)
            extra = set(clean) - set(header)
            if extra:
                logger.warning(
                    f"Timing file {path} has no columns {sorted(extra)}; not written."
                )
            text = io.StringIO()
            csv.DictWriter(text, fieldnames=header, extrasaction="ignore").writerow(
                clean
            )
            fd.write(text.getvalue())  # one write: one line
            fd.flush()
            os.fsync(fd.fileno())
        finally:
            if not fd.closed:
                _unlock(fd)
    return path


def read_timings(program, directory=None):
    """The rows of ``program``'s timing file, as dicts (the current file only)."""
    path = timing_path(program, directory)
    if not path.exists():
        return []
    with open(path, newline="") as fd:
        return list(csv.DictReader(fd))


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
