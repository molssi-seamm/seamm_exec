# -*- coding: utf-8 -*-

"""Helper routines for determining the computational environment from queueuing
systems."""

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

    # Expand `[i-k]` naming in nodelist, eg. SLURM_NODELIST=tc[053,059,183,200]
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
        if isinstance(ce["JOB_GPUS"], str):
            ce["NGPUS"] = len(ce["JOB_GPUS"].split(","))
        else:
            ce["NGPUS"] = 1

    _slurm_normalize_memory(ce)

    return ce


def _slurm_normalize_memory(ce):
    """Put ``MEM_PER_NODE`` and ``MEM_PER_CPU`` into **bytes**, matching
    ``_local()`` so every consumer sees the same units.

    SLURM reports ``SLURM_MEM_PER_CPU`` / ``SLURM_MEM_PER_NODE`` in **MiB**, and
    normally sets only the one matching how memory was requested (``--mem-per-cpu``
    vs ``--mem`` / ``--mem-per-node``); the other is absent. Here we convert
    whichever is present to bytes and derive the missing one from the cores
    allocated per node (``NTASKS_PER_NODE * CPUS_PER_TASK``). If neither is set
    (no memory limit requested), fall back to the node's available memory.
    """
    MiB = 1024 * 1024
    cpus_per_task = int(ce.get("CPUS_PER_TASK", 1) or 1)
    ntasks_per_node = int(ce.get("NTASKS_PER_NODE", 1) or 1)
    cores_per_node = max(1, ntasks_per_node * cpus_per_task)

    mem_node = int(ce.get("MEM_PER_NODE", 0) or 0)  # MiB (from SLURM)
    mem_cpu = int(ce.get("MEM_PER_CPU", 0) or 0)  # MiB (from SLURM)
    if mem_node > 0:
        ce["MEM_PER_NODE"] = mem_node * MiB
        ce["MEM_PER_CPU"] = ce["MEM_PER_NODE"] // cores_per_node
    elif mem_cpu > 0:
        ce["MEM_PER_CPU"] = mem_cpu * MiB
        ce["MEM_PER_NODE"] = ce["MEM_PER_CPU"] * cores_per_node
    else:
        available = psutil.virtual_memory().available
        ce["MEM_PER_NODE"] = available
        ce["MEM_PER_CPU"] = available // cores_per_node
    return ce
