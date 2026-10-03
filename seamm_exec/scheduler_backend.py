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
import time

from .tasks import FAILED, FINISHED, LOST, QUEUED, RUNNING, TaskResult

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
    the TaskSet holds the bundle and tries again later."""


class _Entry:
    """What the backend knows of one submitted task."""

    def __init__(self, task, directory, marker, bundle_dir, job_id):
        self.task = task
        self.directory = Path(directory)
        self.marker = Path(marker)
        self.bundle_dir = Path(bundle_dir)
        self.job_id = str(job_id)
        self.reason = None


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
        self.bundle_walltime = bundle_walltime
        self.max_queued = max_queued
        self.poll_interval = poll_interval
        self.job_name_prefix = job_name_prefix

        self._entries = {}  # backend id -> _Entry
        self._jobs = {}  # job id -> last JobStatus (or None if not seen)
        self._misses = {}  # job id -> polls in a row it was not found
        self._pulled = set()  # job ids whose files are back (or never will be)
        self._ended_polls = {}  # job id -> polls since it was seen ended
        self._pull_failures = {}  # job id -> failed pulls, not counting outages
        self._unpullable = {}  # job id -> why its files cannot come back
        self._unconfirmed = {}  # (bundle, keys) -> (bundle_dir, job name, script)
        self._polled_at = 0.0
        self._count = None
        self._counted_at = 0.0

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
        now = time.time()
        if self._count is None or now - self._counted_at > self.poll_interval:
            count = self.queue.count_jobs()
            if count is None:
                return None
            self._count = count
            self._counted_at = now
        return self.max_queued - self._count

    def submit(self, tasks, directories, on_start=None, bundle=None, markers=None):
        """Submit ``tasks`` as one bundle: one batch job. Returns their ids."""
        if not tasks:
            return []
        directories = [Path(d) for d in directories]
        markers = [Path(m) for m in (markers or directories)]
        bundle = bundle or "bundle"

        # A submission whose outcome was unknown (the connection dropped): find
        # out whether the queue has it before submitting it again.
        key = (bundle, tuple(t.key for t in tasks))
        if key in self._unconfirmed:
            bundle_dir, job_name, script = self._unconfirmed[key]
            found = self.queue.find_jobs(job_name)
            if found is None:
                raise QueueFull(
                    f"cannot yet tell whether {bundle_dir.name} reached the queue"
                )
            if found:
                del self._unconfirmed[key]
                logger.info(
                    f"{bundle_dir.name} had reached the queue as job {found[0]}"
                )
                return self._register(tasks, directories, markers, bundle_dir, found[0])
            return self._submit_script(
                key, tasks, directories, markers, bundle_dir, job_name, script
            )

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
        script = self._script(tasks, bundle_dir)
        (bundle_dir / "run.sh").write_text(script)

        if not self.shared:
            paths = {str(self.relative(bundle_dir))}
            for directory, marker in zip(directories, markers):
                paths.add(str(self.relative(directory)))
                paths.add(str(self.relative(marker)))
            try:
                self._clear_remote_markers(markers)
                self.stager.push(
                    str(self.job_directory), self.remote_job_directory, sorted(paths)
                )
            except Exception as e:
                if _TRANSIENT.search(str(e)):
                    raise QueueFull(f"cannot stage to the cluster: {e}") from e
                raise

        # Unique, so the queue can be asked whether it has this very bundle
        job_name = f"{self.job_name_prefix}-{bundle_dir.name}-{secrets.token_hex(3)}"
        return self._submit_script(
            key, tasks, directories, markers, bundle_dir, job_name, script
        )

    def _submit_script(
        self, key, tasks, directories, markers, bundle_dir, job_name, script
    ):
        try:
            job_id = self.queue.submit(script, job_name=job_name)
        except Exception as e:
            if _QUEUE_FULL.search(str(e)):
                self._unconfirmed.pop(key, None)
                raise QueueFull(str(e)) from e
            if _TRANSIENT.search(str(e)):
                # It may or may not have reached the queue.
                self._unconfirmed[key] = (bundle_dir, job_name, script)
                raise QueueFull(str(e)) from e
            self._unconfirmed.pop(key, None)
            raise
        self._unconfirmed.pop(key, None)
        if self._count is not None:
            self._count += 1
        logger.info(f"Submitted {bundle_dir.name} ({len(tasks)} tasks) as job {job_id}")
        return self._register(tasks, directories, markers, bundle_dir, job_id)

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
        if not self.shared:
            self.relative(directory)
            self.relative(marker)

    def adopt(self, task, directory, marker, record):
        """Take back a task an earlier evaluator submitted, from its manifest
        record. Returns its id, or None if the record is not one of ours."""
        backend_id = record.get("id")
        parsed = parse_id(backend_id)
        if parsed is None:
            return None
        job_id, bundle_name, key = parsed
        if key != task.key:
            return None
        bundle_dir = Path(marker).parent / "_bundles" / bundle_name
        self._entries[backend_id] = _Entry(task, directory, marker, bundle_dir, job_id)
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
        if not jobs:
            return
        logger.info(f"Cancelling jobs running earlier inputs: {sorted(jobs)}")
        try:
            self.queue.cancel_many(sorted(jobs))
        except Exception as e:
            # Harmless: DONE carries the fingerprint, so their results are
            # never taken for the new inputs.
            logger.warning(f"Could not cancel {sorted(jobs)}: {e}")

    def status(self, ids):
        self._poll([self._entries[i].job_id for i in ids if i in self._entries])
        result = {}
        for backend_id in ids:
            entry = self._entries.get(backend_id)
            if entry is None:
                result[backend_id] = LOST
                continue
            result[backend_id] = self._state(entry)
        return result

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
        )

    def reason(self, backend_id):
        """Why a task is lost, for the manifest."""
        entry = self._entries.get(backend_id)
        return None if entry is None else entry.reason

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
        }

    def _bundle_resources(self, tasks):
        """One allocation big enough for the largest task in the bundle."""

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

        walltimes = [t.resources.walltime for t in tasks]
        if all(w is not None for w in walltimes):
            walltime = sum(walltimes)
        else:
            walltime = self.bundle_walltime
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
            "nodes": largest("nodes"),
            "partition": first("partition"),
            "account": first("account"),
            "qos": first("qos"),
        }

    def _script(self, tasks, bundle_dir):
        from seamm_scheduler import build_script

        scheduler = self.queue.scheduler
        where = self.where(bundle_dir)
        directives = scheduler.directives(
            self._bundle_resources(tasks), extra=self.directives
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

    def _clear_remote_markers(self, markers):
        """Remove stale DONE/FAILED on the cluster, which rsync would keep."""
        paths = []
        for marker in markers:
            remote = self.where(marker)
            paths += [f"{remote}/DONE", f"{remote}/FAILED"]
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

    def _marker(self, entry):
        """ "DONE", "FAILED" or None, from markers written for *this* task."""
        try:
            # A fresh listing, not a cached lookup of each name
            names = set(os.listdir(entry.marker))
        except OSError:
            return None
        digest = entry.task.digest()
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
