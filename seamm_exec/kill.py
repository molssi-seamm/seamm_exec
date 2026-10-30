# -*- coding: utf-8 -*-

"""Stopping what a killed job spawned.

A job killed through its JobServer (``kill_job``, or a deleted job) gets a marker,
:data:`KILL_MARKER`, in its directory before it is stopped. A job that receives
SIGTERM or SIGHUP and finds the marker cancels the queue jobs it submitted -- its
task bundles and the evaluators of a parallel loop's iterations -- after passing
the marker down to each iteration's evaluator, so a nested evaluator cancels its
own bundles in turn. Without the marker a SIGTERM is an interruption (a walltime
limit, a lost node): the bundles keep running, and the resumed job adopts them
from its manifest (Paul, 2026-10-30; ChemAI 5469, whose killed job left its Owl
evaluator 1327569 and ORCA bundle 1327599 running).
"""

import logging
from pathlib import Path

logger = logging.getLogger("seamm-exec")

#: The marker the JobServer writes in a job's directory before stopping it; the
#: same name is in seamm_jobserver
KILL_MARKER = ".seamm_killed"


def is_killed(directory):
    """Whether the job or evaluator in ``directory`` was killed."""
    if directory is None:
        return False
    try:
        return (Path(directory) / KILL_MARKER).exists()
    except OSError:
        return False
