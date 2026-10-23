"""Copies of a running task's outputs in its task directory, so that a task
stopped part way -- by the job's time limit, say -- leaves them (MBE, 2026-10-23:
ten large ORCA runs that timed out lost their orca.out with the node scratch)."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

from seamm_exec import partial_outputs
from seamm_exec.partial_outputs import SUFFIX, Copier

from .test_tasks import _driver_env, _wait_for


def test_copies_follow_the_files(tmp_path):
    source, destination = tmp_path / "scratch", tmp_path / "task"
    source.mkdir()
    destination.mkdir()
    out = source / "orca.out"
    out.write_text("SCF done\n")
    (source / "big.trj").write_bytes(b"x" * 2000)
    copier = Copier(
        source, destination, ["orca.out", "*.trj"], every=0.05, max_bytes=1000
    )
    copier.start()
    try:
        assert _wait_for(lambda: (destination / ("orca.out" + SUFFIX)).exists(), 5)
        out.write_text("SCF done\nMP2 done\n")
        assert _wait_for(
            lambda: (destination / ("orca.out" + SUFFIX))
            .read_text()
            .endswith("MP2 done\n"),
            5,
        )
    finally:
        copier.stop()
    # too large to copy
    assert not (destination / ("big.trj" + SUFFIX)).exists()
    # the copies stay after stop(), e.g. when the task failed ...
    assert (destination / ("orca.out" + SUFFIX)).exists()
    # ... and go once the files are in place
    copier.remove()
    assert not list(destination.iterdir())


def test_copy_all_without_periodic_copies(tmp_path):
    source, destination = tmp_path / "scratch", tmp_path / "task"
    source.mkdir()
    destination.mkdir()
    (source / "orca.out").write_text("partial\n")
    copier = Copier(source, destination, ["orca.out"], every=0).start()
    try:
        assert not (destination / ("orca.out" + SUFFIX)).exists()
        partial_outputs.copy_all()
        assert (destination / ("orca.out" + SUFFIX)).read_text() == "partial\n"
    finally:
        copier.stop()


DRIVER = textwrap.dedent("""
    import sys
    from seamm_exec import Local, LocalPool, Resources, Task, TaskSet

    step, sleep = sys.argv[1], sys.argv[2]
    ce = {"NTASKS": 2, "MEM_PER_NODE": 2**34, "MEM_PER_CPU": 2**32}
    ts = TaskSet(directory=step, backend=LocalPool(Local(), ce=ce),
                 executor=Local(), poll_interval=0.1)
    ts.add(Task(key="long", program="sh", config={}, shell=True,
                cmd=[f"echo started > out.log; sleep {sleep}; echo done >> out.log"],
                return_files=["out.log"], resources=Resources(1), in_situ=False))
    for r in ts.run():
        print(r.key, r.state, flush=True)
""")


def test_a_stopped_task_leaves_its_output(tmp_path):
    """SIGTERM to the evaluator (a job's time limit) copies a task's outputs from
    its temporary directory before the task is killed."""
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    step = tmp_path / "step"
    partial = step / "tasks" / "long" / ("out.log" + SUFFIX)
    env = _driver_env()
    env["SEAMM_PARTIAL_INTERVAL"] = "3600"  # only the copy on SIGTERM
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(step), "60"],
        stdout=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    try:
        manifest = step / "tasks" / "manifest.json"
        assert _wait_for(
            lambda: manifest.exists() and '"pgid"' in manifest.read_text(), 20
        )
        time.sleep(1.0)  # the task has written out.log
        assert not partial.exists()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert partial.read_text() == "started\n"


def test_a_finished_task_has_no_copies(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    step = tmp_path / "step"
    env = _driver_env()
    env["SEAMM_PARTIAL_INTERVAL"] = "0.1"
    out = subprocess.run(
        [sys.executable, str(driver), str(step), "1"],
        stdout=subprocess.PIPE,
        universal_newlines=True,
        env=env,
        timeout=60,
        check=True,
    ).stdout
    assert "long finished" in out
    task = step / "tasks" / "long"
    assert (task / "out.log").read_text() == "started\ndone\n"
    assert not list(Path(task).glob("*" + SUFFIX))
    assert os.environ.get("SEAMM_PARTIAL_INTERVAL") is None
