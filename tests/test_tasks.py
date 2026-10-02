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
    ts = TaskSet(directory=tmp_path, backend=pool(cores=4), executor=Local())
    for i in range(4):
        ts.add(
            shell_task(
                f"t{i}", "sleep 1; echo {NTASKS} > n.txt", resources=Resources(1)
            )
        )
    t0 = time.monotonic()
    results = run_all(ts)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.5, f"4 one-second tasks on 4 cores took {elapsed:.1f} s"
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
    many.add(shell_task("b", cmd, env={BINDING_ENV: "core"}))
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
    assert record["attempts"] == 2
    assert [h["state"] for h in record["history"]] == ["lost", "finished"]


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

    def submit(self, tasks, directories):
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
    r = run_all(make("ORCA TERMINATED NORMALLY"))["orca"]  # rerun, and now passes
    assert r.ok and r.attempts == 2
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
