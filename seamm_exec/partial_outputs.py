# -*- coding: utf-8 -*-

"""Copies of a running task's outputs in its task directory.

A task that runs in a temporary directory -- node-local scratch under a scheduler
-- has its returned files put in place only when it finishes. One stopped before
then, by the job's time limit or a cancel, loses them with the scratch: ten large
ORCA calculations that timed out after 11-21 h left no ``orca.out`` to tell where
the time went (MBE, 2026-10-23).

So while such a task runs, its returned files are copied to the task directory
every :data:`DEFAULT_INTERVAL` seconds as ``<name>.partial``, and once more when
the evaluator is told to stop (SIGTERM, see ``local_pool._on_signal``). A task
that finishes puts its files in place as before and removes the copies. Large
files (over :data:`MAX_BYTES`, e.g. MD trajectories) are not copied.
"""

import logging
import os
from pathlib import Path
import shutil
import threading

logger = logging.getLogger(__name__)

#: Seconds between copies; the environment variable SEAMM_PARTIAL_INTERVAL
#: overrides it, and 0 turns the periodic copies off (the copy on SIGTERM stays).
DEFAULT_INTERVAL = 600.0
#: Files larger than this are not copied
MAX_BYTES = 50_000_000
#: Appended to the name of each copy
SUFFIX = ".partial"

_active = set()
_lock = threading.Lock()


def interval():
    """Seconds between the copies, from SEAMM_PARTIAL_INTERVAL or the default."""
    text = os.environ.get("SEAMM_PARTIAL_INTERVAL")
    if text is None:
        return DEFAULT_INTERVAL
    try:
        return max(0.0, float(text))
    except ValueError:
        return DEFAULT_INTERVAL


class Copier:
    """Copy a running task's returned files to its task directory as they grow.

    Parameters
    ----------
    source : str or pathlib.Path
        Where the task runs.
    destination : str or pathlib.Path
        The task directory.
    patterns : [str]
        The task's returned files (glob patterns).
    every : float or None
        Seconds between copies; None for :func:`interval`, 0 for none.
    max_bytes : int
        Larger files are not copied.
    """

    def __init__(self, source, destination, patterns, every=None, max_bytes=MAX_BYTES):
        self.source = Path(source)
        self.destination = Path(destination)
        self.patterns = list(patterns)
        self.every = interval() if every is None else every
        self.max_bytes = max_bytes
        self._copied = {}  # name: (size, mtime) of the last copy
        self._stop = threading.Event()
        self._thread = None
        self._copy_lock = threading.Lock()

    def copy(self):
        """Copy the files that changed since the last copy; never raises."""
        with self._copy_lock:
            for pattern in self.patterns:
                try:
                    paths = sorted(self.source.glob(pattern))
                except Exception:
                    continue
                for path in paths:
                    try:
                        if not path.is_file():
                            continue
                        stat = path.stat()
                        if stat.st_size > self.max_bytes:
                            continue
                        state = (stat.st_size, stat.st_mtime)
                        if self._copied.get(path.name) == state:
                            continue
                        target = self.destination / (path.name + SUFFIX)
                        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
                        shutil.copyfile(path, tmp)
                        os.replace(tmp, target)
                        self._copied[path.name] = state
                    except Exception as e:
                        logger.debug(
                            f"Could not copy {path} to the task directory: {e}"
                        )

    def start(self):
        """Copy periodically until :meth:`stop`, and on :func:`copy_all`."""
        with _lock:
            _active.add(self)
        if self.every > 0:
            self._thread = threading.Thread(
                target=self._run, name="seamm-partial-outputs", daemon=True
            )
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.every):
            self.copy()

    def stop(self):
        """Stop copying; the copies stay (see :meth:`remove`)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with _lock:
            _active.discard(self)

    def remove(self):
        """Remove the copies, once the task's own files are in place."""
        for name in list(self._copied):
            try:
                (self.destination / (name + SUFFIX)).unlink()
            except OSError:
                pass
        self._copied.clear()


def copy_all():
    """Copy the outputs of every running task now, e.g. when told to stop."""
    with _lock:
        copiers = list(_active)
    for copier in copiers:
        copier.copy()
