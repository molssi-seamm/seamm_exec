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

import psutil

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
        self.on_start = None
        self.concurrent = False


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
    resolve_programs : bool = False
        Kept for compatibility; it changes nothing now. A task without
        ``config`` is always configured here, from ``<root>/<program>.ini`` and
        the program's resolver (see :mod:`seamm_exec.resolve`); a task with
        ``config`` is run with it exactly as given.
    """

    name = "local"

    def __init__(
        self,
        executor,
        *,
        root=None,
        ce=None,
        synchronous=False,
        max_concurrent=None,
        resolve_programs=False,
    ):
        self.executor = executor
        self.resolve_programs = resolve_programs
        self.root = Path(root).expanduser() if root is not None else None
        self.synchronous = synchronous
        self.max_concurrent = max_concurrent

        if synchronous:
            # Base.run(): the caller's ce goes to the code untouched and is
            # never interpreted here.
            self.ce = ce if ce is not None else {}
            self.cores = 1
            self.memory = 0
            self.ngpus = 0
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
        if self._read_config(task.program) is not None:
            return True
        from .resolve import available

        return available(task.program, self.root)

    def config_for(self, task):
        """The program's configuration: ``task.config``, else ``<program>.ini``.

        Adds ``code_dir``, the directory holding ``code``, for commands that run
        a code's companion programs (e.g. ORCA's ``orca_2aim``), when ``code``
        is a path rather than a bare name.
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
        return _with_code_dir(config)

    def _configure(self, task, ce):
        """``(config, cmd, env)`` for a task, resolved on this machine.

        A task with ``config`` -- a step that configured its program itself, and
        every unconverted plug-in -- keeps it, its command and its environment
        exactly. Otherwise the configuration is the ``[<executor>]`` section of
        ``<root>/<program>.ini`` (if any), passed through the program's resolver
        (if it has one) with the task's share of the machine, ``ce``.
        """
        if task.config is not None:
            return self.config_for(task), task.cmd, dict(task.env)
        from .resolve import has_resolver, resolve

        config = self._read_config(task.program)
        cmd, env = list(task.cmd), dict(task.env)
        if has_resolver(task.program):
            config, cmd, env = resolve(
                task.program, config or {}, cmd, env, ce, self.root
            )
        elif config is None:
            raise RuntimeError(
                f"No configuration for '{task.program}': the task has no config, "
                f"there is no [{self.executor.name}] section in "
                f"{self._ini_path(task.program)}, and no resolver for "
                f"'{task.program}' is installed in this Python (the plug-in that "
                "provides it must be installed where the task runs)."
            )
        return _with_code_dir(config), cmd, env

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
    def submit(self, tasks, directories, on_start=None):
        ids = []
        with self._condition:
            for task, directory in zip(tasks, directories):
                backend_id = f"{self._host}:{os.getpid()}:{next(self._ids)}"
                job = _Job(backend_id, task, directory)
                job.on_start = on_start
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
        with self._condition:
            job = self._jobs.pop(backend_id)
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
        adopting it the pool kills its process group and the task is rerun. A
        pid is reused once its process has gone, so the group is killed only if
        its leader is the process the manifest recorded: same start time and
        working directory. (The host name check means a laptop that changed
        networks leaves the leftovers alone, the safe direction.)
        """
        states = {}
        for record in records:
            pgid = record.get("pgid")
            if (
                pgid
                and record.get("host") == self._host
                and _alive(pgid)
                and _same_process(pgid, record)
            ):
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
                # Concurrent if any other task is queued or running now: it may
                # share the machine with this one for some or all of its run.
                # A task that takes the whole pool never shares it.
                job.concurrent = (
                    job.cores < self.cores
                    and sum(
                        1 for j in self._jobs.values() if j.state in (QUEUED, RUNNING)
                    )
                    > 1
                )
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
        cmd = task.cmd
        if self.synchronous:
            config = task.config
            env = task.env
        else:
            config, cmd, env = self._configure(task, job.ce)
            if concurrent:
                # Keep concurrent tasks off each other's cores. A lone task
                # gets the environment it always had.
                env.setdefault("OMP_NUM_THREADS", str(job.ce["CPUS_PER_TASK"]))
                env.setdefault(BINDING_ENV, "none")
        return dict(
            config=config,
            cmd=cmd,
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
        context = None
        try:
            concurrent = job.concurrent
            context = getattr(self.executor, "_task_context", None)
            if context is not None:
                context.hooks = _Hooks(self, job)
            # A task run in place in the step directory must not prune the
            # tasks/ bookkeeping that other tasks write while it runs.
            keep = [] if job.directory is None else [Path(job.directory) / "tasks"]
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
            # Whatever happened, free the slot so the TaskSet is not left
            # waiting on a task that will never finish.
            if context is not None:
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
        if job.on_start is not None:
            info = {"pgid": process.pid, "host": self._host}
            try:
                leader = psutil.Process(process.pid)
                info["create_time"] = leader.create_time()
                info["cwd"] = leader.cwd()
            except (psutil.Error, OSError):
                pass
            try:
                job.on_start(job.task, info)
            except Exception:
                logger.exception("Error in the task-start callback")

    def _process_finished(self, job, process):
        _unregister(process.pid)


def _with_code_dir(config):
    """Add ``code_dir``, the directory holding ``code``, when ``code`` is a path.

    A bare name is found on the PATH (in a conda environment, a container, ...),
    and so are its companions, so a command must then name them bare too.
    """
    config = dict(config)
    if "code_dir" not in config and config.get("code"):
        code = Path(config["code"]).expanduser()
        if code.parent != Path("."):
            config["code_dir"] = str(code.parent)
    return config


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
# what they did before. A signal the evaluator ignores (SIGHUP under nohup) is
# left ignored. (A SIGKILL cannot be caught; the next run of the step kills the
# leftovers it finds in the manifest.)
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
            if signal.getsignal(signum) == signal.SIG_IGN:
                # e.g. SIGHUP under nohup: the evaluator, and so its tasks,
                # are meant to survive it.
                _previous_handlers[signum] = signal.SIG_IGN
                continue
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


def _same_process(pgid, record):
    """Whether process ``pgid`` is still the task's, not a later reuse of the pid."""
    try:
        leader = psutil.Process(pgid)
        if os.getpgid(pgid) != pgid:
            return False
        created = record.get("create_time")
        if created is None or abs(leader.create_time() - created) > 1.0:
            return False
        cwd = record.get("cwd")
        if cwd is not None and leader.cwd() != cwd:
            return False
    except (psutil.Error, OSError):
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
