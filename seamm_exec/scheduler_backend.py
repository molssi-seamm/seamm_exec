# -*- coding: utf-8 -*-

"""SchedulerBackend: run tasks as batch jobs of a queueing system.

Each call to :meth:`SchedulerBackend.submit` is one *bundle*: one batch job
whose allocation runs the bundle's tasks through the task worker
(``python -m seamm_exec.task_worker bundle.json``) in SEAMM mode, that is,
through a ``LocalPool`` inside the allocation, with each program resolved from
the ``<program>.ini`` files of the machine it runs on. The ``TaskSet`` decides
the bundles (``bundle_tasks``, ``bundle_walltime``) and how many it keeps in
the queue (:meth:`room`).

Files: a bundle lives in ``<step dir>/tasks/_bundles/<bundle>.<n>/``
(``bundle.json``, ``run.sh`` and the scheduler's log), the tasks in their own
directories, and the worker writes each task's ``DONE`` or ``FAILED`` in
``<step dir>/tasks/<key>/``. When the cluster does not share the evaluator's
filesystem the directories are pushed before submission and pulled back once
the bundle's job has ended, to ``<remote_root>/<job>/...``, the same relative
paths under the remote job directory.

Backend ids are ``<job id>#<bundle>.<n>#<key>``, so a restarted evaluator can
find a task's job, bundle and files from its manifest record alone
(:meth:`adopt`) and poll it rather than submit it again.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import sys
import threading
import time

from .tasks import FAILED, FINISHED, LOST, QUEUED, RUNNING, TaskResult


class _SharedCount:
    """The user's job count on one queue system, shared by every back end in
    this process that submits there.

    ``count_jobs`` counts all of the user's jobs on the scheduler, whatever
    section submitted them, so back ends for different sections of one cluster
    -- or several TaskSets running at once in one evaluator (an MBE step's
    levels) -- must share one count: each refreshing its own cache let N of
    them see the same room and overshoot ``max_queued_tasks`` N-fold.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.count = None
        self.counted_at = 0.0

    def current(self, refresh, poll_interval):
        """The count, refreshed by ``refresh()`` when older than
        ``poll_interval``; None when it cannot be known."""
        with self.lock:
            now = time.time()
            if self.count is None or now - self.counted_at > poll_interval:
                count = refresh()
                if count is None:
                    return None
                self.count = count
                self.counted_at = now
            return self.count

    def bump(self, n=1):
        with self.lock:
            if self.count is not None:
                self.count += n


_shared_counts = {}
_shared_counts_lock = threading.Lock()


def shared_count_for(queue):
    """The :class:`_SharedCount` of the queue system ``queue`` submits to: the
    scheduler's kind on the transport's host (``local`` for this machine)."""
    scheduler = getattr(queue, "scheduler", None)
    transport = getattr(queue, "transport", None)
    key = (
        type(scheduler).__name__ if scheduler is not None else type(queue).__name__,
        getattr(transport, "host", None) or "local",
    )
    with _shared_counts_lock:
        if key not in _shared_counts:
            _shared_counts[key] = _SharedCount()
        return _shared_counts[key]


logger = logging.getLogger("seamm-exec")

#: sbatch's message when a QOS's per-user submit limit is reached
_QUEUE_FULL = re.compile(
    r"QOSMaxSubmitJob|AssocMaxSubmitJob|MaxSubmitJobs|job submit limit|"
    r"would exceed.*(queued|submit)|Maximum number of jobs",
    re.IGNORECASE,
)


#: A failure to reach the cluster, worth trying again later
_TRANSIENT = re.compile(
    r"ssh: |Connection (timed out|refused|reset|closed)|Operation timed out|"
    r"Could not resolve hostname|Network is unreachable|No route to host|"
    r"Socket timed out|Unable to contact slurm controller",
    re.IGNORECASE,
)


class QueueFull(RuntimeError):
    """The queue will not take more jobs now (it is full, or cannot be reached);
    the TaskSet holds the bundle and tries again later.

    ``maybe_submitted`` is True when the bundle may have reached the queue
    (the connection dropped during ``sbatch``): its tasks must then stay
    recorded as queued, so that a restart looks for the job by name.
    """

    def __init__(self, message, maybe_submitted=False):
        super().__init__(message)
        self.maybe_submitted = maybe_submitted


class _Prepared:
    """A bundle written, and perhaps staged, but not yet known to be queued."""

    def __init__(self, bundle_dir, job_name, script):
        self.bundle_dir = bundle_dir
        self.job_name = job_name
        self.script = script
        self.staged = False
        self.maybe_submitted = False


class _Entry:
    """What the backend knows of one submitted task."""

    def __init__(self, task, directory, marker, bundle_dir, job_id, job_name=None):
        self.task = task
        self.digest = task.digest()  # once: it hashes every input file
        self.directory = Path(directory)
        self.marker = Path(marker)
        self.bundle_dir = Path(bundle_dir)
        # None until a job adopted by name is found in the queue
        self.job_id = None if job_id is None else str(job_id)
        self.job_name = job_name
        self.never_queued = False
        self.reason = None
        self.timed_out = False


class SchedulerBackend:
    """A ``TaskBackend`` that submits bundles of tasks to a queueing system.

    Parameters
    ----------
    queue : seamm_scheduler.QueueBackend
        Submits, polls and cancels jobs (a scheduler plus a transport).
    name : str
        The name recorded in the manifest, ``queue:<target>``.
    job_directory : str or Path
        The job's directory. Task directories must be inside it when the
        cluster does not share the filesystem.
    stager : seamm_scheduler.JobStager, optional
        ``RsyncStager`` when the cluster does not share the filesystem. None
        (or a ``LocalStager``) means shared storage.
    remote_job_directory : str, optional
        Where the job directory is mirrored on the cluster, if not shared.
    directives : dict, optional
        Site defaults, in the scheduler's own spelling (the target section's
        directive keys: partition, account, qos, export, ...).
    setup : str, optional
        Shell lines run in the batch script before the worker (module loads).
    python : str, optional
        A Python with ``seamm_exec`` where the tasks run. Default: this one.
    root : str, optional
        The SEAMM root with the ``<program>.ini`` files where the tasks run.
    accepts_config : bool = True
        Whether ``Task.config`` is valid where the tasks run (local
        transport). On a remote cluster it is not sent.
    bundle_walltime : float, optional
        Seconds to request for a bundle whose tasks give no walltime.
    max_queued : int, optional
        The most jobs this user may have queued and running at once.
    poll_interval : float = 30
        Seconds between polls of the queue.
    """

    #: The TaskSet submits one call per bundle.
    bundles = True

    def __init__(
        self,
        queue,
        *,
        name="queue",
        job_directory,
        stager=None,
        remote_job_directory=None,
        directives=None,
        setup=None,
        python=None,
        root=None,
        executor="local",
        accepts_config=True,
        bundle_walltime=None,
        max_queued=None,
        poll_interval=30.0,
        job_name_prefix="seamm",
        max_walltime=None,
    ):
        self.queue = queue
        self.name = name
        self.job_directory = Path(job_directory).resolve()
        self.stager = stager
        self.remote_job_directory = remote_job_directory
        self.directives = dict(directives or {})
        self.setup = setup
        self.python = python or sys.executable
        self.root = root
        self.executor = executor
        self.accepts_config = accepts_config
        if executor != "local":
            raise RuntimeError(
                f"Tasks can go to a queue only with the local executor, not "
                f"'{executor}'."
            )
        self.bundle_walltime = bundle_walltime
        # The queue's longest walltime: retries after a timeout never ask more
        self.max_walltime = max_walltime
        self.max_queued = max_queued
        self.poll_interval = poll_interval
        self.job_name_prefix = job_name_prefix
        self.abandon_timeout = 120.0

        self._entries = {}  # backend id -> _Entry
        self._jobs = {}  # job id -> last JobStatus (or None if not seen)
        self._misses = {}  # job id -> polls in a row it was not found
        self._pulled = set()  # job ids whose files are back (or never will be)
        self._ended_polls = {}  # job id -> polls since it was seen ended
        self._pull_failures = {}  # job id -> failed pulls, not counting outages
        self._unpullable = {}  # job id -> why its files cannot come back
        self._prepared = {}  # (bundle, keys) -> _Prepared, until it is queued
        self._resolved_at = 0.0
        self._polled_at = 0.0
        # The user's job count, shared with every back end on this queue system
        self._shared_count = shared_count_for(queue)

    # ------------------------------------------------------------------
    # Construction from a target section
    # ------------------------------------------------------------------
    @classmethod
    def from_target(cls, section, *, job_directory, root=None, executor="local"):
        """The back end a target section with ``tasks = queue`` describes."""
        queue = section.build_task_backend()
        stager = section.build_task_stager()
        shared = section.tasks_share_filesystem
        remote_job_directory = None
        python = None
        remote_root = root
        if remote_root is None and section.task_transport == "local":
            # The evaluator's own installation, as a step would find it.
            try:
                from seamm_util.root import current_root

                remote_root = str(current_root())
            except Exception:
                remote_root = os.environ.get("SEAMM_ROOT", "~/SEAMM")
        if not shared:
            if not section.remote_root:
                raise RuntimeError(
                    f"Target '{section.name}' sends tasks to {section.host} without "
                    "a shared filesystem, so it needs remote_root (where task "
                    "directories are staged)."
                )
            remote_job_directory = str(
                PurePosixPath(section.remote_root) / remote_name(job_directory)
            )
        if section.task_transport == "ssh":
            if not section.remote_python:
                raise RuntimeError(
                    f"Target '{section.name}' sends tasks to {section.host} over "
                    "ssh, so it needs remote_python: a Python with seamm_exec "
                    "on that cluster, which runs the tasks."
                )
            python = section.remote_python
            remote_root = section.remote_seamm_root or default_root(python)
        directives = dict(section.directives)
        return cls(
            queue,
            name=f"queue:{section.name}",
            job_directory=job_directory,
            stager=None if shared else stager,
            remote_job_directory=remote_job_directory,
            directives=directives,
            setup=section.setup,
            python=python,
            root=remote_root,
            executor=executor,
            accepts_config=section.task_transport == "local",
            bundle_walltime=section.bundle_walltime,
            max_walltime=_max_walltime(section),
            max_queued=section.max_queued_tasks,
            poll_interval=(
                section.poll_interval if section.poll_interval is not None else 30.0
            ),
        )

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def shared(self):
        return self.remote_job_directory is None

    def relative(self, path):
        """``path`` relative to the job directory."""
        path = Path(path).resolve()
        try:
            return path.relative_to(self.job_directory)
        except ValueError:
            raise RuntimeError(
                f"{path} is not inside the job directory {self.job_directory}, so "
                "it cannot be staged to the cluster."
            ) from None

    def where(self, path):
        """The path the cluster sees for a local ``path``."""
        if self.shared:
            return str(Path(path).resolve())
        return str(PurePosixPath(self.remote_job_directory) / self.relative(path))

    # ------------------------------------------------------------------
    # The TaskBackend interface
    # ------------------------------------------------------------------
    def room(self):
        """How many more bundles may be submitted now, or None for no limit."""
        if self.max_queued is None:
            return None
        count = self._shared_count.current(self.queue.count_jobs, self.poll_interval)
        if count is None:
            return None
        return self.max_queued - count

    def submit(
        self,
        tasks,
        directories,
        on_start=None,
        bundle=None,
        markers=None,
        on_prepared=None,
        bundle_walltime=None,
        walltime_scale=1.0,
    ):
        """Submit ``tasks`` as one bundle: one batch job. Returns their ids.

        ``bundle_walltime`` (seconds) is the caller's limit on a bundle, used to
        bound its time when the tasks give none. ``walltime_scale`` multiplies
        the time estimated for tasks that give none, e.g. 2 for a task whose
        last attempt ran out of time (still within ``bundle_walltime``).

        ``on_prepared(tasks, info)`` is called before ``sbatch`` with the
        bundle's unique job name and directory, so the caller can record them:
        a restart that finds no job id looks the job up by that name.
        """
        if not tasks:
            return []
        directories = [Path(d) for d in directories]
        markers = [Path(m) for m in (markers or directories)]
        bundle = bundle or "bundle"

        # A bundle held earlier (queue full, cluster unreachable) is the same
        # bundle when it is tried again: same directory, same job name.
        key = (bundle, tuple(t.key for t in tasks), walltime_scale)
        prepared = self._prepared.get(key)
        if prepared is None:
            prepared = self._prepare(
                bundle,
                tasks,
                directories,
                markers,
                bundle_walltime=bundle_walltime,
                walltime_scale=walltime_scale,
            )
            self._prepared[key] = prepared
        if on_prepared is not None:
            on_prepared(
                tasks,
                {
                    "job_name": prepared.job_name,
                    "bundle_dir": prepared.bundle_dir.name,
                },
            )

        if prepared.maybe_submitted:
            # The connection dropped during sbatch: does the queue have it?
            found = self.queue.find_jobs(prepared.job_name)
            if found is None:
                raise QueueFull(
                    f"cannot yet tell whether {prepared.bundle_dir.name} reached "
                    "the queue",
                    maybe_submitted=True,
                )
            if found:
                del self._prepared[key]
                logger.info(
                    f"{prepared.bundle_dir.name} had reached the queue as job "
                    f"{found[0]}"
                )
                return self._register(
                    tasks, directories, markers, prepared.bundle_dir, found[0]
                )
            prepared.maybe_submitted = False

        if not self.shared and not prepared.staged:
            paths = {str(self.relative(prepared.bundle_dir))}
            for directory, marker in zip(directories, markers):
                paths.add(str(self.relative(directory)))
                paths.add(str(self.relative(marker)))
            try:
                self._clear_remote_markers(tasks, directories, markers)
                self.stager.push(
                    str(self.job_directory), self.remote_job_directory, sorted(paths)
                )
            except Exception as e:
                if _TRANSIENT.search(str(e)):
                    raise QueueFull(f"cannot stage to the cluster: {e}") from e
                raise
            prepared.staged = True

        try:
            job_id = self.queue.submit(prepared.script, job_name=prepared.job_name)
        except Exception as e:
            if _QUEUE_FULL.search(str(e)):
                raise QueueFull(str(e)) from e
            if _TRANSIENT.search(str(e)):
                prepared.maybe_submitted = True
                raise QueueFull(str(e), maybe_submitted=True) from e
            del self._prepared[key]
            raise
        del self._prepared[key]
        self._shared_count.bump()
        logger.info(
            f"Submitted {prepared.bundle_dir.name} ({len(tasks)} tasks) as job "
            f"{job_id}"
        )
        return self._register(tasks, directories, markers, prepared.bundle_dir, job_id)

    def _prepare(
        self,
        bundle,
        tasks,
        directories,
        markers,
        bundle_walltime=None,
        walltime_scale=1.0,
    ):
        """Write a bundle: its directory, the tasks' inputs, bundle.json, run.sh."""
        # <step dir>/tasks/_bundles/<bundle>.<n>
        bundles_dir = markers[0].parent / "_bundles"
        bundles_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (bundles_dir / f"{bundle}.{n}").exists():
            n += 1
        bundle_dir = bundles_dir / f"{bundle}.{n}"
        bundle_dir.mkdir()

        entries = []
        for task, directory, marker in zip(tasks, directories, markers):
            directory.mkdir(parents=True, exist_ok=True)
            marker.mkdir(parents=True, exist_ok=True)
            for stale in ("DONE", "FAILED"):
                if (marker / stale).exists():
                    (marker / stale).unlink()
            # An earlier attempt's output must not pass this one's success check
            for name in task.success_text or {}:
                if (directory / name).is_file():
                    (directory / name).unlink()
            for filename, data in (task.files or {}).items():
                path = directory / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(data, bytes):
                    path.write_bytes(data)
                else:
                    path.write_text(data)
            entries.append(self._bundle_entry(task, directory, marker))

        bundle_json = {
            "version": 2,
            "mode": "seamm",
            "root": self.root,
            "executor": self.executor,
            "tasks": entries,
        }
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle_json, indent=2))
        script = self._script(
            tasks,
            bundle_dir,
            bundle_walltime=bundle_walltime,
            walltime_scale=walltime_scale,
        )
        (bundle_dir / "run.sh").write_text(script)
        # Unique, so the queue can be asked whether it has this very bundle
        job_name = f"{self.job_name_prefix}-{bundle_dir.name}-{secrets.token_hex(3)}"
        return _Prepared(bundle_dir, job_name, script)

    def _register(self, tasks, directories, markers, bundle_dir, job_id):
        ids = []
        for task, directory, marker in zip(tasks, directories, markers):
            backend_id = f"{job_id}#{bundle_dir.name}#{task.key}"
            self._entries[backend_id] = _Entry(
                task, directory, marker, bundle_dir, job_id
            )
            ids.append(backend_id)
        self._jobs.setdefault(str(job_id), None)
        return ids

    def check(self, task, directory, marker):
        """Fail early, before anything is submitted, for a task this back end
        cannot run (a directory outside the job, without shared storage)."""
        for path in (directory, marker, self.job_directory):
            if any(c.isspace() for c in str(Path(path).resolve())):
                raise RuntimeError(
                    f"'{path}' contains whitespace, which batch scripts and "
                    "rsync cannot carry safely. Use a directory without spaces "
                    "for jobs whose tasks go to a queue."
                )
        if not self.shared:
            self.relative(directory)
            self.relative(marker)

    def adopt(self, task, directory, marker, record):
        """Take back a task an earlier evaluator submitted, from its manifest
        record. Returns its id, or None if the record is not one of ours."""
        backend_id = record.get("id")
        parsed = parse_id(backend_id)
        if parsed is None and record.get("job_name") and record.get("bundle_dir"):
            # Prepared, perhaps submitted, but its id was never recorded (the
            # evaluator died during sbatch, or the connection dropped): find
            # it by its unique job name.
            backend_id = f"?{record['job_name']}#{record['bundle_dir']}#{task.key}"
            parsed = parse_id(backend_id)
        if parsed is None:
            return None
        job_id, bundle_name, key = parsed
        if key != task.key:
            return None
        bundle_dir = Path(marker).parent / "_bundles" / bundle_name
        if job_id.startswith("?"):
            self._entries[backend_id] = _Entry(
                task, directory, marker, bundle_dir, None, job_name=job_id[1:]
            )
        else:
            self._entries[backend_id] = _Entry(
                task, directory, marker, bundle_dir, job_id
            )
            self._jobs.setdefault(job_id, None)
        return backend_id

    def abandon(self, records, keep=()):
        """Cancel the jobs of ``records`` (tasks whose inputs have changed)
        unless a task in ``keep`` (ids adopted) still runs in the same job."""
        kept = {parse_id(i)[0] for i in keep if parse_id(i)}
        jobs = set()
        for record in records:
            parsed = parse_id(record.get("id"))
            if parsed is not None and parsed[0] not in kept:
                jobs.add(parsed[0])
            elif parsed is None and record.get("job_name"):
                found = self.queue.find_jobs(record["job_name"]) or []
                jobs.update(j for j in found if j not in kept)
        if not jobs:
            return
        jobs = sorted(jobs)
        logger.info(f"Cancelling jobs running earlier inputs: {jobs}")
        try:
            self.queue.cancel_many(jobs)
        except Exception as e:
            # Not fatal: DONE carries the fingerprint, so their results are
            # never taken for the new inputs.
            logger.warning(f"Could not cancel {jobs}: {e}")
            return
        # Wait for them to end (COMPLETING can take a while), so they cannot
        # write into the task directories the new inputs will use.
        deadline = time.time() + self.abandon_timeout
        while time.time() < deadline:
            try:
                statuses = self.queue.poll_many(jobs)
            except Exception:
                statuses = None
            if statuses is not None and not getattr(
                self.queue.scheduler, "poll_failed", False
            ):
                if all(
                    statuses.get(j) is None or statuses[j].is_terminal for j in jobs
                ):
                    return
            time.sleep(min(5.0, self.poll_interval))
        logger.warning(f"Jobs {jobs} had not ended after cancelling them")

    def status(self, ids):
        self._resolve_names([self._entries[i] for i in ids if i in self._entries])
        self._poll(
            [
                self._entries[i].job_id
                for i in ids
                if i in self._entries and self._entries[i].job_id is not None
            ]
        )
        result = {}
        for backend_id in ids:
            entry = self._entries.get(backend_id)
            if entry is None:
                result[backend_id] = LOST
                continue
            result[backend_id] = self._state(entry)
        return result

    def _resolve_names(self, entries):
        """Find the jobs of entries adopted by name, at most once a poll."""
        pending = {}
        for entry in entries:
            if entry.job_id is None and not entry.never_queued:
                pending.setdefault(entry.job_name, []).append(entry)
        if not pending or time.time() - self._resolved_at < self.poll_interval / 2:
            return
        self._resolved_at = time.time()
        for job_name, group in pending.items():
            found = self.queue.find_jobs(job_name)
            if found is None:
                continue  # cannot tell yet
            for entry in group:
                if found:
                    entry.job_id = found[0]
                    self._jobs.setdefault(found[0], None)
                else:
                    entry.never_queued = True

    def wait(self, ids, timeout=None):
        """Sleep until the next poll is due."""
        delay = self.poll_interval - (time.time() - self._polled_at)
        if delay > 0:
            time.sleep(delay)

    def cancel(self, ids):
        jobs = set()
        for backend_id in ids:
            entry = self._entries.get(backend_id)
            if entry is None:
                continue
            status = self._jobs.get(entry.job_id)
            if status is None or not status.is_terminal:
                jobs.add(entry.job_id)
        if jobs:
            self.queue.cancel_many(sorted(jobs))

    def fetch(self, task, backend_id):
        entry = self._entries.pop(backend_id, None)
        if entry is None:
            return TaskResult(key=task.key, state=LOST, reason="unknown task")
        state = self._state(entry)
        stdout = _read_text(entry.directory / "stdout.txt")
        stderr = _read_text(entry.directory / "stderr.txt")
        if state == FINISHED:
            done = _read_json(entry.marker / "DONE") or {}
            files = {}
            for name in done.get("files", []):
                data = _read_returned(entry.directory, name)
                if data is not None:
                    files[name] = data
            return TaskResult(
                key=task.key,
                state=FINISHED,
                returncode=done.get("returncode"),
                stdout=stdout,
                stderr=stderr,
                directory=entry.directory,
                files=files,
                in_situ=done.get("in_situ"),
                run_directory=done.get("run_directory"),
            )
        if state == FAILED:
            failed = _read_json(entry.marker / "FAILED") or {}
            return TaskResult(
                key=task.key,
                state=FAILED,
                returncode=failed.get("returncode"),
                stdout=stdout,
                stderr=stderr,
                directory=entry.directory,
                reason=failed.get("reason") or entry.reason,
            )
        return TaskResult(
            key=task.key,
            state=LOST,
            directory=entry.directory,
            stdout=stdout,
            stderr=stderr,
            reason=entry.reason or "lost",
            timed_out=entry.timed_out,
        )

    def reason(self, backend_id):
        """Why a task is lost, for the manifest."""
        entry = self._entries.get(backend_id)
        return None if entry is None else entry.reason

    def timed_out(self, backend_id):
        """Whether a lost task's job was stopped for running out of time."""
        entry = self._entries.get(backend_id)
        return False if entry is None else entry.timed_out

    def forget(self, backend_id):
        """Drop a task the TaskSet will submit again."""
        self._entries.pop(backend_id, None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _bundle_entry(self, task, directory, marker):
        resources = asdict(task.resources)
        return {
            "key": task.key,
            "directory": self.where(directory),
            "marker": self.where(marker),
            "program": task.program,
            "cmd": list(task.cmd),
            "files": sorted(task.files or {}),
            "return_files": list(task.return_files),
            "resources": resources,
            "env": dict(task.env),
            "in_situ": task.in_situ,
            "shell": task.shell,
            "input": task.input_data,
            "config": task.config if self.accepts_config else None,
            "fingerprint": task.digest(),
            "success_text": task.success_text,
            "keep": list(task.keep),
        }

    def _bundle_resources(self, tasks, bundle_walltime=None, walltime_scale=1.0):
        """One allocation big enough for the largest task in the bundle.

        A bundle is one batch job running the task worker and a local pool in
        its allocation, so it is always one node: a task's ranks must share the
        node, and its node-local scratch (seamm_exec#37). Tasks spanning nodes
        are not supported.
        """

        def largest(name):
            values = [getattr(t.resources, name) for t in tasks]
            values = [v for v in values if v is not None]
            return max(values) if values else None

        def first(name):
            for t in tasks:
                value = getattr(t.resources, name)
                if value is not None:
                    return value
            return None

        if (largest("nodes") or 1) > 1:
            logger.warning(
                "A task asked for more than one node; task bundles run on one "
                "node, so it gets one."
            )
        walltimes = [t.resources.walltime for t in tasks]
        limit = bundle_walltime or self.bundle_walltime
        if all(w is not None for w in walltimes):
            walltime = sum(walltimes)
        else:
            # Without the tasks' walltimes the queue's default would apply (an
            # hour on some partitions) and kill a long bundle: use their
            # estimates with a margin, within the bundle limit.
            estimate = sum(
                w if w is not None else (t.estimated_seconds or 0.0)
                for t, w in zip(tasks, walltimes)
            )
            if estimate > 0:
                # A retry after running out of time gets more (walltime_scale),
                # within the bundle limit and the queue's longest walltime
                walltime = walltime_scale * (2.0 * estimate + 600.0)
                if limit:
                    walltime = min(walltime, limit)
                if self.max_walltime:
                    walltime = min(walltime, self.max_walltime)
            else:
                walltime = limit
        ntasks = largest("ntasks")
        if ntasks is None and "ntasks" not in self.directives:
            # Always ask for the cores, so the allocation's environment says
            # how many it has.
            ntasks = 1
        return {
            "ntasks": ntasks,
            "cpus_per_task": largest("cpus_per_task") or 1,
            "mem_per_cpu": largest("mem_per_cpu"),
            "ngpus": largest("ngpus") or 0,
            "walltime": walltime,
            "nodes": 1,
            "partition": first("partition"),
            "account": first("account"),
            "qos": first("qos"),
        }

    def _script(self, tasks, bundle_dir, bundle_walltime=None, walltime_scale=1.0):
        from seamm_scheduler import build_script

        scheduler = self.queue.scheduler
        where = self.where(bundle_dir)
        directives = scheduler.directives(
            self._bundle_resources(
                tasks, bundle_walltime=bundle_walltime, walltime_scale=walltime_scale
            ),
            extra=self.directives,
        )
        directives.update(scheduler.log_directives(where))
        lines = []
        if self.setup:
            lines.append(self.setup)
        lines.append(f"cd {shlex.quote(where)}")
        lines.append(
            f"exec {shlex.quote(self.python)} -m seamm_exec.task_worker bundle.json"
        )
        return build_script(directives, "\n".join(lines), scheduler=scheduler)

    def _clear_remote_markers(self, tasks, directories, markers):
        """Remove on the cluster what rsync would keep: stale DONE/FAILED, and
        an earlier attempt's files that a success check reads."""
        paths = []
        for task, directory, marker in zip(tasks, directories, markers):
            remote = self.where(marker)
            paths += [f"{remote}/DONE", f"{remote}/FAILED"]
            for name in task.success_text or {}:
                paths.append(f"{self.where(directory)}/{name}")
        rc, out, err = self.queue._run(
            ["xargs", "-0", "rm", "-f"], input_text="\0".join(paths) + "\0"
        )
        if rc != 0:
            if _TRANSIENT.search(err):
                raise RuntimeError(err.strip())
            logger.warning(f"Could not clear old task markers: {err.strip()}")

    def _poll(self, job_ids):
        """Poll the queue for ``job_ids``, at most once per poll interval, and
        bring back the files of bundles whose jobs have ended."""
        job_ids = sorted(set(job_ids))
        now = time.time()
        if now - self._polled_at < self.poll_interval / 2:
            return
        # Every pass counts, so that wait() always sleeps.
        self._polled_at = now

        live = [
            j
            for j in job_ids
            if self._jobs.get(j) is None or not self._jobs[j].is_terminal
        ]
        if live:
            try:
                statuses = self.queue.poll_many(live)
            except Exception as e:
                logger.warning(f"Could not poll {self.name}: {e}")
                statuses = None
            if statuses is not None:
                failed = getattr(self.queue.scheduler, "poll_failed", False)
                if failed:
                    logger.warning(
                        f"Could not ask {self.name} about every job; trying again "
                        "later."
                    )
                for job_id in live:
                    status = statuses.get(job_id)
                    if status is None:
                        # Missing only counts when the queue could be asked.
                        if not failed:
                            self._misses[job_id] = self._misses.get(job_id, 0) + 1
                        continue
                    self._misses.pop(job_id, None)
                    self._jobs[job_id] = status

        # Jobs that have ended (terminal, or gone from the queue) and whose
        # files are not back yet -- whether or not they were polled just now.
        for job_id in job_ids:
            if job_id in self._pulled:
                continue
            status = self._jobs.get(job_id)
            ended = (status is not None and status.is_terminal) or self._misses.get(
                job_id, 0
            ) >= 3
            if not ended:
                continue
            self._ended_polls[job_id] = self._ended_polls.get(job_id, 0) + 1
            if self.shared:
                # One more poll before trusting what the markers say, for
                # filesystems that cache directory lookups.
                if self._ended_polls[job_id] >= 2:
                    self._pulled.add(job_id)
                continue
            try:
                self._pull([job_id])
            except Exception as e:
                if not _TRANSIENT.search(str(e)):
                    n = self._pull_failures.get(job_id, 0) + 1
                    self._pull_failures[job_id] = n
                    if n >= 5:
                        # The files cannot come back (e.g. scratch purged).
                        self._unpullable[job_id] = str(e)
                        self._pulled.add(job_id)
                logger.warning(f"Could not stage back job {job_id}: {e}")
                continue
            self._pulled.add(job_id)
            self._remove_remote(job_id)

    def _pull(self, job_ids):
        """Stage back the directories of the bundles whose jobs ended."""
        paths = set()
        for entry in self._entries.values():
            if entry.job_id in job_ids:
                paths.add(str(self.relative(entry.bundle_dir)))
                paths.add(str(self.relative(entry.directory)))
                paths.add(str(self.relative(entry.marker)))
        if paths:
            self.stager.pull(
                self.remote_job_directory, str(self.job_directory), sorted(paths)
            )

    def _remove_remote(self, job_id):
        """Remove the remote copies of a bundle's directories, now staged back,
        so they neither pile up under ``remote_root`` nor come back after the
        task layer archives or prunes them here. A directory another task still
        running uses (a step directory given to several) is kept."""
        mine, others = set(), set()
        for entry in self._entries.values():
            paths = {
                str(self.relative(entry.bundle_dir)),
                str(self.relative(entry.directory)),
                str(self.relative(entry.marker)),
            }
            if entry.job_id == job_id:
                mine |= paths
            elif entry.job_id not in self._pulled:
                others |= paths
        paths = sorted(p for p in mine - others if p not in ("", "."))
        if not paths:
            return
        base = PurePosixPath(self.remote_job_directory)
        argv = ["rm", "-rf", "--"] + [str(base / p) for p in paths]
        try:
            rc, out, err = self.queue._run(argv)
            if rc != 0:
                logger.warning(f"Could not remove the remote copies of {job_id}: {err}")
        except Exception as e:
            # Not fatal: they are copies
            logger.warning(f"Could not remove the remote copies of {job_id}: {e}")

    def _marker(self, entry):
        """ "DONE", "FAILED" or None, from markers written for *this* task."""
        try:
            # A fresh listing, not a cached lookup of each name
            names = set(os.listdir(entry.marker))
        except OSError:
            return None
        digest = entry.digest
        for name in ("DONE", "FAILED"):
            if name in names:
                data = _read_json(entry.marker / name)
                if data is None:
                    continue  # being written
                if data.get("fingerprint") not in (None, digest):
                    continue  # left by an earlier version of the task
                return name
        return None

    def _state(self, entry):
        if entry.job_id is None:
            # Adopted by name and not found yet
            if entry.never_queued:
                entry.reason = (
                    f"bundle {entry.bundle_dir.name} never reached the queue "
                    f"(no job named {entry.job_name})"
                )
                return LOST
            if self.shared and self._marker(entry) == "DONE":
                return FINISHED
            return QUEUED
        # The worker's markers decide, whenever they can be seen: on a shared
        # filesystem even while the bundle still runs.
        if self.shared or entry.job_id in self._pulled:
            marker = self._marker(entry)
            if marker == "DONE":
                return FINISHED
            if marker == "FAILED":
                return FAILED
        status = self._jobs.get(entry.job_id)
        if entry.job_id in self._pulled:
            # Ended, its files are back (or cannot come back), and no marker.
            if entry.job_id in self._unpullable:
                entry.reason = (
                    f"the files of job {entry.job_id} could not be staged back: "
                    f"{self._unpullable[entry.job_id]}"
                )
            elif status is not None and status.is_terminal:
                entry.reason = (
                    f"job {entry.job_id} ended ({status.state}) before the task "
                    "finished"
                )
                timed_out = getattr(status, "timed_out", None)
                if timed_out is None:  # an older seamm_scheduler
                    timed_out = (status.state or "").upper() == "TIMEOUT"
                entry.timed_out = bool(timed_out)
            else:
                entry.reason = (
                    f"job {entry.job_id} is no longer known to the queue and the "
                    "task did not finish"
                )
            tail = _log_tail(entry.bundle_dir)
            if tail:
                entry.reason += f"; its log ends: {tail}"
            return LOST
        if status is None or not status.is_terminal:
            if status is not None and status.task_state == "running":
                return RUNNING
            return QUEUED
        return RUNNING  # ended; its files are not back yet


def _max_walltime(section):
    """The queue's longest walltime, in seconds, from a target section: its
    ``max_walltime``, else the maximum of an overridable ``time``."""
    value = getattr(section, "max_walltime", None)
    if value:
        return float(value)
    limits = getattr(section, "limits", None) or {}
    limit = limits.get("time")
    maximum = getattr(limit, "maximum", None) if limit is not None else None
    if maximum:
        try:
            from seamm_scheduler.config import _parse_time

            return float(_parse_time(maximum))
        except Exception:
            return None
    return None


def parse_id(backend_id):
    """``<job id>#<bundle>.<n>#<key>`` -> (job id, bundle dir name, key)."""
    if not backend_id or backend_id.count("#") != 2:
        return None
    job_id, bundle, key = backend_id.split("#")
    return job_id, bundle, key


def remote_name(job_directory):
    """The name of a job directory on the cluster: its own name and a hash of
    its full path. Job numbers are unique only within one datastore, and
    several installations (~/SEAMM, ~/SEAMM_DEV, ChemAI) may share a
    ``remote_root``; a hand run's directory may be called anything."""
    path = Path(job_directory).resolve()
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:10]
    return f"{path.name}-{digest}"


def default_root(python):
    """The SEAMM root a venv's Python belongs to: ``<root>/venv/bin/python``
    or ``<root>/venvs/<stamp>/bin/python``."""
    path = PurePosixPath(python)
    venv = path.parent.parent
    if venv.parent.name == "venvs":
        return str(venv.parent.parent)
    return str(venv.parent)


def _log_tail(bundle_dir):
    """The last line of a bundle's scheduler log, if there is one."""
    lines = []
    for path in sorted(Path(bundle_dir).glob("*.out")):
        lines = [x for x in _read_text(path).splitlines() if x.strip()] or lines
    return lines[-1].strip()[:300] if lines else None


def _read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _read_returned(directory, name):
    """A returned file: ``@subdir+name`` lands in ``subdir/name``."""
    if name.startswith("@") and "+" in name:
        subdir, fname = name[1:].split("+", 1)
        path = Path(directory) / subdir / fname
    else:
        path = Path(directory) / name
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data


__all__ = ["SchedulerBackend", "QueueFull", "parse_id", "remote_name"]
