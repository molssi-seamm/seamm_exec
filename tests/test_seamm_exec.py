"""
Unit and regression test for the seamm_exec package.
"""

# Import package, test suite, and other packages as needed
import logging
from pathlib import Path
import sys

import pytest  # noqa: F401

import seamm_exec  # noqa: F401
from seamm_exec.base import Base, _running_under_scheduler
from seamm_exec.computational_environment import _slurm_normalize_memory

MiB = 1024 * 1024


class _FakeExecutor(Base):
    """A minimal Base subclass that records the directory it was asked to
    run in, instead of actually launching anything."""

    def __init__(self):
        super().__init__(logging.getLogger("test-fake-executor"))
        self.ran_in = None

    @property
    def name(self):
        return "fake"

    def exec(
        self,
        config,
        cmd=[],
        directory=None,
        input_data=None,
        env={},
        shell=False,
        ce={},
    ):
        self.ran_in = Path(directory)
        (self.ran_in / "out.txt").write_text("hello")
        return {"returncode": 0, "stdout": "", "stderr": ""}


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


def test_running_under_scheduler(monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    assert not _running_under_scheduler()
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    assert _running_under_scheduler()


def test_auto_in_situ_runs_in_scratch_under_slurm(monkeypatch, tmp_path):
    """molssi-seamm/orca_step#20: with in_situ=None (the new default for
    steps), a SLURM allocation must run in a temp dir (node-local scratch,
    honoring $TMPDIR), not the given (NFS) job directory -- and the
    requested result files must still land back in the job directory."""
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    executor = _FakeExecutor()
    result = executor.run(
        config={}, cmd=["true"], directory=tmp_path, return_files=["out.txt"]
    )
    assert result is not None
    assert executor.ran_in != tmp_path
    assert not executor.ran_in.exists()  # cleaned up afterwards
    assert (tmp_path / "out.txt").read_text() == "hello"


def test_auto_in_situ_runs_in_place_off_scheduler(monkeypatch, tmp_path):
    """Off a scheduler (e.g. an interactive/local run), in_situ=None must
    keep running directly in the given job directory, as before."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    executor = _FakeExecutor()
    result = executor.run(
        config={}, cmd=["true"], directory=tmp_path, return_files=["out.txt"]
    )
    assert result is not None
    assert executor.ran_in == tmp_path
    assert (tmp_path / "out.txt").read_text() == "hello"


def test_explicit_in_situ_overrides_auto_detection(monkeypatch, tmp_path):
    """An explicit True/False must still win over the scheduler auto-detect."""
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    executor = _FakeExecutor()
    executor.run(
        config={},
        cmd=["true"],
        directory=tmp_path,
        return_files=["out.txt"],
        in_situ=True,
    )
    assert executor.ran_in == tmp_path
