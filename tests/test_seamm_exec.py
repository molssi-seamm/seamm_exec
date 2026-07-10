"""
Unit and regression test for the seamm_exec package.
"""

# Import package, test suite, and other packages as needed
import sys

import pytest  # noqa: F401

import seamm_exec  # noqa: F401
from seamm_exec.computational_environment import _slurm_normalize_memory

MiB = 1024 * 1024


def test_seamm_exec_imported():
    """Sample test, will always pass so long as import statement worked."""
    assert "seamm_exec" in sys.modules


def test_slurm_memory_per_cpu_to_bytes():
    """--mem-per-cpu=8G sets SLURM_MEM_PER_CPU=8192 (MiB) and no MEM_PER_NODE.
    It must become bytes, and MEM_PER_NODE derived from the cores per node."""
    ce = {"MEM_PER_CPU": 8192, "NTASKS_PER_NODE": 4, "CPUS_PER_TASK": 1}
    _slurm_normalize_memory(ce)
    assert ce["MEM_PER_CPU"] == 8192 * MiB  # 8 GiB per core, in bytes
    assert ce["MEM_PER_NODE"] == 8192 * MiB * 4  # 32 GiB on the node


def test_slurm_memory_per_node_to_bytes():
    """--mem=32G sets SLURM_MEM_PER_NODE=32768 (MiB) and no MEM_PER_CPU."""
    ce = {"MEM_PER_NODE": 32768, "NTASKS_PER_NODE": 4, "CPUS_PER_TASK": 1}
    _slurm_normalize_memory(ce)
    assert ce["MEM_PER_NODE"] == 32768 * MiB
    assert ce["MEM_PER_CPU"] == 8192 * MiB


def test_slurm_memory_accounts_for_cpus_per_task():
    """Cores per node is ntasks-per-node * cpus-per-task."""
    ce = {"MEM_PER_CPU": 4096, "NTASKS_PER_NODE": 2, "CPUS_PER_TASK": 4}
    _slurm_normalize_memory(ce)
    assert ce["MEM_PER_CPU"] == 4096 * MiB
    assert ce["MEM_PER_NODE"] == 4096 * MiB * 8  # 2 * 4 = 8 cores/node


def test_slurm_memory_fallback_when_unset():
    """No SLURM memory request -> fall back to physically available memory."""
    ce = {"NTASKS_PER_NODE": 4, "CPUS_PER_TASK": 1}
    _slurm_normalize_memory(ce)
    assert ce["MEM_PER_NODE"] > 0
    assert ce["MEM_PER_CPU"] == ce["MEM_PER_NODE"] // 4
