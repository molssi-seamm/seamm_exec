# -*- coding: utf-8 -*-

"""Find the job's target: where its tasks run.

A target is a section of the JobServer's ``<root>/<jobserver-name>.ini`` (see
``seamm_scheduler.config``). The evaluator finds it, in order:

1. given explicitly (``TaskSet(target=...)``, a name or a ``TargetSection``);
2. ``<job dir>/target.json``, written by the JobServer when it starts the job,
   so an evaluator running as a batch job on a cluster, which cannot read the
   JobServer's ini file, still knows its target;
3. the environment variable ``SEAMM_TARGET``, naming a section of
   ``<root>/<hostname>.ini`` (or of the file named by ``SEAMM_TARGETS``), for
   runs by hand with ``run_flowchart``;
4. none: tasks run in the evaluator's own ``LocalPool``, as before targets
   existed.
"""

import json
import logging
import os
from pathlib import Path
import socket

logger = logging.getLogger("seamm-exec")

TARGET_FILE = "target.json"


def find_target(target=None, *, job_directory=None, root=None):
    """The target section for the job, or None.

    Parameters
    ----------
    target : str or TargetSection, optional
        A section, or the name of one in ``<root>/<hostname>.ini``.
    job_directory : str or Path, optional
        Where to look for ``target.json``. Default: the current directory.
    root : str or Path, optional
        The SEAMM root holding the JobServer's ini file.

    Returns
    -------
    seamm_scheduler.TargetSection or None
    """
    from seamm_scheduler import TargetSection

    if isinstance(target, TargetSection):
        return target
    if target is not None:
        return _from_ini(str(target), root)

    directory = Path(job_directory) if job_directory is not None else Path.cwd()
    path = directory / TARGET_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            raise RuntimeError(f"Could not read the job's target from {path}: {e}")
        return TargetSection.from_settings(data)

    name = os.environ.get("SEAMM_TARGET")
    if name:
        return _from_ini(name, root, os.environ.get("SEAMM_TARGETS"))
    return None


def write_target(section, job_directory):
    """Write ``<job dir>/target.json`` for a section (what the JobServer does)."""
    path = Path(job_directory) / TARGET_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(section.task_settings(), indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


def _from_ini(name, root, ini_path=None):
    from seamm_scheduler import load_target

    if ini_path:
        path = Path(ini_path).expanduser()
        section = load_target(path.parent, path.stem, section=name)
    else:
        if root is None:
            root = os.environ.get("SEAMM_ROOT", "~/SEAMM")
        section = load_target(root, socket.gethostname(), section=name)
        if section is None:
            raise RuntimeError(
                f"Target '{name}': there is no {Path(root) / socket.gethostname()}.ini"
            )
    return section
