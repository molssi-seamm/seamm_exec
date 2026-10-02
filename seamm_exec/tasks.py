# -*- coding: utf-8 -*-

"""Tasks: the units of work a step hands to a back end.

A step describes each external calculation as a :class:`Task` -- the program, a
command template, the input files, the files to keep and the resources -- and
runs a group of them with a :class:`TaskSet`, which submits what is not done to a
back end (a :class:`TaskBackend`, e.g. :class:`~seamm_exec.local_pool.LocalPool`),
waits, and yields a :class:`TaskResult` for each as it finishes.

With a step directory, the ``TaskSet`` keeps ``<step dir>/tasks/manifest.json``
and writes ``<step dir>/tasks/<key>/DONE`` when a task finishes, so a rerun of
the step in the same directory never recomputes a finished task.

See ``docs/developer_guide/campaigns/2026-10-02`` for the design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import tarfile
import threading
import time
from typing import Iterator, Protocol, runtime_checkable

logger = logging.getLogger("seamm-exec")

#: The pure-Python bundle worker, run by path (``python task_worker.py bundle.json``)
WORKER_SCRIPT = Path(__file__).parent / "task_worker.py"

MANIFEST_VERSION = 1

# States
QUEUED = "queued"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
LOST = "lost"
TERMINAL_STATES = (FINISHED, FAILED, CANCELLED, LOST)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+=@,-]*$")


@dataclass
class Resources:
    """What a task needs, in scheduler-neutral terms.

    Each back end translates these: the ``LocalPool`` into a share of this
    machine or allocation, a scheduler into its directives.

    ``ntasks = None`` means "all of the back end's capacity" (for the
    ``LocalPool``, the whole machine or allocation, as a code got before tasks).
    Memory is in bytes and walltime in seconds.
    """

    ntasks: int | None = None
    cpus_per_task: int = 1
    mem_per_cpu: int | None = None
    ngpus: int = 0
    walltime: float | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    nodes: int | None = None


@dataclass
class Task:
    """One external calculation.

    Attributes
    ----------
    key : str
        Unique within the step and stable across restarts (e.g. a fragment key).
        Letters, digits and ``._+=@,-``; it names the task's directory.
    program : str
        The program's identity ("orca", "mopac", ...), which names its
        ``<root>/<program>.ini``.
    cmd : [str]
        The command template; ``{code}``, ``{code_dir}``, ``{NTASKS}``, ... are
        filled in by the back end from the configuration and the task's share of
        the resources.
    files : {str: str or bytes}
        Input files to write before running.
    return_files : [str]
        Globs of the files to keep; ``"@subdir+name"`` moves a file into a
        subdirectory, as with ``Base.run()``.
    resources : Resources
    env : {str: str}
        Extra environment variables.
    in_situ : bool or None
        True runs in the task directory, leaving output there to watch; False in
        a temporary directory, copying back only ``return_files``; None picks
        per ``Base.run()`` (scratch under a scheduler, in place otherwise).
    shell : bool
    input_data : str or None
        Data for the standard input.
    estimated_seconds : float or None
        The step's estimate of the cost, used by the inline rule for tiny tasks.
    target : str or None
        Reserved: None is the job's target.
    directory : str or Path or None
        Where the task runs and its results land. None means
        ``<step dir>/tasks/<key>/``, the default for fan-out; a step running a
        single calculation passes its own directory so its output stays where it
        always was.
    config : dict or None
        A local override of the program's configuration (the ``<program>.ini``
        section), for steps that resolve it themselves. Remote back ends ignore
        it.
    fingerprint : str or None
        Identifies the inputs for restart. None hashes ``cmd`` and ``files``;
        give one when the inputs carry run-dependent text (core counts,
        absolute paths).
    success_text : {str: str or [str]} or None
        For codes whose return code does not show failure (ORCA exits 0 after
        an error termination): each file must contain its text (or all of its
        texts), or the task failed, gets no ``DONE`` and is tried again on the
        next run.
    """

    key: str
    program: str
    cmd: list = field(default_factory=list)
    files: dict | None = None
    return_files: list = field(default_factory=list)
    resources: Resources = field(default_factory=Resources)
    env: dict = field(default_factory=dict)
    in_situ: bool | None = None
    shell: bool = False
    input_data: str | None = None
    estimated_seconds: float | None = None
    target: str | None = None
    directory: str | Path | None = None
    config: dict | None = None
    fingerprint: str | None = None
    success_text: dict | None = None

    def digest(self):
        """The fingerprint of the inputs, used to detect a changed task."""
        if self.fingerprint is not None:
            return str(self.fingerprint)
        h = hashlib.sha256()
        h.update(json.dumps(list(self.cmd)).encode())
        for name in sorted(self.files or {}):
            data = self.files[name]
            h.update(b"\0" + name.encode() + b"\0")
            h.update(data if isinstance(data, bytes) else str(data).encode())
        return "sha256:" + h.hexdigest()


@dataclass
class TaskResult:
    """The outcome of a task.

    Attributes
    ----------
    key : str
    state : str
        finished | failed | cancelled | lost
    returncode : int or None
    stdout, stderr : str
    directory : Path or None
        Where the task's results are (for an archived task, where they were).
    files : {str: str or bytes}
        The returned files' contents.
    attempts : int
        How many times the task has been submitted, over all runs.
    history : [dict]
        One record per attempt.
    in_situ : bool or None
        Whether it ran in place.
    run_directory : str or None
        Where it actually ran (a scratch directory when not in situ).
    restored : bool
        True when the result came from an earlier run rather than this one.
    reason : str or None
        Why a task failed: its return code, a failed success check, ...
    archive : Path or None
        The tar holding the task's directory, once archived.
    raw : dict or None
        The ``Base.run()``-style dictionary, for this run's results.
    """

    key: str
    state: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    directory: Path | None = None
    files: dict = field(default_factory=dict)
    attempts: int = 0
    history: list = field(default_factory=list)
    in_situ: bool | None = None
    run_directory: str | None = None
    restored: bool = False
    archive: Path | None = None
    reason: str | None = None
    raw: dict | None = None

    @property
    def ok(self):
        return self.state == FINISHED

    @classmethod
    def from_raw(cls, key, raw, directory=None):
        """Make a result from the dictionary ``Base._run_task()`` returns."""
        if raw is None:
            return cls(key=key, state=FAILED, directory=directory, raw=None)
        returncode = raw.get("returncode")
        files = {}
        for name in raw.get("files", []):
            entry = raw.get(name)
            if isinstance(entry, dict):
                files[name] = entry.get("data")
        return cls(
            key=key,
            state=FINISHED if returncode in (None, 0) else FAILED,
            returncode=returncode,
            stdout=raw.get("stdout", "") or "",
            stderr=raw.get("stderr", "") or "",
            directory=directory,
            files=files,
            in_situ=raw.get("in_situ"),
            run_directory=raw.get("directory"),
            raw=raw,
        )


@runtime_checkable
class TaskBackend(Protocol):
    """What runs tasks: the ``LocalPool`` now; a scheduler or TaskServer later.

    A back end may also offer ``wait(ids, timeout)`` to block until one of
    ``ids`` changes state, ``reattach(records) -> {key: state}`` to recover tasks
    an earlier evaluator submitted, ``capacity()`` and ``has_program(task)``.
    """

    name: str

    def submit(self, tasks: list, directories: list, on_start=None) -> list:
        """Submit tasks; return their ids. ``on_start(task, info)``, if given, is
        called when a task's process starts (``info`` is what is needed to find
        it again, e.g. its process group)."""
        ...

    def status(self, ids: list) -> dict: ...

    def cancel(self, ids: list) -> None: ...

    def fetch(self, task: Task, backend_id: str) -> TaskResult: ...


class Manifest:
    """``<step dir>/tasks/manifest.json``: what the evaluator knows of its tasks.

    One record per key: the back end, its id, the state, the fingerprint, the
    bundle, timestamps, the attempt count and history, and the archive.
    """

    def __init__(self, path, save_interval=1.0):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.records = {}
        self.save_interval = save_interval
        self._dirty = False
        self._saved_at = 0.0
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.records = data.get("tasks", {})
            except Exception:
                logger.warning(f"Could not read the task manifest {self.path}")
                bad = self.path.with_suffix(".json.bad")
                shutil.copyfile(self.path, bad)

    def get(self, key):
        return self.records.get(key)

    def update(self, key, **values):
        """Change a record. It is written by the next :meth:`flush`."""
        with self.lock:
            record = self.records.setdefault(key, {"key": key})
            record.update(values)
            self._dirty = True
            return record

    def flush(self, force=False):
        """Write the manifest if it changed, at most every ``save_interval``.

        The manifest is for reattaching and reporting; whether a task finished
        is recorded by its ``DONE`` file, which is written at once.
        """
        with self.lock:
            if not self._dirty:
                return
            if not force and _now() - self._saved_at < self.save_interval:
                return
            self.save()

    def save(self):
        with self.lock:
            self._dirty = False
            self._saved_at = _now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"version": MANIFEST_VERSION, "tasks": self.records}, indent=2
                )
            )
            os.replace(tmp, self.path)


def _now():
    return time.time()


class TaskSet:
    """What a step uses: add tasks, then iterate over their results.

    ``run()`` yields finished tasks from earlier runs first (from their ``DONE``
    markers, never recomputed), then submits the rest and yields each result as
    it completes. Within a run a task that fails (nonzero return code) is not
    retried; a lost one is, up to ``max_lost_retries``. Across runs a failed or
    lost task is tried again until it has had ``max_attempts`` attempts in all.

    Parameters
    ----------
    node : seamm.Node, optional
        The step. Gives the step directory, the executor and ``root``.
    target : str, optional
        Reserved for the job's target; unused in this version.
    directory : str or Path, optional
        The step directory, overriding ``node.directory``.
    executor : seamm_exec.Base, optional
        Overrides ``node.flowchart.executor``.
    backend : TaskBackend, optional
        Where tasks go. Default: a ``LocalPool`` for this machine.
    local : LocalPool, optional
        The evaluator's own pool, for the inline rule. Defaults to ``backend``
        when that is a ``LocalPool``, else one is made on demand.
    root : str or Path, optional
        Where the ``<program>.ini`` files are. Default ``node.global_options["root"]``.
    manifest : bool = True
        Keep the manifest and ``DONE`` markers. False only for ``Base.run()``.
    archive : bool = False
        Pack each bundle's task directories into ``tasks/<bundle>.tar`` once all
        its tasks are done, and remove the directories.
    bundle_tasks : int, optional
        Tasks per bundle, in the order added. Default: one bundle.
    max_attempts : int = 3
        Attempts per task over all runs.
    max_lost_retries : int = 2
        Resubmissions of a lost task within one run.
    inline_below : float = 60
        Tasks estimated to take less than this many seconds run in the local
        pool rather than on a remote back end, if their program is installed
        here.
    poll_interval : float
        Seconds between status checks of a back end without ``wait()``.
    """

    def __init__(
        self,
        node=None,
        target=None,
        *,
        directory=None,
        executor=None,
        backend=None,
        local=None,
        root=None,
        manifest=True,
        archive=False,
        bundle_tasks=None,
        max_attempts=3,
        max_lost_retries=2,
        inline_below=60.0,
        poll_interval=1.0,
    ):
        self.node = node
        self.target = target
        if directory is None and node is not None:
            directory = node.directory
        self.directory = Path(directory) if directory is not None else None
        if executor is None and node is not None:
            executor = node.flowchart.executor
        self.executor = executor
        if root is None and node is not None:
            try:
                root = node.global_options.get("root")
            except Exception:
                root = None
        self.root = root
        self.archive = archive
        self.bundle_tasks = bundle_tasks
        self.max_attempts = max_attempts
        self.max_lost_retries = max_lost_retries
        self.inline_below = inline_below
        self.poll_interval = poll_interval

        self._backend = backend
        self._local = local

        self.use_manifest = manifest
        if manifest and self.directory is None:
            raise ValueError("A TaskSet with a manifest needs a step directory.")
        self.manifest = None
        if manifest:
            self.manifest = Manifest(self.tasks_directory / "manifest.json")

        self.tasks = {}  # key -> Task, in the order added
        self._bundles = {}  # key -> bundle name
        self._results = {}  # key -> final TaskResult of this run

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    @property
    def tasks_directory(self):
        """``<step dir>/tasks``"""
        return self.directory / "tasks" if self.directory is not None else None

    @property
    def local(self):
        """The evaluator's own LocalPool."""
        from .local_pool import LocalPool

        if self._local is None:
            if isinstance(self._backend, LocalPool):
                self._local = self._backend
            else:
                self._local = LocalPool(self.executor, root=self.root)
        return self._local

    @property
    def backend(self):
        """The back end for this TaskSet's tasks (the job's target)."""
        if self._backend is None:
            self._backend = self.local
        return self._backend

    def add(self, task):
        """Add a task. Keys must be unique and filesystem-safe."""
        if not _KEY_RE.match(task.key):
            raise ValueError(
                f"Task key '{task.key}' must be letters, digits and ._+=@,- "
                "and start with a letter or digit."
            )
        if task.key in self.tasks:
            raise ValueError(f"Duplicate task key '{task.key}'.")
        if task.directory is not None:
            mine = Path(task.directory).resolve()
            for other in self.tasks.values():
                if (
                    other.directory is not None
                    and Path(other.directory).resolve() == mine
                ):
                    raise ValueError(
                        f"Tasks '{other.key}' and '{task.key}' share the directory "
                        f"{task.directory}."
                    )
        n = len(self.tasks)
        if self.bundle_tasks:
            bundle = f"bundle_{n // self.bundle_tasks:04d}"
        else:
            bundle = "bundle_0000"
        self.tasks[task.key] = task
        self._bundles[task.key] = bundle

    def capacity(self):
        """The back end's capacity, so a step can size its tasks beforehand.

        Returns
        -------
        dict
            ``{"cores": int, "memory": bytes, "ngpus": int}``
        """
        backend = self.backend
        if hasattr(backend, "capacity"):
            return backend.capacity()
        return self.local.capacity()

    def task_directory(self, task):
        """Where a task runs and its results land."""
        if task.directory is not None:
            return Path(task.directory)
        if self.tasks_directory is None:
            return None
        return self.tasks_directory / task.key

    def marker_directory(self, key):
        """``<step dir>/tasks/<key>``, holding the task's ``DONE``."""
        return self.tasks_directory / key

    def route(self, task):
        """The back end for a task: the job's target, except for tiny tasks.

        The inline rule: a task whose ``estimated_seconds`` is below
        ``inline_below`` runs in the evaluator's own pool instead of a remote
        back end, provided its program is installed here.
        """
        from .local_pool import LocalPool

        backend = self.backend
        if isinstance(backend, LocalPool):
            return backend
        if (
            task.estimated_seconds is not None
            and task.estimated_seconds < self.inline_below
            and self.local.has_program(task)
        ):
            return self.local
        return backend

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def run(self) -> Iterator[TaskResult]:
        """Submit what is not done, wait, and yield results as they finish."""
        if not self.use_manifest:
            yield from self._run_without_manifest()
            return

        self.tasks_directory.mkdir(parents=True, exist_ok=True)

        self._tars = {}
        pending = []
        for key, task in self.tasks.items():
            record = self.manifest.get(key)
            if (
                record is not None
                and record.get("fingerprint") is not None
                and record["fingerprint"] != task.digest()
                and record.get("attempts", 0) > 0
            ):
                # New inputs: a fresh start, keeping the old history.
                previous = list(record.get("previous", []))
                previous.append(
                    {
                        "fingerprint": record.get("fingerprint"),
                        "attempts": record.get("attempts"),
                        "history": record.get("history", []),
                    }
                )
                record = self.manifest.update(
                    key, previous=previous, attempts=0, history=[], reason=None
                )
            self.manifest.update(key, bundle=self._bundles[key])
            stored = self._restore(task, record)
            if stored is not None:
                self._results[key] = stored
                yield stored
                continue
            pending.append(task)
        self.manifest.flush(force=True)

        # Tasks an earlier evaluator left queued or running
        pending = self._reattach(pending)

        # Tasks past their attempts are reported, not run
        runnable = []
        for task in pending:
            record = self.manifest.get(task.key) or {}
            if record.get("attempts", 0) >= self.max_attempts:
                result = self._result_from_record(task, record, FAILED)
                result.reason = (
                    f"attempts exhausted: {record.get('attempts')} attempts, the "
                    f"limit is {self.max_attempts}; last: {record.get('reason')}. "
                    "Changing its input, or deleting the task's entry in "
                    f"{self.manifest.path}, allows it to run again"
                )
                result.stderr = f"Not rerun: {result.reason}.\n" + (result.stderr or "")
                self._results[task.key] = result
                yield result
            else:
                runnable.append(task)

        # Directories exist before anything runs, so that an in-situ task's
        # cleanup sees them as pre-existing.
        for task in runnable:
            self.marker_directory(task.key).mkdir(parents=True, exist_ok=True)
            directory = self.task_directory(task)
            directory.mkdir(parents=True, exist_ok=True)

        self._archive_completed_bundles()

        inflight = {}  # (backend, id) -> task
        lost_retries = {}
        try:
            self._submit(runnable, inflight)
            self.manifest.flush(force=True)
            while inflight:
                self.manifest.flush()
                changed = False
                by_backend = {}
                for (backend, backend_id), task in inflight.items():
                    by_backend.setdefault(backend, []).append(backend_id)
                for backend, ids in by_backend.items():
                    states = backend.status(ids)
                    for backend_id in ids:
                        state = states.get(backend_id, LOST)
                        task = inflight[(backend, backend_id)]
                        record = self.manifest.get(task.key) or {}
                        if state not in TERMINAL_STATES:
                            if state != record.get("state"):
                                self.manifest.update(task.key, state=state)
                            continue
                        changed = True
                        del inflight[(backend, backend_id)]
                        if state == LOST:
                            n = lost_retries.get(task.key, 0)
                            if (
                                n < self.max_lost_retries
                                and record.get("attempts", 0) < self.max_attempts
                            ):
                                lost_retries[task.key] = n + 1
                                self._finish_attempt(
                                    task.key, LOST, None, reason="lost"
                                )
                                self._submit([task], inflight)
                                continue
                        result = self._collect(task, backend, backend_id, state)
                        self._results[task.key] = result
                        yield result
                        self._archive_completed_bundles()
                if not changed and inflight:
                    self._wait(inflight)
        finally:
            if inflight:
                by_backend = {}
                for (backend, backend_id), task in inflight.items():
                    by_backend.setdefault(backend, []).append(backend_id)
                    self.manifest.update(task.key, state=CANCELLED)
                for backend, ids in by_backend.items():
                    try:
                        backend.cancel(ids)
                    except Exception:
                        logger.exception("Error cancelling tasks")
            self.manifest.flush(force=True)
            self._close_tars()

    def _run_without_manifest(self):
        """``Base.run()``: no bookkeeping, results as ``Base`` always made them."""
        for task in self.tasks.values():
            backend = self.route(task)
            directory = self.task_directory(task)
            (backend_id,) = backend.submit([task], [directory])
            states = backend.status([backend_id])
            while states.get(backend_id) not in TERMINAL_STATES:
                self._wait({(backend, backend_id): task})
                states = backend.status([backend_id])
            result = backend.fetch(task, backend_id)
            self._results[task.key] = result
            yield result

    def _submit(self, tasks, inflight):
        by_backend = {}
        for task in tasks:
            by_backend.setdefault(self.route(task), []).append(task)
        for backend, group in by_backend.items():
            now = _now()
            for task in group:
                record = self.manifest.get(task.key) or {}
                self.manifest.update(
                    task.key,
                    backend=backend.name,
                    state=QUEUED,
                    fingerprint=task.digest(),
                    attempts=record.get("attempts", 0) + 1,
                    submitted=now,
                    directory=self._relative(self.task_directory(task)),
                    pgid=None,
                    host=None,
                )
            ids = backend.submit(
                group,
                [self.task_directory(t) for t in group],
                on_start=self._on_start,
            )
            for task, backend_id in zip(group, ids):
                self.manifest.update(task.key, id=backend_id)
                inflight[(backend, backend_id)] = task

    def _on_start(self, task, info):
        """Called by a back end when a task's process starts.

        Written at once: if the evaluator dies a moment later, the next run must
        know the process to stop before it starts the task again.
        """
        self.manifest.update(task.key, state=RUNNING, started=_now(), **info)
        self.manifest.flush(force=True)

    def _wait(self, inflight):
        backends = {}
        for backend, backend_id in inflight:
            backends.setdefault(backend, []).append(backend_id)
        if len(backends) == 1:
            ((backend, ids),) = backends.items()
            if hasattr(backend, "wait"):
                backend.wait(ids, timeout=self.poll_interval * 10)
                return
        time.sleep(self.poll_interval)

    def _finish_attempt(self, key, state, returncode, reason=None):
        record = self.manifest.get(key) or {}
        history = list(record.get("history", []))
        history.append(
            {
                "attempt": record.get("attempts", 0),
                "backend": record.get("backend"),
                "id": record.get("id"),
                "state": state,
                "returncode": returncode,
                "reason": reason,
                "submitted": record.get("submitted"),
                "started": record.get("started"),
                "finished": _now(),
            }
        )
        self.manifest.update(
            key,
            state=state,
            returncode=returncode,
            reason=reason,
            finished=_now(),
            history=history,
        )
        return history

    def _collect(self, task, backend, backend_id, state):
        """Fetch a task's result, record it and mark it DONE if it finished."""
        if state == FINISHED or state == FAILED:
            result = backend.fetch(task, backend_id)
            if result.state == FINISHED:
                problem = self._check_success(task, result)
                if problem is not None:
                    result.state = FAILED
                    result.reason = f"success check: {problem}"
                    result.stderr = (
                        result.stderr or ""
                    ) + f"\nThe task failed: {problem}.\n"
            elif result.state == FAILED and result.reason is None:
                if result.returncode is None:
                    result.reason = "the task could not be run"
                else:
                    result.reason = f"return code {result.returncode}"
            state = result.state
        else:
            result = None
        if result is None:
            result = TaskResult(
                key=task.key,
                state=state,
                directory=self.task_directory(task),
                reason=state,
            )
        history = self._finish_attempt(
            task.key, state, result.returncode, reason=result.reason
        )
        record = self.manifest.get(task.key)
        result.attempts = record.get("attempts", 0)
        result.history = history
        if state == FINISHED:
            self._write_done(task, result)
        else:
            done = self.marker_directory(task.key) / "DONE"
            if done.exists():
                done.unlink()
        return result

    def _check_success(self, task, result):
        """Apply ``task.success_text``: None if it passed, else the reason."""
        if not task.success_text:
            return None
        directory = self.task_directory(task)
        for name, text in task.success_text.items():
            data = result.files.get(name)
            if data is None and directory is not None:
                path = directory / self._returned_path(name)
                data = path.read_bytes() if path.exists() else None
            if isinstance(data, bytes):
                data = data.decode(errors="replace")
            texts = [text] if isinstance(text, str) else list(text)
            if data is None:
                return f"{name} is missing"
            for text in texts:
                if text not in data:
                    return f"'{text}' is not in {name}"
        return None

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    def _write_done(self, task, result):
        """Write ``tasks/<key>/DONE``: the record needed to restore the result."""
        done = {
            "key": task.key,
            "state": FINISHED,
            "returncode": result.returncode,
            "fingerprint": task.digest(),
            "files": sorted(result.files),
            "in_situ": result.in_situ,
            "run_directory": result.run_directory,
            "directory": self._relative(self.task_directory(task)),
            "finished": _now(),
        }
        marker = self.marker_directory(task.key)
        marker.mkdir(parents=True, exist_ok=True)
        tmp = marker / "DONE.tmp"
        tmp.write_text(json.dumps(done, indent=2))
        os.replace(tmp, marker / "DONE")

    def _restore(self, task, record):
        """The stored result of a finished task, or None if it must run."""
        done = None
        archive = None
        if record is not None and record.get("archive"):
            archive = self.tasks_directory / record["archive"]
            done = self._read_from_tar(archive, f"{task.key}/DONE")
            if done is not None:
                done = json.loads(done)
        if done is None:
            path = self.marker_directory(task.key) / "DONE"
            if path.exists():
                try:
                    done = json.loads(path.read_text())
                except Exception:
                    done = None
                archive = None
        if done is None:
            return None
        if done.get("fingerprint") != task.digest():
            logger.warning(
                f"Task '{task.key}' in {self.directory} finished earlier with "
                "different inputs, so it will be rerun."
            )
            return None

        directory = self.task_directory(task)
        files = {}
        for name in done.get("files", []):
            relative = self._returned_path(name)
            if archive is not None:
                data = self._read_from_tar(archive, f"{task.key}/{relative}")
            else:
                path = directory / relative
                data = path.read_bytes() if path.exists() else None
            files[name] = _decode(data)
        stdout = stderr = ""
        for name in ("stdout.txt", "stderr.txt"):
            if archive is not None:
                data = self._read_from_tar(archive, f"{task.key}/{name}")
            else:
                path = directory / name
                data = path.read_bytes() if path.exists() else None
            text = _decode(data) if data is not None else ""
            if isinstance(text, bytes):
                text = ""
            if name == "stdout.txt":
                stdout = text
            else:
                stderr = text
        record = record or {}
        return TaskResult(
            key=task.key,
            state=FINISHED,
            returncode=done.get("returncode"),
            stdout=stdout,
            stderr=stderr,
            directory=directory,
            files=files,
            attempts=record.get("attempts", 0),
            history=record.get("history", []),
            in_situ=done.get("in_situ"),
            run_directory=done.get("run_directory"),
            restored=True,
            archive=archive,
        )

    def _reattach(self, pending):
        """Recover tasks an earlier evaluator left queued or running.

        Back ends that can (a scheduler) report them still queued or running;
        the ``LocalPool`` cannot adopt another process's children, so it kills
        any it finds and reports them lost. Lost tasks are rerun. In this
        version every task still in flight is resubmitted.
        """
        by_backend = {}
        for task in pending:
            record = self.manifest.get(task.key)
            if record is None or record.get("state") not in (QUEUED, RUNNING):
                continue
            backend = self._backend_named(record.get("backend"))
            if backend is None or not hasattr(backend, "reattach"):
                continue
            by_backend.setdefault(backend, []).append(record)
        for backend, records in by_backend.items():
            states = backend.reattach(records)
            for key, state in states.items():
                if state == LOST:
                    self._finish_attempt(
                        key, LOST, None, reason="the evaluator stopped while it ran"
                    )
        return pending

    def _backend_named(self, name):
        if name is None:
            return None
        if self._backend is not None and self._backend.name == name:
            return self._backend
        if name == "local":
            return self.local
        return None

    def _result_from_record(self, task, record, state):
        return TaskResult(
            key=task.key,
            state=state,
            returncode=record.get("returncode"),
            directory=self.task_directory(task),
            attempts=record.get("attempts", 0),
            history=record.get("history", []),
        )

    # ------------------------------------------------------------------
    # Archiving
    # ------------------------------------------------------------------
    def _archive_completed_bundles(self):
        """Tar each completed bundle's task directories, if archiving."""
        if not self.archive:
            return
        bundles = {}
        for key, bundle in self._bundles.items():
            bundles.setdefault(bundle, []).append(key)
        for bundle, keys in bundles.items():
            if not all(k in self._results for k in keys):
                continue
            # Only directories under tasks/ are archived; a task given its own
            # directory keeps it.
            # Failed tasks stay as directories: they may run again.
            to_pack = [
                k
                for k in keys
                if self.tasks[k].directory is None
                and self._results[k].ok
                and not self._results[k].restored
                and (self.tasks_directory / k).is_dir()
            ]
            if not to_pack:
                continue
            tar_path = self.tasks_directory / f"{bundle}.tar"
            self._close_tars(tar_path)
            with tarfile.open(tar_path, "a" if tar_path.exists() else "w") as tar:
                for k in to_pack:
                    tar.add(self.tasks_directory / k, arcname=k)
            for k in to_pack:
                shutil.rmtree(self.tasks_directory / k)
                self.manifest.update(k, archive=tar_path.name)
                self._results[k].archive = tar_path

    def _read_from_tar(self, path, member):
        """A member of an archive, opening each archive once per run."""
        path = Path(path)
        tars = getattr(self, "_tars", None)
        if tars is None:
            tars = self._tars = {}
        if path not in tars:
            if not path.exists():
                return None
            try:
                tar = tarfile.open(path, "r")
                # The last copy of a name wins, as on extraction.
                index = {info.name: info for info in tar.getmembers()}
            except tarfile.TarError:
                return None
            tars[path] = (tar, index)
        tar, index = tars[path]
        info = index.get(member)
        if info is None:
            return None
        fd = tar.extractfile(info)
        return fd.read() if fd is not None else None

    def _close_tars(self, path=None):
        """Close the cached archives (or just ``path``, before it is changed)."""
        tars = getattr(self, "_tars", None) or {}
        for p in [path] if path is not None else list(tars):
            entry = tars.pop(Path(p), None) if p is not None else None
            if entry is not None:
                entry[0].close()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _returned_path(name):
        """Where a returned file lands: ``@subdir+name`` is ``subdir/name``."""
        if name.startswith("@") and "+" in name:
            subdir, fname = name[1:].split("+", 1)
            return f"{subdir}/{fname}"
        return name

    def _relative(self, path):
        if path is None:
            return None
        try:
            return str(Path(path).relative_to(self.directory))
        except ValueError:
            return str(path)

    def summary(self):
        """Counts by state, for the Dashboard and the step's report.

        Returns
        -------
        dict
            ``{"total": n, "finished": n, "failed": n, ...}``
        """
        counts = {"total": len(self.tasks)}
        for key in self.tasks:
            if key in self._results:
                state = self._results[key].state
            elif self.manifest is not None and self.manifest.get(key):
                state = self.manifest.get(key).get("state", QUEUED)
            else:
                state = "new"
            counts[state] = counts.get(state, 0) + 1
        return counts


def run_task(task, node=None, **kwargs):
    """Run one task through a :class:`TaskSet` and return its result.

    The convenience for a step that runs a single calculation: it gets the
    manifest, restart and the pool's handling of the process like any other
    task. ``kwargs`` go to :class:`TaskSet` (e.g. ``directory=`` when the task
    runs somewhere other than ``node.directory``).

    Returns
    -------
    TaskResult
    """
    task_set = TaskSet(node, **kwargs)
    task_set.add(task)
    results = list(task_set.run())
    return results[0]


def _decode(data):
    """Text if the bytes decode as UTF-8, else the bytes (as Base.run reads)."""
    if data is None:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data


def this_host():
    return socket.gethostname()


__all__ = [
    "Resources",
    "Task",
    "TaskResult",
    "TaskBackend",
    "TaskSet",
    "Manifest",
    "run_task",
    "WORKER_SCRIPT",
]
