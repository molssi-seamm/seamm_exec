# -*- coding: utf-8 -*-

"""Helper routines for determining the computational environment from queueuing systems.
"""

import os

import psutil


def computational_environment(limits={}):
    """Examine where we are running to get the computational environment.

    Parameters
    ----------
    limits : dict(str, any)
        Limits to impose on the number of tasks, gpus, etc.

    Returns
    -------
    dict(str, any)
        The attributes of the computational enviroment, limited by the imposed limits.
    """

    if "SLURM_JOB_ID" in os.environ:
        ce = _slurm()
    else:
        ce = _local()

    for key, value in limits.items():
        if key in ce:
            if ce[key] > value:
                ce[key] = value
        else:
            ce[key] = value

    return ce


def _local():
    """Get the number of cores, GPUs, etc. for the local machine."""
    ce = {}

    ce["NTASKS"] = psutil.cpu_count(logical=False)

    memory = psutil.virtual_memory()
    ce["MEM_PER_NODE"] = memory.available
    ce["MEM_PER_CPU"] = ce["MEM_PER_NODE"] // ce["NTASKS"]

    return ce


def _slurm():
    """Get the number of tasks, gpus, etc. for a SLURM job."""
    ce = {}
    if "SLURM_JOB_ID" not in os.environ:
        raise RuntimeError("This does not appear to be a SLURM job.")

    ce["type"] = "slurm"
    for item, value in os.environ.items():
        if value.isdecimal():
            value = int(value)
        if item[0:6] == "SLURM_":
            ce[item[6:]] = value
        elif item[0:7] == "SBATCH_":
            ce[item[7:]] = value

    if "NTASKS_PER_NODE" not in ce:
        ce["NTASKS_PER_NODE"] = int(ce["NTASKS"]) // int(ce["NNODES"])

    # Expand `[i-k]` naming in nodelist
    nodes = ce["NODELIST"].split(",")
    nodelist = []
    npernode = ce["NTASKS_PER_NODE"]
    for node in nodes:
        if "[" in node:
            node, count = node.split("[")
            first, last = count[0:-1].split("-")
            for i in range(int(first), int(last) + 1):
                nodelist.append(f"{node}{i}:{npernode}")
        else:
            nodelist.append(f"{node}:{npernode}")
    nodelist = ",".join(nodelist)
    ce["NODELIST"] = nodelist

    if "JOB_GPUS" in ce:
        ce["NGPUS"] = len(ce["JOB_GPUS"].split(","))

    return ce
