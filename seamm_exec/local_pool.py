# -*- coding: utf-8 -*-

"""LocalPool: run tasks concurrently on this machine or in this allocation.

The pool's capacity is the computational environment -- the whole machine, or
the SLURM allocation the evaluator runs in -- and each task gets a share of it
from its :class:`~seamm_exec.tasks.Resources`. Tasks start, in the order
submitted, as soon as enough cores and memory are free; a task larger than the
pool is clamped to it and runs alone.

Each task runs through the executor's ``_run_task()`` (the body of the original
``Base.run()``), so the conda, modules and docker handling, ``in_situ`` and the
return-file contract are exactly those of ``Base.run()``.
"""

from __future__ import annotations

import configparser
import itertools
import logging
import os
from pathlib import Path
import signal
import socket
import threading
import time
import traceback

from .computational_environment import computational_environment
from .tasks import (
    CANCELLED,
    FAILED,
    FINISHED,
    LOST,
    QUEUED,
    RUNNING,
    TERMINAL_STATES,
    TaskResult,
)

logger = logging.getLogger("seamm-exec")

# Set for concurrent MPI tasks so that independent mpiruns don't all bind their
# ranks to the same low-numbered cores (see orca_step's _mpi_env).
BINDING_ENV = "OMPI_MCA_hwloc_base_binding_policy"


class _Job:
    """The pool's record of one submitted task."""

    def __init__(self, backend_id, task, directory):
        self.id = backend_id
        self.task = task
        self.directory = directory
        self.state = QUEUED
        self.cores = 1
        self.memory = 0
        self.ce = None
        self.raw = None
        self.error = None
        self.process = None
        self.thread = None


class _Hooks:
    """Passed to the executor's ``exec()`` through its thread-local context."""

    def __init__(self, pool, job):
        self.pool = pool
        self.job = job

    def started(self, process):
        self.pool._started(self.job, process)

    def finished(self, process):
        self.pool._process_finished(self.job, process)


class LocalPool:
    """A back end that runs tasks as local subprocesses.

    Parameters
    ----------
    executor : seamm_exec.Base
        Provides ``_run_task()`` and ``exec()``.
    root : str or Path, optional
        Where the ``<program>.ini`` files are, for tasks without ``config``.
    ce : dict, optional
        The computational environment to share out. Default:
        ``computational_environment()``.
    synchronous : bool = False
        Run each task in ``submit()``, in the caller's thread, with the given
        ``ce`` unchanged, the environment untouched and exceptions propagated:
        exactly the original ``Base.run()``.
    max_concurrent : int, optional
        At most this many tasks at once.
    """

    name = "local"

    def __init__(
        self, executor, *, root=None, ce=None, synchronous=False, max_concurrent=None
    ):
        self.executor = executor
        self.root = Path(root).expanduser() if root is not None else None
        self.synchronous = synchronous
        self.max_concurrent = max_concurrent
        self.on_start = None  # callback(task, info), set by the TaskSet

        if synchronous:
            self.ce = ce if ce is not None else {}
        else:
            self.ce = dict(ce) if ce else computational_environment()
        self.cores = max(1, int(self.ce.get("NTASKS", 1) or 1))
        self.memory = int(self.ce.get("MEM_PER_NODE", 0) or 0)
        self.ngpus = int(self.ce.get("NGPUS", 0) or 0)

        self._jobs = {}
        self._queue = []
        self._free_cores = self.cores
        self._free_memory = self.memory
        self._running = 0
        self._ids = itertools.count(1)
        self._condition = threading.Condition()
        self._host = socket.gethostname()

    # ------------------------------------------------------------------
    # Capacity and programs
    # ------------------------------------------------------------------
    def capacity(self):
        """The cores, memory (bytes) and GPUs this pool shares out."""
        return {"cores": self.cores, "memory": self.memory, "ngpus": self.ngpus}

    def has_program(self, task):
        """Whether the task's program can run here."""
        if task.config is not None:
            return True
        return self._read_config(task.program) is not None

    def config_for(self, task):
        """The program's configuration: ``task.config``, else ``<program>.ini``.

        Adds ``code_dir``, the directory holding ``code``, for commands that run
        a code's companion programs (e.g. ORCA's ``orca_2aim``).
        """
        if task.config is not None:
            config = dict(task.config)
        else:
            config = self._read_config(task.program)
            if config is None:
                raise RuntimeError(
                    f"No configuration for '{task.program}': the task has no config "
                    f"and there is no [{self.executor.name}] section in "
                    f"{self._ini_path(task.program)}."
                )
        if "code_dir" not in config and config.get("code"):
            config["code_dir"] = str(Path(config["code"]).expanduser().parent)
        return config

    def _ini_path(self, program):
        if self.root is None:
            return None
        return self.root / f"{program}.ini"

    def _read_config(self, program):
        path = self._ini_path(program)
        if path is None or not path.exists():
            return None
        full_config = configparser.ConfigParser()
        full_config.read(path)
        section = self.executor.name
        if section not in full_config:
            return None
        return dict(full_config.items(section))

    # ------------------------------------------------------------------
    # The TaskBackend interface
    # ------------------------------------------------------------------
    def submit(self, tasks, directories):
        ids = []
        with self._condition:
            for task, directory in zip(tasks, directories):
                backend_id = f"{self._host}:{os.getpid()}:{next(self._ids)}"
                job = _Job(backend_id, task, directory)
                job.cores, job.memory, job.ce = self._share(task)
                self._jobs[backend_id] = job
                self._queue.append(job)
                ids.append(backend_id)
        if not self.synchronous:
            # signal.signal works only in the main thread, which submits.
            _install_handlers()
        if self.synchronous:
            for backend_id in ids:
                job = self._jobs[backend_id]
                self._queue.remove(job)
                self._run_synchronously(job)
        else:
            self._dispatch()
        return ids

    def status(self, ids):
        with self._condition:
            return {i: self._jobs[i].state if i in self._jobs else LOST for i in ids}

    def wait(self, ids, timeout=None):
        """Block until one of ``ids`` is finished, or ``timeout`` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if any(
                    self._jobs[i].state in TERMINAL_STATES
                    for i in ids
                    if i in self._jobs
                ):
                    return
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                self._condition.wait(remaining)

    def cancel(self, ids):
        to_kill = []
        with self._condition:
            for i in ids:
                job = self._jobs.get(i)
                if job is None or job.state in TERMINAL_STATES:
                    continue
                if job in self._queue:
                    self._queue.remove(job)
                elif job.process is not None:
                    to_kill.append(job.process.pid)
                # A job started but without a process yet is killed when its
                # process starts (see _started).
                job.state = CANCELLED
            self._condition.notify_all()
        for pgid in to_kill:
            _kill_group(pgid)

    def fetch(self, task, backend_id):
        job = self._jobs[backend_id]
        if job.state == CANCELLED:
            return TaskResult(key=task.key, state=CANCELLED, directory=job.directory)
        if job.error is not None:
            return TaskResult(
                key=task.key, state=FAILED, stderr=job.error, directory=job.directory
            )
        return TaskResult.from_raw(task.key, job.raw, directory=job.directory)

    def reattach(self, records):
        """Tasks from an earlier evaluator: kill any still running here; lost.

        A process that is not our child cannot be waited for, so instead of
        adopting it the pool kills its process group and the task is rerun.
        """
        states = {}
        for record in records:
            pgid = record.get("pgid")
            if pgid and record.get("host") == self._host and _alive(pgid):
                logger.warning(
                    f"Killing the leftover process group {pgid} of task "
                    f"'{record.get('key')}' from an earlier run."
                )
                _kill_group(pgid)
            states[record["key"]] = LOST
        return states

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _share(self, task):
        """The cores, memory and computational environment for a task."""
        if self.synchronous:
            return 1, 0, self.ce
        r = task.resources
        cpus_per_task = max(1, int(r.cpus_per_task or 1))
        if r.ntasks is None:
            ntasks = max(1, self.cores // cpus_per_task)
        else:
            ntasks = max(1, int(r.ntasks))
        cores = ntasks * cpus_per_task
        if cores > self.cores:
            # Clamp to the pool; it will run alone.
            ntasks = max(1, self.cores // cpus_per_task)
            cpus_per_task = min(cpus_per_task, self.cores)
            cores = min(self.cores, ntasks * cpus_per_task)
        if r.mem_per_cpu is not None:
            mem_per_cpu = int(r.mem_per_cpu)
        else:
            mem_per_cpu = self.memory // self.cores if self.cores else 0
        memory = min(mem_per_cpu * cores, self.memory) if self.memory else 0

        ce = dict(self.ce)
        ce["NTASKS"] = ntasks
        ce["CPUS_PER_TASK"] = cpus_per_task
        ce["MEM_PER_CPU"] = mem_per_cpu
        ce["MEM_PER_NODE"] = mem_per_cpu * cores
        if r.ngpus:
            ce["NGPUS"] = r.ngpus
        else:
            ce.pop("NGPUS", None)
        return cores, memory, ce

    def _fits(self, job):
        if self.max_concurrent is not None and self._running >= self.max_concurrent:
            return False
        if self._running == 0:
            return True  # an oversized task runs alone
        return job.cores <= self._free_cores and job.memory <= self._free_memory

    def _dispatch(self):
        with self._condition:
            while self._queue and self._fits(self._queue[0]):
                job = self._queue.pop(0)
                job.state = RUNNING
                self._running += 1
                self._free_cores -= job.cores
                self._free_memory -= job.memory
                job.thread = threading.Thread(
                    target=self._run_threaded,
                    args=(job,),
                    name=f"seamm-task-{job.task.key}",
                    daemon=True,
                )
                job.thread.start()

    def _release(self, job):
        with self._condition:
            self._running -= 1
            self._free_cores += job.cores
            self._free_memory += job.memory
            self._condition.notify_all()
        self._dispatch()

    # ------------------------------------------------------------------
    # Running one task
    # ------------------------------------------------------------------
    def _arguments(self, job, concurrent):
        task = job.task
        if self.synchronous:
            config = task.config
            env = task.env
        else:
            config = self.config_for(task)
            env = dict(task.env)
            env.setdefault("OMP_NUM_THREADS", str(job.ce["CPUS_PER_TASK"]))
            if concurrent and BINDING_ENV not in env:
                env[BINDING_ENV] = "none"
        return dict(
            config=config,
            cmd=task.cmd,
            directory=job.directory,
            input_data=task.input_data,
            files=task.files,
            env=env,
            return_files=task.return_files,
            shell=task.shell,
            in_situ=task.in_situ,
            ce=job.ce,
        )

    def _run_synchronously(self, job):
        """The original ``Base.run()``: in this thread, exceptions propagate."""
        job.state = RUNNING
        try:
            job.raw = self.executor._run_task(**self._arguments(job, False))
        except BaseException:
            job.state = FAILED
            raise
        job.state = _state_of(job.raw)

    def _run_threaded(self, job):
        concurrent = len(self._jobs) > 1
        context = self.executor._task_context
        context.hooks = _Hooks(self, job)
        # A task run in place in the step directory must not prune the tasks/
        # bookkeeping that other tasks write while it runs.
        keep = [] if job.directory is None else [Path(job.directory) / "tasks"]
        try:
            job.raw = self.executor._run_task(
                **self._arguments(job, concurrent), set_umask=False, keep=keep
            )
            with self._condition:
                if job.state != CANCELLED:
                    job.state = _state_of(job.raw)
        except Exception:
            job.error = traceback.format_exc()
            logger.error(f"Task '{job.task.key}' raised an exception:\n{job.error}")
            with self._condition:
                if job.state != CANCELLED:
                    job.state = FAILED
        finally:
            context.hooks = None
            self._release(job)

    def _started(self, job, process):
        _register(process.pid)
        with self._condition:
            job.process = process
            cancelled = job.state == CANCELLED
        if cancelled:
            _kill_group(process.pid)
            return
        if self.on_start is not None:
            try:
                self.on_start(job.task, {"pgid": process.pid, "host": self._host})
            except Exception:
                logger.exception("Error in the task-start callback")

    def _process_finished(self, job, process):
        _unregister(process.pid)


def _state_of(raw):
    if raw is None:
        return FAILED
    returncode = raw.get("returncode")
    return FINISHED if returncode in (None, 0) else FAILED


# ----------------------------------------------------------------------
# Killing the tasks with the evaluator
#
# Tasks run in their own sessions so they can be killed as a group, which also
# means a signal to the evaluator's process group no longer reaches them. While
# any are running, SIGTERM and SIGHUP to the evaluator first kill them, then do
# what they did before. (A SIGKILL cannot be caught; the next run of the step
# kills the leftovers it finds in the manifest.)
# ----------------------------------------------------------------------
_live_groups = set()
_live_lock = threading.Lock()
_previous_handlers = {}


def _register(pgid):
    with _live_lock:
        _live_groups.add(pgid)


def _unregister(pgid):
    with _live_lock:
        _live_groups.discard(pgid)


def _install_handlers():
    """Install the signal handlers, if we can (main thread only)."""
    if _previous_handlers:
        return
    if threading.current_thread() is not threading.main_thread():
        return
    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            _previous_handlers[signum] = signal.signal(signum, _on_signal)
        except (ValueError, OSError):
            pass


def _on_signal(signum, frame):
    with _live_lock:
        groups = list(_live_groups)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    previous = _previous_handlers.get(signum, signal.SIG_DFL)
    if callable(previous):
        previous(signum, frame)
    elif previous == signal.SIG_IGN:
        return
    else:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _kill_group(pgid, grace=5.0):
    """SIGTERM the process group, then SIGKILL it if it is still there."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pgid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
