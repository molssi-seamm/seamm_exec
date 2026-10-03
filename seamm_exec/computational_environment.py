# -*- coding: utf-8 -*-

"""Helper routines for determining the computational environment from queueuing
systems."""

import os
from pathlib import Path
import re

import psutil

# The queueing systems whose allocations are recognized, in order. Each names
# the variable that marks a job (``env_names["job_id"]``) in seamm_scheduler.
SCHEDULERS = ("slurm", "pbs")


def scheduler_job_variables():
    """The environment variables that mark a batch job, one per scheduler."""
    try:
        from seamm_scheduler import get_scheduler

        return tuple(get_scheduler(n).env_names["job_id"] for n in SCHEDULERS)
    except ImportError:
        return ("SLURM_JOB_ID", "PBS_JOBID")


def running_scheduler():
    """The name of the queueing system whose allocation this is, or None."""
    for name, variable in zip(SCHEDULERS, scheduler_job_variables()):
        if variable in os.environ:
            return name
    return None


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

    scheduler = running_scheduler()
    if scheduler == "slurm":
        ce = _slurm()
    elif scheduler == "pbs":
        ce = _pbs()
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

    # Without --ntasks SLURM sets no SLURM_NTASKS: fall back to the cores
    # allocated on this node.
    if "NNODES" not in ce:
        ce["NNODES"] = int(ce.get("JOB_NUM_NODES", 1) or 1)
    if "NTASKS" not in ce:
        cpus = ce.get("CPUS_ON_NODE", ce.get("JOB_CPUS_PER_NODE", 1))
        try:
            cpus = int(str(cpus).split("(")[0].split(",")[0])
        except ValueError:
            cpus = 1
        per_task = int(ce.get("CPUS_PER_TASK", 1) or 1)
        ce["NTASKS"] = max(1, cpus // per_task) * int(ce["NNODES"])

    if "NTASKS_PER_NODE" not in ce:
        ce["NTASKS_PER_NODE"] = int(ce["NTASKS"]) // int(ce["NNODES"])

    # Expand the hostlist, e.g. SLURM_NODELIST=tc[053,059-061],tc200
    nodelist = []
    npernode = ce["NTASKS_PER_NODE"]
    nodelist_text = ce.get("NODELIST", ce.get("JOB_NODELIST", "localhost"))
    for node in expand_hostlist(str(nodelist_text)):
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


def _pbs():
    """Get the number of tasks, nodes, etc. for a PBS job.

    PBS exports less than SLURM: the job id, ``NCPUS`` (the cores of this
    node's chunk), ``OMP_NUM_THREADS`` and ``PBS_NODEFILE``, which has one line
    per MPI rank. Memory is not exported, so it is this node's available memory.
    """
    from seamm_scheduler.pbs import Pbs

    names = Pbs.env_names
    if names["job_id"] not in os.environ:
        raise RuntimeError("This does not appear to be a PBS job.")
    ce = {"type": "pbs", "JOB_ID": os.environ[names["job_id"]]}

    hosts = []
    nodefile = os.environ.get(names["nodefile"])
    if nodefile and Path(nodefile).exists():
        hosts = [
            h.strip() for h in Path(nodefile).read_text().splitlines() if h.strip()
        ]
    ncpus = int(os.environ.get(names["ncpus"], "0") or 0)
    threads = int(os.environ.get(names["threads"], "1") or 1)
    if hosts:
        counts = {}
        for host in hosts:
            counts[host] = counts.get(host, 0) + 1
        ce["NTASKS"] = len(hosts)
        ce["NNODES"] = len(counts)
        ce["NTASKS_PER_NODE"] = max(counts.values())
        ce["NODELIST"] = ",".join(f"{h}:{n}" for h, n in counts.items())
    else:
        ce["NTASKS"] = max(1, ncpus // max(1, threads))
        ce["NNODES"] = 1
        ce["NTASKS_PER_NODE"] = ce["NTASKS"]
    ce["CPUS_PER_TASK"] = max(1, threads)
    if names["ngpus"] in os.environ:
        ce["NGPUS"] = int(os.environ[names["ngpus"]] or 0)

    cores_per_node = max(1, ce["NTASKS_PER_NODE"] * ce["CPUS_PER_TASK"])
    available = psutil.virtual_memory().available
    ce["MEM_PER_NODE"] = available
    ce["MEM_PER_CPU"] = available // cores_per_node
    return ce


def expand_hostlist(text):
    """SLURM's compressed hostlist -> host names.

    ``tc[053,059-061],gpu7`` -> ``tc053 tc059 tc060 tc061 gpu7``; zero padding
    is kept; a suffix after the brackets (``tc[01-02]-ib``) and several
    bracket groups (``r[1-2]n[1-2]``, the product) are expanded as SLURM does.
    """
    # Split on the commas that are not inside brackets
    tokens = []
    depth = 0
    current = ""
    for c in text:
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == "," and depth == 0:
            tokens.append(current)
            current = ""
        else:
            current += c
    tokens.append(current)

    hosts = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        names = [""]
        for part in re.split(r"(\[[^\]]*\])", token):
            if part.startswith("[") and part.endswith("]"):
                values = []
                for item in part[1:-1].split(","):
                    if "-" in item:
                        first, last = item.split("-", 1)
                        width = len(first)
                        values.extend(
                            f"{i:0{width}d}" for i in range(int(first), int(last) + 1)
                        )
                    else:
                        values.append(item)
                names = [n + v for n in names for v in values]
            else:
                names = [n + part for n in names]
        hosts.extend(names)
    return hosts
