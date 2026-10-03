# -*- coding: utf-8 -*-

"""Run a SEAMM-mode bundle inside an allocation (see ``task_worker.py``).

Each task runs through a :class:`~seamm_exec.local_pool.LocalPool` sized to the
allocation, with programs resolved on this machine, and the task's marker
directory receives ``DONE`` or ``FAILED``.
"""

import json
import os
from pathlib import Path
import time

from .local import Local
from .local_pool import LocalPool
from .tasks import TERMINAL_STATES, Resources, Task


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _read_input(path):
    """An input file as text if it is UTF-8, else bytes."""
    data = Path(path).read_bytes()
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data


def _log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def run_seamm_bundle(bundle):
    """Run a SEAMM-mode bundle through a LocalPool. Returns the number failed."""
    pool = LocalPool(Local(), root=bundle.get("root"), resolve_programs=True)
    _log(f"Capacity: {pool.capacity()}")

    tasks = []
    directories = []
    markers = {}
    for entry in bundle.get("tasks", []):
        marker = Path(entry["marker"])
        marker.mkdir(parents=True, exist_ok=True)
        done = marker / "DONE"
        if done.exists():
            try:
                fingerprint = json.loads(done.read_text()).get("fingerprint")
            except Exception:
                fingerprint = None
            if fingerprint in (None, entry.get("fingerprint")):
                _log(f"{entry['key']}: already done")
                continue
            _log(f"{entry['key']}: DONE is for other inputs; running it again")
            done.unlink()
        if (marker / "FAILED").exists():
            (marker / "FAILED").unlink()
        directory = Path(entry["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        files = None
        if entry.get("files"):
            files = {name: _read_input(directory / name) for name in entry["files"]}
        task = Task(
            key=entry["key"],
            program=entry.get("program", ""),
            cmd=list(entry.get("cmd") or []),
            files=files,
            return_files=list(entry.get("return_files") or []),
            resources=Resources(**(entry.get("resources") or {})),
            env=dict(entry.get("env") or {}),
            in_situ=entry.get("in_situ"),
            shell=bool(entry.get("shell", False)),
            input_data=entry.get("input"),
            directory=directory,
            config=entry.get("config"),
            fingerprint=entry.get("fingerprint"),
            success_text=entry.get("success_text"),
        )
        tasks.append(task)
        directories.append(directory)
        markers[task.key] = marker

    if not tasks:
        return 0

    def started(task, info):
        _log(f"{task.key}: started")

    ids = pool.submit(tasks, directories, on_start=started)
    by_id = dict(zip(ids, tasks))
    failures = 0
    remaining = set(ids)
    while remaining:
        pool.wait(list(remaining), timeout=10)
        states = pool.status(list(remaining))
        for backend_id, state in states.items():
            if state not in TERMINAL_STATES:
                continue
            remaining.discard(backend_id)
            task = by_id[backend_id]
            result = pool.fetch(task, backend_id)
            reason = None
            if result.state == "finished":
                directory = Path(task.directory)
                for name, text in (task.success_text or {}).items():
                    data = result.files.get(name)
                    if data is None and (directory / name).exists():
                        data = (directory / name).read_text(errors="replace")
                    if isinstance(data, bytes):
                        data = data.decode(errors="replace")
                    texts = [text] if isinstance(text, str) else list(text)
                    if data is None:
                        reason = f"success check: {name} is missing"
                    else:
                        for t in texts:
                            if t not in data:
                                reason = f"success check: '{t}' is not in {name}"
                    if reason is not None:
                        break
            elif result.returncode is None:
                lines = (result.stderr or "").strip().splitlines()
                reason = "could not be run: " + (lines[-1] if lines else "unknown")
            else:
                reason = f"return code {result.returncode}"
            marker = markers[task.key]
            if result.state == "finished" and reason is None:
                _write_json(
                    marker / "DONE",
                    {
                        "key": task.key,
                        "state": "finished",
                        "returncode": result.returncode,
                        "fingerprint": task.digest(),
                        "files": sorted(result.files),
                        "in_situ": result.in_situ,
                        "run_directory": result.run_directory,
                        "finished": time.time(),
                    },
                )
                _log(f"{task.key}: finished")
            else:
                failures += 1
                _write_json(
                    marker / "FAILED",
                    {
                        "key": task.key,
                        "state": "failed",
                        "returncode": result.returncode,
                        "reason": reason,
                        "fingerprint": task.digest(),
                        "finished": time.time(),
                    },
                )
                _log(f"{task.key}: failed: {reason}")
    return failures
