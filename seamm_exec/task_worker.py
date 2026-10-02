#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Run a bundle of tasks, one after another, inside one allocation.

Usage::

    python task_worker.py bundle.json

This is the generic worker that a scheduler back end submits for a bundle of
small tasks. It is pure Python with no SEAMM imports, so it runs anywhere a
``python3`` exists, and is run by path rather than imported.

``bundle.json``::

    {
      "tasks": [
        {
          "key": "frag-0001",
          "directory": "/path/to/tasks/frag-0001",
          "command": "orca orca.inp > orca.out",   # fully resolved
          "shell": true,
          "env": {"OMP_NUM_THREADS": "1"},
          "input": null,
          "fingerprint": "sha256:...",
          "success_text": {"orca.out": "ORCA TERMINATED NORMALLY"}
        },
        ...
      ]
    }

For each task, in order: if ``<directory>/DONE`` exists it is skipped;
otherwise the command runs in the directory with its standard output and error
in ``stdout.txt`` and ``stderr.txt``, the return code goes in ``returncode``,
and a successful run -- return code 0 and, if given, every ``success_text``
file containing its text -- writes ``DONE`` (JSON). A failure does not stop the
bundle.
The worker exits 0 if every task succeeded and 1 otherwise.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


def _succeeded(directory, success_text):
    """Whether every file in ``success_text`` contains its text."""
    for name, text in (success_text or {}).items():
        path = Path(directory) / name
        try:
            data = path.read_text(errors="replace")
        except OSError:
            return False
        texts = [text] if isinstance(text, str) else text
        if any(t not in data for t in texts):
            return False
    return True


def run_bundle(bundle):
    """Run the tasks of a bundle. Returns the number that failed."""
    failures = 0
    for task in bundle.get("tasks", []):
        directory = Path(task["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "DONE").exists():
            continue

        env = dict(os.environ)
        env.update(task.get("env") or {})
        shell = bool(task.get("shell", False))
        command = task["command"]
        if not shell and isinstance(command, str):
            command = shlex.split(command)

        started = time.time()
        with open(directory / "stdout.txt", "w") as out, open(
            directory / "stderr.txt", "w"
        ) as err:
            try:
                p = subprocess.run(
                    command,
                    cwd=directory,
                    env=env,
                    input=task.get("input"),
                    shell=shell,
                    stdout=out,
                    stderr=err,
                    universal_newlines=True,
                )
                returncode = p.returncode
            except Exception as e:
                err.write(f"The task could not be started: {e}\n")
                returncode = -1
        (directory / "returncode").write_text(f"{returncode}\n")

        if returncode == 0 and not _succeeded(directory, task.get("success_text")):
            with open(directory / "stderr.txt", "a") as err:
                err.write("The task failed its success_text check.\n")
            returncode = None

        if returncode == 0:
            done = {
                "key": task.get("key"),
                "state": "finished",
                "returncode": 0,
                "fingerprint": task.get("fingerprint"),
                "started": started,
                "finished": time.time(),
            }
            tmp = directory / "DONE.tmp"
            tmp.write_text(json.dumps(done, indent=2))
            os.replace(tmp, directory / "DONE")
        else:
            failures += 1
    return failures


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("Usage: task_worker.py bundle.json", file=sys.stderr)
        return 2
    bundle = json.loads(Path(argv[0]).read_text())
    return 0 if run_bundle(bundle) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
