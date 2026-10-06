"""Tests of the task layer: TaskSet, LocalPool, the manifest and the worker."""

import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import textwrap
import time

import pytest

import seamm_exec
from seamm_exec import Local
from seamm_exec.base import Base
from seamm_exec.local_pool import LocalPool, BINDING_ENV
from seamm_exec.tasks import WORKER_SCRIPT, Resources, Task, TaskResult, TaskSet

GiB = 1024**3


@pytest.fixture(autouse=True)
def _off_scheduler(monkeypatch):
    """Run as on a workstation: in place, binding untouched."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv(BINDING_ENV, raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)


def pool(cores=4, memory=16 * GiB, executor=None, **kwargs):
    """A LocalPool with a fixed capacity, independent of this machine."""
    ce = {"NTASKS": cores, "MEM_PER_NODE": memory, "MEM_PER_CPU": memory // cores}
    return LocalPool(executor or Local(), ce=ce, **kwargs)


def shell_task(key, command, **kwargs):
    kwargs.setdefault("config", {})
    kwargs.setdefault("return_files", ["*.txt"])
    return Task(key=key, program="sh", cmd=[command], shell=True, **kwargs)


def run_all(task_set):
    return {r.key: r for r in task_set.run()}


# ----------------------------------------------------------------------
# Base.run() is unchanged
# ----------------------------------------------------------------------
def test_base_run_result_and_no_new_files(tmp_path):
    """The shim returns the old dictionary and writes no task bookkeeping."""
    result = Local().run(
        config={},
        cmd=["echo hello > out.txt; echo said-it; echo oops >&2; touch junk.dat"],
        directory=tmp_path,
        return_files=["out.txt"],
        shell=True,
    )
    assert result["returncode"] == 0
    assert result["stdout"] == "said-it\n"
    assert result["stderr"] == "oops\n"
    assert result["in_situ"] is True
    assert result["directory"] == str(tmp_path)
    assert result["files"] == ["out.txt"]
    assert result["out.txt"] == {"data": "hello\n", "exception": None}
    assert "listing" in result
    # In situ: files not returned are removed; stdout/stderr written.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "out.txt",
        "stderr.txt",
        "stdout.txt",
    ]
    assert not (tmp_path / "tasks").exists()


def test_base_run_propagates_exceptions(tmp_path):
    class Exploding(Base):
        def __init__(self):
            super().__init__(logging.getLogger("test"))

        @property
        def name(self):
            return "exploding"

        def exec(self, config, **kwargs):
            raise KeyError("boom")

    with pytest.raises(KeyError):
        Exploding().run(config={}, cmd=["true"], directory=tmp_path)


def test_base_run_subdir_return(tmp_path):
    """'@subdir+name' still moves a returned file into a subdirectory."""
    Local().run(
        config={},
        cmd=["echo x > '@sub+a.txt'"],
        directory=tmp_path,
        return_files=["@sub+a.txt"],
        shell=True,
        in_situ=False,
    )
    assert (tmp_path / "sub" / "a.txt").read_text() == "x\n"


# ----------------------------------------------------------------------
# LocalPool: concurrency and slots
# ----------------------------------------------------------------------
def test_tasks_run_concurrently(tmp_path):
    """Four one-core tasks in a four-core pool all run at the same time.

    Judged by their running intervals overlapping, not by the total elapsed
    time, which a busy CI machine stretches."""
    stamp = f"{sys.executable} -c 'import time; print(time.time())'"
    ts = TaskSet(directory=tmp_path, backend=pool(cores=4), executor=Local())
    for i in range(4):
        ts.add(
            shell_task(
                f"t{i}",
                f"{stamp} > start.txt; sleep 2; {stamp} > end.txt; "
                "echo {NTASKS} > n.txt",
                resources=Resources(1),
            )
        )
    results = run_all(ts)
    starts, ends = [], []
    for i in range(4):
        directory = tmp_path / "tasks" / f"t{i}"
        starts.append(float((directory / "start.txt").read_text()))
        ends.append(float((directory / "end.txt").read_text()))
    assert max(starts) < min(ends), f"not all running at once: {starts} {ends}"
    for i in range(4):
        r = results[f"t{i}"]
        assert r.ok and r.returncode == 0 and r.attempts == 1
        assert r.files["n.txt"] == "1\n"
        assert r.directory == tmp_path / "tasks" / f"t{i}"
        assert (tmp_path / "tasks" / f"t{i}" / "DONE").exists()
    manifest = json.loads((tmp_path / "tasks" / "manifest.json").read_text())
    assert {k: v["state"] for k, v in manifest["tasks"].items()} == {
        f"t{i}": "finished" for i in range(4)
    }
    assert ts.summary() == {"total": 4, "finished": 4}


def test_slots_partition_the_pool(tmp_path):
    """Two-core tasks in a four-core pool: never more than two at once."""
    ts = TaskSet(directory=tmp_path, backend=pool(cores=4), executor=Local())
    for i in range(5):
        ts.add(
            shell_task(
                f"t{i}",
                "python3 -c 'import time; print(time.time())' > start.txt; sleep 0.4;"
                " python3 -c 'import time; print(time.time())' > end.txt;"
                " echo {NTASKS} {MEM_PER_CPU} > ce.txt",
                resources=Resources(ntasks=2, mem_per_cpu=GiB),
            )
        )
    results = run_all(ts)
    spans = [
        (float(r.files["start.txt"]), float(r.files["end.txt"]))
        for r in results.values()
    ]
    for start, _ in spans:
        overlap = sum(1 for s, e in spans if s <= start < e)
        assert overlap <= 2
    assert all(r.files["ce.txt"] == f"2 {GiB}\n" for r in results.values())


def test_whole_pool_and_oversized_tasks(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(cores=4), executor=Local())
    ts.add(shell_task("all", "echo {NTASKS} > n.txt"))  # ntasks=None
    ts.add(shell_task("big", "echo {NTASKS} > n.txt", resources=Resources(ntasks=64)))
    results = run_all(ts)
    assert results["all"].files["n.txt"] == "4\n"
    assert results["big"].files["n.txt"] == "4\n"  # clamped to the pool
    assert ts.capacity() == {"cores": 4, "memory": 16 * GiB, "ngpus": 0}


def test_binding_disabled_only_when_concurrent(tmp_path):
    cmd = f"echo ${BINDING_ENV}:$OMP_NUM_THREADS > b.txt"
    one = TaskSet(directory=tmp_path / "one", backend=pool(), executor=Local())
    one.add(shell_task("a", cmd))
    assert run_all(one)["a"].files["b.txt"] == ":\n"  # untouched

    many = TaskSet(directory=tmp_path / "many", backend=pool(), executor=Local())
    many.add(shell_task("a", cmd, resources=Resources(1, cpus_per_task=2)))
    many.add(shell_task("b", cmd, env={BINDING_ENV: "core"}, resources=Resources(1)))
    results = run_all(many)
    assert results["a"].files["b.txt"] == "none:2\n"
    assert results["b"].files["b.txt"] == "core:1\n"  # the task's own setting wins


def test_code_dir_placeholder(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    ts.add(
        shell_task("a", "echo {code_dir} > d.txt", config={"code": "/opt/orca/orca"})
    )
    assert run_all(ts)["a"].files["d.txt"] == "/opt/orca\n"


def test_config_from_ini(tmp_path):
    (tmp_path / "fake.ini").write_text("[local]\ninstallation = local\ncode = echo\n")
    ts = TaskSet(
        directory=tmp_path / "step",
        backend=pool(root=tmp_path),
        executor=Local(),
    )
    ts.add(
        Task(
            key="a",
            program="fake",
            cmd=["{code}", "hi", ">", "o.txt"],
            shell=True,
            return_files=["o.txt"],
        )
    )
    assert run_all(ts)["a"].files["o.txt"] == "hi\n"


# ----------------------------------------------------------------------
# Task directories and in-situ pruning
# ----------------------------------------------------------------------
def test_task_in_step_directory_keeps_bookkeeping(tmp_path):
    """A single task run in place in the step directory (ORCA, MOPAC)."""
    (tmp_path / "earlier.txt").write_text("kept")
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    ts.add(
        shell_task(
            "orca",
            "echo out > orca.out; echo junk > scratch.tmp",
            directory=tmp_path,
            in_situ=True,
            return_files=["orca.out"],
        )
    )
    result = run_all(ts)["orca"]
    assert result.ok
    assert result.directory == tmp_path
    assert (tmp_path / "orca.out").read_text() == "out\n"
    assert not (tmp_path / "scratch.tmp").exists()
    assert (tmp_path / "earlier.txt").exists()
    assert (tmp_path / "tasks" / "orca" / "DONE").exists()
    assert (tmp_path / "tasks" / "manifest.json").exists()


def test_concurrent_in_situ_prunes_each_task(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    for i in range(3):
        ts.add(
            shell_task(
                f"t{i}",
                "echo a > keep.txt; echo b > drop.dat; sleep 0.2",
                in_situ=True,
                resources=Resources(1),
            )
        )
    run_all(ts)
    for i in range(3):
        d = tmp_path / "tasks" / f"t{i}"
        assert (d / "keep.txt").exists()
        assert not (d / "drop.dat").exists()
        assert (d / "DONE").exists()


def test_scratch_tasks_copy_back(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    for i in range(2):
        ts.add(
            shell_task(
                f"t{i}",
                "echo a > keep.txt; echo b > drop.dat",
                in_situ=False,
                resources=Resources(1),
            )
        )
    results = run_all(ts)
    for i in range(2):
        d = tmp_path / "tasks" / f"t{i}"
        assert (d / "keep.txt").read_text() == "a\n"
        assert not (d / "drop.dat").exists()
        assert results[f"t{i}"].in_situ is False
        assert results[f"t{i}"].run_directory != str(d)


def test_bad_keys_and_duplicates(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    with pytest.raises(ValueError):
        ts.add(shell_task("a/b", "true"))
    ts.add(shell_task("a", "true", directory=tmp_path / "x"))
    with pytest.raises(ValueError):
        ts.add(shell_task("a", "true"))
    with pytest.raises(ValueError):
        ts.add(shell_task("b", "true", directory=tmp_path / "x"))


# ----------------------------------------------------------------------
# Restart
# ----------------------------------------------------------------------
def counting_tasks(tmp_path, n=3, command="echo {key} > r.txt"):
    """Tasks that append to a counter outside the step, to see what ran."""
    counter = tmp_path / "ran.log"
    ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
    for i in range(n):
        ts.add(
            shell_task(
                f"t{i}",
                f"echo t{i} >> {counter}; " + command.format(key=f"t{i}"),
                resources=Resources(1),
            )
        )
    return ts, counter


def test_finished_tasks_are_not_rerun(tmp_path):
    ts, counter = counting_tasks(tmp_path)
    first = run_all(ts)
    assert all(not r.restored for r in first.values())
    assert len(counter.read_text().split()) == 3

    ts, counter = counting_tasks(tmp_path)
    second = run_all(ts)
    assert len(counter.read_text().split()) == 3  # nothing ran again
    for key, r in second.items():
        assert r.restored and r.ok
        assert r.files == first[key].files
        assert r.attempts == 1


def test_changed_inputs_are_rerun(tmp_path, caplog):
    ts, counter = counting_tasks(tmp_path)
    run_all(ts)
    ts, counter = counting_tasks(tmp_path, command="echo {key} changed > r.txt")
    results = run_all(ts)
    assert len(counter.read_text().split()) == 6
    assert results["t0"].files["r.txt"] == "t0 changed\n"
    assert "different inputs" in caplog.text


def test_fingerprint_overrides_hash(tmp_path):
    """Run-dependent text (e.g. %pal) must not force a rerun."""
    counter = tmp_path / "ran.log"

    def make(ncores):
        ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
        ts.add(
            shell_task(
                "a",
                f"echo a >> {counter}",
                files={"orca.inp": f"%pal nprocs {ncores} end\n"},
                fingerprint="water-dimer-0001",
            )
        )
        return ts

    run_all(make(4))
    assert run_all(make(8))["a"].restored
    assert counter.read_text() == "a\n"


def test_env_does_not_affect_restart(tmp_path):
    counter = tmp_path / "ran.log"
    for path in ("/a", "/b"):
        ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
        ts.add(shell_task("a", f"echo a >> {counter}", env={"SOMEPATH": path}))
        run_all(ts)
    assert counter.read_text() == "a\n"


def test_failed_tasks_retry_across_runs_up_to_the_cap(tmp_path):
    counter = tmp_path / "ran.log"

    def make():
        ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
        ts.add(shell_task("bad", f"echo x >> {counter}; exit 3"))
        return ts

    r = run_all(make())["bad"]
    assert r.state == "failed" and r.returncode == 3 and r.attempts == 1
    assert not (tmp_path / "step" / "tasks" / "bad" / "DONE").exists()
    assert len(counter.read_text().split()) == 1  # never retried within a run

    assert run_all(make())["bad"].attempts == 2
    assert run_all(make())["bad"].attempts == 3
    r = run_all(make())["bad"]  # past the cap: reported, not run
    assert r.state == "failed" and r.attempts == 3
    assert r.reason.startswith("attempts exhausted")
    assert "manifest.json" in r.reason
    assert [h["returncode"] for h in r.history] == [3, 3, 3]
    assert {h["reason"] for h in r.history} == {"return code 3"}
    assert len(counter.read_text().split()) == 3


DRIVER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from seamm_exec import Local, LocalPool, Resources, Task, TaskSet

    step, sleep_slow = sys.argv[1], sys.argv[2]
    log = Path(step).parent / "ran.log"
    ce = {"NTASKS": 4, "MEM_PER_NODE": 2**34, "MEM_PER_CPU": 2**32}
    ts = TaskSet(directory=step, backend=LocalPool(Local(), ce=ce),
                 executor=Local(), poll_interval=0.1)
    for i in range(3):
        ts.add(Task(key=f"fast{i}", program="sh", config={}, shell=True,
                    cmd=[f"echo fast{i} >> {log}; echo ok > r.txt"],
                    return_files=["r.txt"], resources=Resources(1)))
    ts.add(Task(key="slow", program="sh", config={}, shell=True,
                cmd=[f"echo slow >> {log}; sleep $SLOW; echo ok > r.txt"],
                env={"SLOW": sleep_slow},
                return_files=["r.txt"], resources=Resources(1)))
    for r in ts.run():
        print(r.key, r.state, r.restored, flush=True)
""")


def _driver_env():
    """Make the driver import this checkout of seamm_exec, not an installed one."""
    env = dict(os.environ)
    source = str(Path(seamm_exec.__file__).parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (source, env.get("PYTHONPATH")) if p)
    return env


def _wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _alive(pgid):
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_killed_evaluator_resumes(tmp_path, sig):
    """Kill the evaluator mid-run: the rerun recomputes nothing finished."""
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    step = tmp_path / "step"
    manifest = step / "tasks" / "manifest.json"

    def slow_pgid():
        try:
            record = json.loads(manifest.read_text())["tasks"]["slow"]
        except Exception:
            return None
        return record.get("pgid")

    proc = subprocess.Popen(
        [sys.executable, str(driver), str(step), "60"],
        stdout=subprocess.PIPE,
        universal_newlines=True,
        env=_driver_env(),
    )
    try:
        assert _wait_for(
            lambda: all(
                (step / "tasks" / f"fast{i}" / "DONE").exists() for i in range(3)
            )
            and slow_pgid() is not None
        )
        pgid = slow_pgid()
        assert _alive(pgid)
        proc.send_signal(sig)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()

    if sig == signal.SIGTERM:
        # The evaluator's handler takes its tasks with it.
        assert _wait_for(lambda: not _alive(pgid), timeout=10)
    else:
        assert _alive(pgid)  # a SIGKILL leaves the task running ...

    out = subprocess.run(
        [sys.executable, str(driver), str(step), "0"],
        stdout=subprocess.PIPE,
        universal_newlines=True,
        timeout=60,
        check=True,
        env=_driver_env(),
    ).stdout
    assert not _alive(pgid)  # ... until the rerun kills it
    lines = sorted(out.split("\n")[:-1])
    assert lines == [
        "fast0 finished True",
        "fast1 finished True",
        "fast2 finished True",
        "slow finished False",
    ]
    ran = (tmp_path / "ran.log").read_text().split()
    assert sorted(ran) == ["fast0", "fast1", "fast2", "slow", "slow"]
    record = json.loads(manifest.read_text())["tasks"]["slow"]
    # Stopped because the evaluator was killed: not one of the task's attempts.
    assert record["attempts"] == 1
    assert [h["state"] for h in record["history"]] == ["lost", "finished"]
    assert [h["counted"] for h in record["history"]] == [False, True]


def test_evaluator_kills_do_not_use_up_attempts(tmp_path):
    """A task cut short by the evaluator stopping, more times than max_attempts,
    still runs when the job is resumed."""
    log = tmp_path / "ran.log"

    def make():
        ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
        ts.add(shell_task("a", f"echo a >> {log}"))
        return ts

    manifest = tmp_path / "step" / "tasks" / "manifest.json"
    for _ in range(4):
        # What a killed evaluator leaves: the task recorded as running here,
        # its process gone, one more attempt counted when it was submitted.
        ts = make()
        ts.tasks_directory.mkdir(parents=True, exist_ok=True)
        data = json.loads(manifest.read_text()) if manifest.exists() else None
        record = {} if data is None else data["tasks"].get("a", {})
        ts.manifest.update(
            "a",
            backend="local",
            state="running",
            fingerprint=ts.tasks["a"].digest(),
            attempts=record.get("attempts", 0) + 1,
            history=record.get("history", []),
            pgid=None,
        )
        ts.manifest.flush(force=True)
        ts._reattach([ts.tasks["a"]], {})
        ts.manifest.flush(force=True)
    record = json.loads(manifest.read_text())["tasks"]["a"]
    assert record["attempts"] == 0
    assert len(record["history"]) == 4

    result = run_all(make())["a"]
    assert result.state == "finished"
    assert log.read_text() == "a\n"


# ----------------------------------------------------------------------
# Archiving
# ----------------------------------------------------------------------
def test_archive_one_tar_per_bundle(tmp_path):
    def make():
        ts = TaskSet(
            directory=tmp_path,
            backend=pool(),
            executor=Local(),
            archive=True,
            bundle_tasks=2,
        )
        for i in range(5):
            ts.add(shell_task(f"t{i}", f"echo {i} > r.txt", resources=Resources(1)))
        return ts

    first = run_all(make())
    tasks = tmp_path / "tasks"
    assert sorted(p.name for p in tasks.iterdir()) == [
        "bundle_0000.tar",
        "bundle_0001.tar",
        "bundle_0002.tar",
        "manifest.json",
    ]
    with tarfile.open(tasks / "bundle_0000.tar") as tar:
        names = tar.getnames()
    assert "t0/DONE" in names and "t1/r.txt" in names
    assert first["t4"].files["r.txt"] == "4\n"

    second = run_all(make())  # restored from the tars
    for i in range(5):
        r = second[f"t{i}"]
        assert r.restored and r.files["r.txt"] == f"{i}\n"
        assert r.archive is not None


# ----------------------------------------------------------------------
# The inline rule
# ----------------------------------------------------------------------
class FakeRemote:
    """A stand-in for a scheduler back end."""

    name = "fake-remote"

    def __init__(self):
        self.submitted = []
        self.dirs = {}

    def submit(self, tasks, directories, on_start=None):
        ids = []
        for task, directory in zip(tasks, directories):
            self.submitted.append(task.key)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "remote.txt").write_text("remote\n")
            ids.append(f"r-{task.key}")
            self.dirs[ids[-1]] = directory
        return ids

    def status(self, ids):
        return {i: "finished" for i in ids}

    def cancel(self, ids):
        pass

    def fetch(self, task, backend_id):
        return TaskResult(
            key=task.key,
            state="finished",
            returncode=0,
            directory=self.dirs[backend_id],
            files={"remote.txt": "remote\n"},
        )


def test_inline_rule(tmp_path):
    (tmp_path / "mopac.ini").write_text("[local]\ninstallation = local\ncode = echo\n")
    remote = FakeRemote()
    ts = TaskSet(
        directory=tmp_path / "step",
        backend=remote,
        local=pool(root=tmp_path),
        executor=Local(),
        inline_below=60,
    )
    ts.add(shell_task("tiny", "echo local > l.txt", estimated_seconds=0.1))
    ts.add(shell_task("huge", "echo local > l.txt", estimated_seconds=3600))
    ts.add(shell_task("unknown", "echo local > l.txt"))
    ts.add(  # tiny, but its program is not installed here
        Task(key="absent", program="vasp", cmd=["x"], estimated_seconds=0.1)
    )
    ts.add(  # tiny, and installed here through its ini file
        Task(
            key="mopac",
            program="mopac",
            cmd=["{code} m > m.txt"],
            shell=True,
            return_files=["m.txt"],
            estimated_seconds=0.1,
        )
    )
    results = run_all(ts)
    assert sorted(remote.submitted) == ["absent", "huge", "unknown"]
    assert results["tiny"].files == {"l.txt": "local\n"}
    assert results["mopac"].files == {"m.txt": "m\n"}
    manifest = json.loads((tmp_path / "step" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["tiny"]["backend"] == "local"
    assert manifest["tasks"]["huge"]["backend"] == "fake-remote"


# ----------------------------------------------------------------------
# The bundle worker
# ----------------------------------------------------------------------
def test_worker_runs_a_bundle_and_skips_done(tmp_path):
    counter = tmp_path / "ran.log"
    bundle = {
        "tasks": [
            {
                "key": "good",
                "directory": str(tmp_path / "good"),
                "command": f"echo good >> {counter}; echo $X > x.txt",
                "shell": True,
                "env": {"X": "42"},
                "fingerprint": "f-good",
            },
            {
                "key": "bad",
                "directory": str(tmp_path / "bad"),
                "command": f"echo bad >> {counter}; exit 5",
                "shell": True,
            },
            {
                "key": "argv",
                "directory": str(tmp_path / "argv"),
                "command": "echo plain",
                "input": None,
            },
        ]
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    cmd = [sys.executable, str(WORKER_SCRIPT), str(path)]
    p = subprocess.run(cmd, env={"PATH": os.environ["PATH"]})
    assert p.returncode == 1
    assert (tmp_path / "good" / "x.txt").read_text() == "42\n"
    done = json.loads((tmp_path / "good" / "DONE").read_text())
    assert done["fingerprint"] == "f-good" and done["returncode"] == 0
    assert (tmp_path / "bad" / "returncode").read_text() == "5\n"
    assert not (tmp_path / "bad" / "DONE").exists()
    assert (tmp_path / "argv" / "stdout.txt").read_text() == "plain\n"

    subprocess.run(cmd)
    assert counter.read_text().split() == ["good", "bad", "bad"]


def test_worker_imports_nothing_from_seamm():
    text = Path(WORKER_SCRIPT).read_text()
    assert "import seamm" not in text and "from seamm" not in text
    assert "from ." not in text


def test_broken_executor_fails_the_task_instead_of_hanging(tmp_path):
    """An executor without _run_task (e.g. a test double) must not hang."""

    class Broken:
        name = "local"

    ts = TaskSet(directory=tmp_path, backend=pool(executor=Broken()), executor=Broken())
    ts.add(shell_task("a", "true"))
    ts.add(shell_task("b", "true"))
    results = run_all(ts)
    assert results["a"].state == "failed" and results["a"].returncode is None
    assert "_run_task" in results["a"].stderr


def test_run_task_with_a_node(tmp_path):
    """run_task takes the directory, executor and root from the node."""
    from types import SimpleNamespace

    from seamm_exec import run_task

    node = SimpleNamespace(
        directory=str(tmp_path),
        flowchart=SimpleNamespace(executor=Local()),
        global_options={"root": str(tmp_path)},
    )
    (tmp_path / "echo.ini").write_text("[local]\ninstallation = local\ncode = echo\n")
    task = Task(
        key="one",
        program="echo",
        cmd=["{code} hi > o.txt"],
        shell=True,
        directory=tmp_path,
        return_files=["o.txt"],
    )
    result = run_task(task, node=node)
    assert result.ok and result.files == {"o.txt": "hi\n"}
    assert (tmp_path / "o.txt").exists()
    assert (tmp_path / "tasks" / "one" / "DONE").exists()
    assert run_task(task, node=node).restored


def test_success_text_catches_a_zero_exit_failure(tmp_path):
    """ORCA exits 0 after an error termination; success_text catches it."""
    counter = tmp_path / "ran.log"

    def make(text):
        ts = TaskSet(directory=tmp_path / "step", backend=pool(), executor=Local())
        ts.add(
            shell_task(
                "orca",
                f"echo x >> {counter}; echo '{text}' > orca.txt",
                success_text={"orca.txt": "TERMINATED NORMALLY"},
            )
        )
        return ts

    r = run_all(make("error termination"))["orca"]
    assert r.state == "failed" and r.returncode == 0
    assert "TERMINATED NORMALLY" in r.stderr
    assert r.reason == "success check: 'TERMINATED NORMALLY' is not in orca.txt"
    manifest = json.loads((tmp_path / "step" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["orca"]["history"][0]["reason"] == r.reason
    assert not (tmp_path / "step" / "tasks" / "orca" / "DONE").exists()
    r = run_all(make("ORCA TERMINATED NORMALLY"))["orca"]  # new input: a fresh start
    assert r.ok and r.attempts == 1
    assert run_all(make("ORCA TERMINATED NORMALLY"))["orca"].restored
    assert len(counter.read_text().split()) == 2


def test_worker_applies_success_text(tmp_path):
    bundle = {
        "tasks": [
            {
                "key": k,
                "directory": str(tmp_path / k),
                "command": f"echo '{text}' > out.txt",
                "shell": True,
                "success_text": {"out.txt": "NORMALLY"},
            }
            for k, text in (("good", "ENDED NORMALLY"), ("bad", "error"))
        ]
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    p = subprocess.run([sys.executable, str(WORKER_SCRIPT), str(path)])
    assert p.returncode == 1
    assert (tmp_path / "good" / "DONE").exists()
    assert not (tmp_path / "bad" / "DONE").exists()


def test_success_text_list_needs_every_text(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
    check = {"o.txt": ["first", "second"]}
    ts.add(shell_task("both", "echo first second > o.txt", success_text=check))
    ts.add(shell_task("one", "echo first > o.txt", success_text=check))
    ts.add(shell_task("none", "true", success_text=check, return_files=[]))
    results = run_all(ts)
    assert results["both"].ok
    assert results["one"].reason == "success check: 'second' is not in o.txt"
    assert results["none"].reason == "success check: o.txt is missing"


# ----------------------------------------------------------------------
# Review fixes (2026-10-02)
# ----------------------------------------------------------------------
class _RecordingExecutor(Base):
    """Records what exec() is given; writes out.txt."""

    def __init__(self):
        super().__init__(logging.getLogger("test-recording"))
        self.calls = []

    @property
    def name(self):
        return "recording"

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
        self.calls.append(
            {"config": config, "env": env, "ce": ce, "directory": Path(directory)}
        )
        (Path(directory) / "out.txt").write_text("result")
        (Path(directory) / "junk.dat").write_text("junk")
        return {"returncode": 0, "stdout": "", "stderr": ""}


def test_shim_under_slurm_uses_tmpdir_and_copies_back(tmp_path, monkeypatch):
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    job = tmp_path / "job"
    job.mkdir()
    executor = _RecordingExecutor()
    result = executor.run(
        config={},
        cmd=["x"],
        directory=job,
        files={"in.inp": "input"},
        return_files=["out.txt"],
    )
    ran_in = executor.calls[0]["directory"]
    assert ran_in.parent == scratch and not ran_in.exists()
    assert result["in_situ"] is False
    assert sorted(p.name for p in job.iterdir()) == ["in.inp", "out.txt"]
    assert (job / "in.inp").read_text() == "input"  # inputs written here too


def test_shim_passes_config_env_and_ce_through(tmp_path):
    executor = _RecordingExecutor()
    config = {"code": "x"}
    env = {"A": "1"}
    ce = {"NTASKS": "not-a-number", "ODD": object()}
    executor.run(config=config, cmd=["x"], directory=tmp_path, env=env, ce=ce)
    call = executor.calls[0]
    assert call["config"] is config and call["env"] is env and call["ce"] is ce


class FlakyRemote(FakeRemote):
    """Loses each task the first ``losses`` times it is submitted."""

    name = "flaky"

    def __init__(self, losses):
        super().__init__()
        self.losses = losses
        self.counts = {}

    def status(self, ids):
        states = {}
        for i in ids:
            key = i[2:].rsplit("#", 1)[0]
            states[i] = "lost" if self.counts[key] <= self.losses else "finished"
        return states

    def submit(self, tasks, directories, on_start=None):
        ids = []
        for task, directory in zip(tasks, directories):
            self.counts[task.key] = self.counts.get(task.key, 0) + 1
            ids.append(f"r-{task.key}#{self.counts[task.key]}")
            self.dirs[ids[-1]] = directory
        return ids


def test_lost_tasks_are_retried_within_a_run(tmp_path):
    remote = FlakyRemote(losses=2)
    ts = TaskSet(
        directory=tmp_path, backend=remote, executor=Local(), poll_interval=0.01
    )
    ts.add(shell_task("a", "true"))
    r = run_all(ts)["a"]
    assert r.ok and r.attempts == 3 and remote.counts["a"] == 3
    assert [h["state"] for h in r.history] == ["lost", "lost", "finished"]

    remote = FlakyRemote(losses=5)
    ts = TaskSet(
        directory=tmp_path / "b", backend=remote, executor=Local(), poll_interval=0.01
    )
    ts.add(shell_task("a", "true"))
    r = run_all(ts)["a"]
    assert r.state == "lost" and remote.counts["a"] == 3  # 1 + 2 retries


def test_reattach_kills_only_the_recorded_process(tmp_path):
    import psutil

    p = subprocess.Popen(["sleep", "30"], start_new_session=True, cwd=tmp_path)
    try:
        leader = psutil.Process(p.pid)
        record = {
            "key": "a",
            "pgid": p.pid,
            "host": pool()._host,
            "create_time": leader.create_time(),
            "cwd": leader.cwd(),
        }
        # A reused pid: a different start time, or another directory
        for changed in (
            {"create_time": leader.create_time() - 100},
            {"cwd": "/somewhere/else"},
            {"create_time": None},
        ):
            assert pool().reattach([{**record, **changed}]) == {"a": "lost"}
            assert p.poll() is None, changed
        assert pool().reattach([record]) == {"a": "lost"}
        assert p.wait(timeout=10) is not None
    finally:
        if p.poll() is None:
            p.kill()


def test_start_is_recorded_before_any_throttled_flush(tmp_path):
    """Kill the evaluator just after a task starts: the rerun must find it."""
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent("""
        import sys
        from seamm_exec import Local, LocalPool, Task, TaskSet
        ce = {"NTASKS": 2, "MEM_PER_NODE": 2**33, "MEM_PER_CPU": 2**32}
        # A long poll interval, so the main loop never flushes the manifest
        ts = TaskSet(directory=sys.argv[1], backend=LocalPool(Local(), ce=ce),
                     executor=Local(), poll_interval=60)
        ts.add(Task(key="slow", program="sh", config={}, shell=True,
                    cmd=["touch started; sleep $SLOW"], env={"SLOW": sys.argv[2]}))
        for r in ts.run():
            print(r.key, r.state, flush=True)
    """))
    step = tmp_path / "step"
    proc = subprocess.Popen(
        [sys.executable, str(driver), str(step), "60"], env=_driver_env()
    )
    try:
        assert _wait_for(lambda: (step / "tasks" / "slow" / "started").exists())
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    record = json.loads((step / "tasks" / "manifest.json").read_text())["tasks"]["slow"]
    assert record["state"] == "running" and record["pgid"]
    pgid = record["pgid"]
    assert _alive(pgid)
    subprocess.run(
        [sys.executable, str(driver), str(step), "0"],
        check=True,
        timeout=60,
        env=_driver_env(),
    )
    assert not _alive(pgid)


def test_each_submit_reports_to_its_own_callback(tmp_path):
    shared = pool()
    seen = {"one": [], "two": []}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ids1 = shared.submit(
        [shell_task("a", "true")],
        [tmp_path / "a"],
        on_start=lambda t, info: seen["one"].append(t.key),
    )
    ids2 = shared.submit(
        [shell_task("b", "true")],
        [tmp_path / "b"],
        on_start=lambda t, info: seen["two"].append(t.key),
    )
    for i in ids1 + ids2:
        while shared.status([i])[i] not in ("finished", "failed"):
            shared.wait([i], timeout=1)
    assert seen == {"one": ["a"], "two": ["b"]}


def test_code_dir_only_for_a_path():
    p = pool()
    assert p.config_for(Task(key="a", program="x", config={"code": "orca"})) == {
        "code": "orca"
    }
    config = p.config_for(Task(key="a", program="x", config={"code": "~/o/orca"}))
    assert config["code_dir"] == str(Path("~/o").expanduser())


def test_changed_input_resets_the_attempts(tmp_path):
    def make(command):
        ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local())
        ts.add(shell_task("a", command))
        return ts

    for _ in range(3):
        run_all(make("exit 1"))
    assert run_all(make("exit 1"))["a"].reason.startswith("attempts exhausted")
    r = run_all(make("echo fixed > f.txt"))["a"]
    assert r.ok and r.attempts == 1
    record = json.loads((tmp_path / "tasks" / "manifest.json").read_text())
    previous = record["tasks"]["a"]["previous"]
    assert previous[0]["attempts"] == 3 and len(previous[0]["history"]) == 3


def test_an_ignored_sighup_stays_ignored(tmp_path):
    script = tmp_path / "s.py"
    script.write_text(textwrap.dedent("""
        import signal, sys
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as under nohup
        from seamm_exec import Local, LocalPool, Task, TaskSet
        from seamm_exec.local_pool import _on_signal
        ce = {"NTASKS": 2, "MEM_PER_NODE": 2**33, "MEM_PER_CPU": 2**32}
        ts = TaskSet(directory=sys.argv[1], backend=LocalPool(Local(), ce=ce),
                     executor=Local())
        for k in "ab":
            ts.add(Task(key=k, program="sh", config={}, shell=True, cmd=["true"]))
        list(ts.run())
        print(signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,
              signal.getsignal(signal.SIGTERM) is _on_signal)
    """))
    out = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "step")],
        check=True,
        stdout=subprocess.PIPE,
        universal_newlines=True,
        env=_driver_env(),
    ).stdout
    assert out.split() == ["True", "True"]


def test_failed_tasks_are_not_archived(tmp_path):
    ts = TaskSet(directory=tmp_path, backend=pool(), executor=Local(), archive=True)
    ts.add(shell_task("good", "echo g > g.txt"))
    ts.add(shell_task("bad", "exit 2"))
    run_all(ts)
    with tarfile.open(tmp_path / "tasks" / "bundle_0000.tar") as tar:
        names = {n.split("/")[0] for n in tar.getnames()}
    assert names == {"good"}
    assert (tmp_path / "tasks" / "bad").is_dir()


def test_inline_only_if_the_task_fits(tmp_path):
    """A cheap task needing more cores than the evaluator has goes to the back
    end: its input may already say how many ranks to use (seamm_exec#38)."""
    remote = FakeRemote()
    ts = TaskSet(
        directory=tmp_path / "step",
        backend=remote,
        local=pool(cores=1, root=tmp_path),
        executor=Local(),
        inline_below=60,
    )
    ts.add(
        shell_task(
            "four",
            "echo x > x.txt",
            estimated_seconds=0.1,
            resources=Resources(ntasks=4),
        )
    )
    ts.add(
        shell_task(
            "one",
            "echo x > x.txt",
            estimated_seconds=0.1,
            resources=Resources(ntasks=1),
        )
    )
    ts.add(shell_task("any", "echo x > x.txt", estimated_seconds=0.1))
    results = run_all(ts)
    assert remote.submitted == ["four"]
    assert results["one"].files == {"x.txt": "x\n"}
    assert results["any"].files == {"x.txt": "x\n"}


# ----------------------------------------------------------------------
# Cancelling a running task set from another thread
# ----------------------------------------------------------------------
class SlowRemote(FakeRemote):
    """A stand-in whose tasks run until cancelled."""

    def __init__(self):
        super().__init__()
        self.cancelled = []
        self.poll_interval = 0.05

    def status(self, ids):
        return {i: ("cancelled" if i in self.cancelled else "running") for i in ids}

    def cancel(self, ids):
        self.cancelled.extend(ids)

    def fetch(self, task, backend_id):
        return TaskResult(
            key=task.key, state="cancelled", directory=self.dirs[backend_id]
        )


def test_cancel_from_another_thread(tmp_path):
    import threading

    remote = SlowRemote()
    ts = TaskSet(
        directory=tmp_path / "step",
        backend=remote,
        local=pool(root=tmp_path),
        executor=Local(),
        inline_below=0,
        poll_interval=0.05,
    )
    for i in range(3):
        ts.add(shell_task(f"t{i}", "echo x", estimated_seconds=3600))
    results = []
    worker = threading.Thread(target=lambda: results.extend(ts.run()))
    worker.start()
    # Let it submit and poll a little, then cancel
    deadline = time.time() + 5
    while len(remote.submitted) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert len(remote.submitted) == 3
    t0 = time.time()
    ts.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive(), "run() did not return after cancel()"
    assert time.time() - t0 < 5
    assert ts.cancelled
    assert sorted(r.key for r in results) == ["t0", "t1", "t2"]
    assert all(r.state == "cancelled" and r.reason == "cancelled" for r in results)
    assert sorted(remote.cancelled) == ["r-t0", "r-t1", "r-t2"]
    for i in range(3):
        assert ts.manifest.get(f"t{i}")["state"] == "cancelled"
    # A new run of the same step submits them afresh (nothing is DONE)
    ts2 = TaskSet(
        directory=tmp_path / "step",
        backend=FakeRemote(),
        local=pool(root=tmp_path),
        executor=Local(),
        inline_below=0,
    )
    for i in range(3):
        ts2.add(shell_task(f"t{i}", "echo x", estimated_seconds=3600))
    again = list(ts2.run())
    assert all(r.state == "finished" and not r.restored for r in again)


def test_cancel_kills_local_tasks(tmp_path):
    """A task running in the local pool is killed and the wait wakes at once."""
    import threading

    ts = TaskSet(
        directory=tmp_path / "step",
        local=pool(root=tmp_path),
        executor=Local(),
        poll_interval=5.0,  # long: the cancel must wake the wait, not time out
    )
    ts.add(shell_task("sleeper", "sleep 60"))
    results = []
    worker = threading.Thread(target=lambda: results.extend(ts.run()))
    worker.start()
    time.sleep(0.5)
    t0 = time.time()
    ts.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert time.time() - t0 < 5
    (result,) = results
    assert result.state == "cancelled"
