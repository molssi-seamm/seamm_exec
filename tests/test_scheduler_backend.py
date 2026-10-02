# -*- coding: utf-8 -*-

"""Tests for the SchedulerBackend and the TaskSet's use of it.

The queue is a fake that runs each submitted batch script with bash right
away (optionally after a delay, or never, to look like a queue), so the tests
exercise the real script, the real task worker in SEAMM mode, the LocalPool
inside the "allocation", and the DONE/FAILED markers, without SLURM.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from seamm_scheduler import JobStatus, TargetSection
from seamm_scheduler.slurm import Slurm

from seamm_exec import Resources, Task, TaskSet, run_task
from seamm_exec.local_pool import LocalPool
from seamm_exec.resolve import register, resolve
from seamm_exec.scheduler_backend import (
    QueueFull,
    SchedulerBackend,
    default_root,
    parse_id,
    remote_name,
)
from seamm_exec.targets import find_target, write_target


class FakeQueue:
    """A queue that runs scripts with bash. Shared across 'evaluators'."""

    def __init__(self, start=True, hold=False, fail_submit=None):
        self.scheduler = Slurm()
        self.jobs = {}  # id -> {"proc", "state", "script"}
        self.scripts = []
        self.start = start
        self.hold = hold
        self.fail_submit = fail_submit
        self.n = 1000
        self.lock = threading.Lock()
        self.cancelled = []
        self.queued_elsewhere = 0

    def _run(self, argv, input_text=None):
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def submit(self, script, *, job_name=None):
        if self.fail_submit:
            raise RuntimeError(self.fail_submit)
        with self.lock:
            self.n += 1
            job_id = str(self.n)
            self.scripts.append(script)
            job = {"proc": None, "state": "PENDING", "script": script}
            self.jobs[job_id] = job
        if self.start and not self.hold:
            self.release(job_id)
        return job_id

    def release(self, job_id):
        job = self.jobs[job_id]
        env = dict(os.environ)
        env.pop("SLURM_JOB_ID", None)
        # The worker imports this checkout, not an installed seamm_exec.
        source = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(
            [source] + [p for p in [env.get("PYTHONPATH")] if p]
        )
        job["proc"] = subprocess.Popen(
            ["bash", "-c", job["script"]],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        job["state"] = "RUNNING"

    def end(self, job_id, state):
        """End a job that never ran, e.g. TIMEOUT in the queue."""
        self.jobs[job_id]["state"] = state

    def poll_many(self, ids):
        result = {}
        for i in ids:
            job = self.jobs.get(str(i))
            if job is None:
                continue
            proc = job["proc"]
            if proc is not None and job["state"] == "RUNNING":
                rc = proc.poll()
                if rc is not None:
                    job["state"] = "COMPLETED" if rc == 0 else "FAILED"
            state = job["state"]
            result[str(i)] = JobStatus(str(i), state, self.scheduler.classify(state))
        return result

    def cancel_many(self, ids):
        self.cancelled.extend(ids)
        for i in ids:
            job = self.jobs[str(i)]
            if job["proc"] is not None and job["proc"].poll() is None:
                os.killpg(job["proc"].pid, 9)
            job["state"] = "CANCELLED"

    def count_jobs(self):
        self.poll_many(list(self.jobs))  # as squeue would see them now
        live = [j for j in self.jobs.values() if j["state"] in ("PENDING", "RUNNING")]
        return len(live) + self.queued_elsewhere


@pytest.fixture
def job(tmp_path):
    """A job directory with a root holding fake.ini."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "fake.ini").write_text("[local]\ninstallation = local\ncode = echo\n")
    job = tmp_path / "Job_000007"
    (job / "step").mkdir(parents=True)
    return job, root


def make_backend(queue, job, root, **kwargs):
    kwargs.setdefault("poll_interval", 0.2)
    return SchedulerBackend(
        queue, name="queue:test", job_directory=job, root=str(root), **kwargs
    )


def fake_task(key, text=None, **kwargs):
    text = text if text is not None else key
    kwargs.setdefault("return_files", ["out.txt"])
    return Task(
        key=key,
        program="fake",
        cmd=["{code}", text, "{NTASKS}", ">", "out.txt"],
        shell=True,
        **kwargs,
    )


def run_all(task_set):
    return {r.key: r for r in task_set.run()}


# ---- helpers ----------------------------------------------------------------


def test_ids_and_names():
    assert parse_id("123#bundle_0000.1#frag-1") == ("123", "bundle_0000.1", "frag-1")
    assert parse_id("123") is None
    assert remote_name("/a/b/Job_000123") == "Job_000123"
    name = remote_name("/a/b/tmp")
    assert name.startswith("tmp-") and len(name) == 14
    assert remote_name("/a/b/tmp") == name
    assert default_root("/projects/seamm/SEAMM/venv/bin/python") == (
        "/projects/seamm/SEAMM"
    )
    assert default_root("/x/SEAMM/venvs/2026-10-02T15-03-12/bin/python") == "/x/SEAMM"


# ---- end to end through the fake queue -----------------------------------


def test_bundles_run_through_the_worker(job):
    job, root = job
    queue = FakeQueue()
    backend = make_backend(queue, job, root)
    ts = TaskSet(directory=job / "step", backend=backend, bundle_tasks=2)
    for i in range(5):
        ts.add(fake_task(f"t{i}", resources=Resources(ntasks=1)))
    results = run_all(ts)

    assert len(queue.scripts) == 3  # 2 + 2 + 1
    assert all(r.ok for r in results.values())
    for i in range(5):
        r = results[f"t{i}"]
        assert r.files["out.txt"].split()[0] == f"t{i}"
        # The task's share of the "allocation": the code saw {NTASKS} = 1
        assert r.files["out.txt"].split()[1] == "1"
        # The TaskSet rewrote the worker's DONE in its own form
        done = json.loads((job / "step" / "tasks" / f"t{i}" / "DONE").read_text())
        assert done["files"] == ["out.txt"]
    script = queue.scripts[0]
    assert "#SBATCH --job-name" not in script  # given on the sbatch command
    assert "#SBATCH --ntasks=1" in script
    assert "-m seamm_exec.task_worker bundle.json" in script
    bundles = sorted(p.name for p in (job / "step" / "tasks" / "_bundles").iterdir())
    assert bundles == ["bundle_0000.1", "bundle_0001.1", "bundle_0002.1"]
    bundle = json.loads(
        (
            job / "step" / "tasks" / "_bundles" / "bundle_0000.1" / "bundle.json"
        ).read_text()
    )
    assert bundle["mode"] == "seamm"
    assert [t["key"] for t in bundle["tasks"]] == ["t0", "t1"]
    manifest = json.loads((job / "step" / "tasks" / "manifest.json").read_text())
    assert parse_id(manifest["tasks"]["t3"]["id"])[1] == "bundle_0001.1"

    # A rerun restores everything without submitting
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    for i in range(5):
        ts.add(fake_task(f"t{i}", resources=Resources(ntasks=1)))
    results = run_all(ts)
    assert all(r.restored for r in results.values())
    assert len(queue.scripts) == 3


def test_input_files_and_success_text(job):
    job, root = job
    queue = FakeQueue()
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    ts.add(
        Task(
            key="good",
            program="fake",
            cmd=["cat", "in.txt", ">", "out.txt"],
            shell=True,
            files={"in.txt": "TERMINATED NORMALLY\n"},
            return_files=["out.txt"],
            success_text={"out.txt": "TERMINATED NORMALLY"},
        )
    )
    ts.add(
        Task(
            key="bad",
            program="fake",
            cmd=["echo", "error termination", ">", "out.txt"],
            shell=True,
            return_files=["out.txt"],
            success_text={"out.txt": "TERMINATED NORMALLY"},
        )
    )
    ts.add(Task(key="rc", program="fake", cmd=["exit", "3"], shell=True))
    results = run_all(ts)
    assert results["good"].ok
    assert results["good"].files["out.txt"] == "TERMINATED NORMALLY\n"
    assert results["bad"].state == "failed"
    assert "success check" in results["bad"].reason
    assert results["rc"].state == "failed"
    assert results["rc"].returncode == 3
    assert results["rc"].reason == "return code 3"
    marker = job / "step" / "tasks" / "bad"
    assert json.loads((marker / "FAILED").read_text())["state"] == "failed"
    assert not (marker / "DONE").exists()


def test_config_is_sent_only_to_a_local_transport(job):
    job, root = job
    queue = FakeQueue()
    task = Task(
        key="c",
        program="nowhere",  # no ini: only the config makes it runnable
        cmd=["{code}", "configured", ">", "out.txt"],
        shell=True,
        return_files=["out.txt"],
        config={"installation": "local", "code": "echo"},
    )
    backend = make_backend(queue, job, root, accepts_config=True)
    result = run_task(task, directory=job / "step", backend=backend)
    assert result.ok and result.files["out.txt"] == "configured\n"
    assert len(queue.scripts) == 1

    # A remote target: the task stays on this machine.
    backend = make_backend(queue, job, root, accepts_config=False)
    local = LocalPool(_executor(), root=root)
    ts = TaskSet(directory=job / "step2", backend=backend, local=local)
    ts.add(task)
    (result,) = ts.run()
    assert result.ok
    assert len(queue.scripts) == 1
    manifest = json.loads((job / "step2" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["c"]["backend"] == "local"


def _executor():
    from seamm_exec import Local

    return Local()


def test_restart_adopts_a_queued_bundle(job):
    job, root = job
    queue = FakeQueue(hold=True)

    # The first evaluator submits and dies while the bundle waits in the queue.
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    for i in range(3):
        ts.add(fake_task(f"t{i}"))
    it = ts.run()
    with pytest.raises(TimeoutError):
        _first_with_timeout(it, 1.0)
    assert len(queue.scripts) == 1
    manifest = json.loads((job / "step" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["t0"]["state"] == "queued"

    # The queue starts the job; a new evaluator polls it instead of submitting.
    (job_id,) = queue.jobs
    queue.release(job_id)
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    for i in range(3):
        ts.add(fake_task(f"t{i}"))
    results = run_all(ts)
    assert all(r.ok for r in results.values())
    assert len(queue.scripts) == 1
    assert not queue.cancelled


def _first_with_timeout(iterator, seconds):
    """Run a TaskSet's generator in a thread, then abandon it like a killed
    evaluator (no finally, no cancel)."""
    out = []
    thread = threading.Thread(target=lambda: out.append(next(iterator)), daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError
    return out[0]


def test_lost_tasks_go_back_together_as_one_bundle(job):
    job, root = job
    queue = FakeQueue(hold=True)
    backend = make_backend(queue, job, root)
    ts = TaskSet(directory=job / "step", backend=backend, bundle_tasks=3)
    for i in range(3):
        ts.add(fake_task(f"t{i}"))

    def timeout_first_job():
        while not queue.jobs:
            time.sleep(0.05)
        first = next(iter(queue.jobs))
        queue.end(first, "TIMEOUT")
        # Later submissions run.
        queue.hold = False

    threading.Thread(target=timeout_first_job, daemon=True).start()
    results = run_all(ts)
    assert all(r.ok for r in results.values())
    assert len(queue.scripts) == 2  # the resubmission is one bundle, not three
    manifest = json.loads((job / "step" / "tasks" / "manifest.json").read_text())
    record = manifest["tasks"]["t1"]
    assert record["attempts"] == 2
    assert "ended (TIMEOUT)" in record["history"][0]["reason"]
    assert parse_id(record["id"])[1] == "bundle_0000.2"


def test_a_partially_done_bundle_runs_only_what_is_left(job):
    job, root = job
    queue = FakeQueue()
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    ts.add(fake_task("a"))
    ts.add(fake_task("b"))
    run_all(ts)
    bundle_json = job / "step" / "tasks" / "_bundles" / "bundle_0000.1" / "bundle.json"
    # Resubmitting the same bundle.json (as after a walltime limit) skips the
    # tasks with a DONE.
    (job / "step" / "tasks" / "b" / "DONE").unlink()
    (job / "step" / "tasks" / "a" / "out.txt").write_text("untouched\n")
    p = subprocess.run(
        [sys.executable, "-m", "seamm_exec.task_worker", str(bundle_json)],
        cwd=bundle_json.parent,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    assert "a: already done" in p.stdout
    assert (job / "step" / "tasks" / "a" / "out.txt").read_text() == "untouched\n"
    assert (job / "step" / "tasks" / "b" / "DONE").exists()


def test_room_holds_bundles_until_the_queue_has_space(job):
    job, root = job
    queue = FakeQueue()
    queue.queued_elsewhere = 1
    backend = make_backend(queue, job, root, max_queued=2)
    ts = TaskSet(directory=job / "step", backend=backend, bundle_tasks=1)
    for i in range(4):
        ts.add(fake_task(f"t{i}"))
    peak = []

    def watch():
        while len(peak) < 1000:
            peak.append(
                sum(
                    1
                    for j in list(queue.jobs.values())
                    if j["state"] in ("PENDING", "RUNNING")
                )
            )
            time.sleep(0.01)

    threading.Thread(target=watch, daemon=True).start()
    results = run_all(ts)
    assert all(r.ok for r in results.values())
    assert len(queue.scripts) == 4
    assert max(peak) <= 1  # 2 allowed, 1 used by another job


def test_queue_full_on_submit_holds_instead_of_failing(job):
    job, root = job
    queue = FakeQueue(fail_submit="sbatch: error: QOSMaxSubmitJobPerUserLimit")
    backend = make_backend(queue, job, root)
    ts = TaskSet(directory=job / "step", backend=backend)
    ts.add(fake_task("t0"))

    def clear():
        time.sleep(0.5)
        queue.fail_submit = None

    threading.Thread(target=clear, daemon=True).start()
    (result,) = ts.run()
    assert result.ok
    manifest = json.loads((job / "step" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["t0"]["attempts"] == 1


def test_other_submit_errors_raise_and_do_not_count(job):
    job, root = job
    queue = FakeQueue(fail_submit="sbatch: error: invalid partition specified")
    ts = TaskSet(directory=job / "step", backend=make_backend(queue, job, root))
    ts.add(fake_task("t0"))
    with pytest.raises(RuntimeError, match="invalid partition"):
        list(ts.run())
    manifest = json.loads((job / "step" / "tasks" / "manifest.json").read_text())
    assert manifest["tasks"]["t0"]["attempts"] == 0


def test_queue_full_pattern():
    from seamm_exec.scheduler_backend import _QUEUE_FULL

    assert _QUEUE_FULL.search(
        "sbatch: error: QOSMaxSubmitJobPerUserLimit\nsbatch: error: Batch job "
        "submission failed: Job violates accounting/QOS policy (job submit limit, "
        "user's size and/or time limits)"
    )
    assert not _QUEUE_FULL.search("sbatch: error: invalid partition specified")
    assert issubclass(QueueFull, RuntimeError)


def test_bundle_resources_and_walltime(job):
    job, root = job
    backend = make_backend(FakeQueue(), job, root, bundle_walltime=3600)
    tasks = [
        fake_task("a", resources=Resources(ntasks=4, mem_per_cpu=2 * 1024**3)),
        fake_task("b", resources=Resources(ntasks=2, partition="normal_q")),
    ]
    r = backend._bundle_resources(tasks)
    assert r["ntasks"] == 4
    assert r["mem_per_cpu"] == 2 * 1024**3
    assert r["partition"] == "normal_q"
    assert r["walltime"] == 3600
    tasks = [
        fake_task("a", resources=Resources(walltime=100)),
        fake_task("b", resources=Resources(walltime=200)),
    ]
    assert backend._bundle_resources(tasks)["walltime"] == 300


def test_bundles_by_walltime():
    ts = TaskSet(manifest=False, backend=LocalPool(_executor()), bundle_walltime=100)
    for i, seconds in enumerate([40, 40, 40, 90, 5, 5]):
        ts.add(fake_task(f"t{i}", estimated_seconds=seconds))
    assert [ts._bundles[f"t{i}"] for i in range(6)] == [
        "bundle_0000",  # 40
        "bundle_0000",  # 80
        "bundle_0001",  # 120 > 100: a new bundle
        "bundle_0002",  # 130 > 100
        "bundle_0002",  # 95
        "bundle_0002",  # 100, not more than the limit
    ]


# ---- targets ---------------------------------------------------------------


def test_target_json_round_trip_and_precedence(tmp_path, monkeypatch):
    section = TargetSection(
        name="arc",
        transport="ssh",
        host="tinkercliffs",
        type="local",
        tasks="queue",
        remote_root="/r",
        remote_python="/p/venv/bin/python",
        bundle_tasks=8,
        inline_below=5.0,
    )
    write_target(section, tmp_path)
    assert find_target(job_directory=tmp_path) == section
    other = TargetSection(name="x", transport="local", host=None, tasks="pool")
    assert find_target(other, job_directory=tmp_path) is other

    # SEAMM_TARGET for hand runs
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target.json").unlink()
    ini = tmp_path / "jobserver.ini"
    ini.write_text("[mine]\ntype = local\ntasks = pool\n")
    monkeypatch.setenv("SEAMM_TARGET", "mine")
    monkeypatch.setenv("SEAMM_TARGETS", str(ini))
    assert find_target(job_directory=tmp_path).name == "mine"
    monkeypatch.delenv("SEAMM_TARGET")
    assert find_target(job_directory=tmp_path) is None


def test_taskset_takes_bundling_and_inline_from_the_target(tmp_path):
    section = TargetSection(
        name="q",
        transport="local",
        host=None,
        type="local",
        tasks="queue",
        bundle_tasks=2,
        inline_below=5.0,
    )
    ts = TaskSet(directory=tmp_path, target=section)
    assert ts.bundle_tasks == 2 and ts.inline_below == 5.0
    assert isinstance(ts.backend, SchedulerBackend)
    assert ts.backend.name == "queue:q"
    # No bundling given for a queue: one task per bundle
    section.bundle_tasks = None
    assert TaskSet(directory=tmp_path, target=section).bundle_tasks == 1
    # A pool target is the LocalPool
    pool = TargetSection(name="p", transport="local", host=None, tasks="pool")
    assert isinstance(TaskSet(directory=tmp_path, target=pool).backend, LocalPool)


def test_ssh_target_needs_remote_python_and_root(tmp_path):
    section = TargetSection(
        name="arc",
        transport="ssh",
        host="tinkercliffs",
        type="local",
        tasks="queue",
    )
    with pytest.raises(RuntimeError, match="needs remote_root"):
        SchedulerBackend.from_target(section, job_directory=tmp_path)
    section.remote_root = "/projects/x"
    with pytest.raises(RuntimeError, match="needs remote_python"):
        SchedulerBackend.from_target(section, job_directory=tmp_path)
    section.remote_python = "/projects/seamm/SEAMM/venv/bin/python"
    backend = SchedulerBackend.from_target(section, job_directory=tmp_path / "Job_1")
    assert backend.remote_job_directory == "/projects/x/Job_1"
    assert backend.root == "/projects/seamm/SEAMM"
    assert not backend.accepts_config
    assert backend.where(tmp_path / "Job_1" / "s" / "tasks" / "a") == (
        "/projects/x/Job_1/s/tasks/a"
    )
    with pytest.raises(RuntimeError, match="not inside the job directory"):
        backend.where(tmp_path / "elsewhere")


# ---- staging (ssh) with a stager that copies locally ---------------------


class CopyStager:
    """An RsyncStager stand-in: 'remote' is another local directory."""

    def __init__(self):
        self.pushes = []
        self.pulls = []

    def push(self, local_base, remote_base, paths, *, delete=False):
        import shutil

        self.pushes.append(list(paths))
        for p in paths:
            src = Path(local_base) / p
            dst = Path(remote_base) / p
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)

    def pull(self, remote_base, local_base, paths, *, exclude=()):
        import shutil

        self.pulls.append(list(paths))
        for p in paths:
            src = Path(remote_base) / p
            dst = Path(local_base) / p
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)


def test_staged_bundle_runs_in_the_remote_copy(job, tmp_path):
    job, root = job
    remote = tmp_path / "remote" / "Job_000007"
    queue = FakeQueue()
    stager = CopyStager()
    backend = make_backend(
        queue, job, root, stager=stager, remote_job_directory=str(remote)
    )
    ts = TaskSet(directory=job / "step", backend=backend, bundle_tasks=2)
    ts.add(fake_task("a", files={"in.txt": "x"}))
    ts.add(fake_task("b"))
    results = run_all(ts)
    assert all(r.ok for r in results.values())
    # It ran in the "remote" copy and came back
    assert (remote / "step" / "tasks" / "a" / "out.txt").exists()
    assert (job / "step" / "tasks" / "a" / "out.txt").read_text().startswith("a ")
    assert len(stager.pushes) == 1 and len(stager.pulls) == 1
    assert "step/tasks/_bundles/bundle_0000.1" in stager.pushes[0]
    bundle = json.loads(
        (
            job / "step" / "tasks" / "_bundles" / "bundle_0000.1" / "bundle.json"
        ).read_text()
    )
    assert bundle["tasks"][0]["directory"] == str(remote / "step" / "tasks" / "a")
    assert bundle["tasks"][0]["files"] == ["in.txt"]


# ---- the resolver hook ----------------------------------------------------


def test_resolver_hook(tmp_path):
    calls = []

    def hook(config, cmd, env, ce, root):
        calls.append((dict(config), list(cmd), ce.get("NTASKS"), root))
        config["code"] = "echo resolved"
        env["FROM_HOOK"] = "1"
        return config, ["{code}", "$FROM_HOOK", ">", "out.txt"], env

    register("hooked", hook)
    config, cmd, env = resolve("hooked", {"code": "x"}, ["a"], {}, {"NTASKS": 2}, "/r")
    assert config["code"] == "echo resolved"
    assert calls == [({"code": "x"}, ["a"], 2, "/r")]
    # Unknown programs pass through unchanged
    assert resolve("nothing", {"a": 1}, ["x"], {"E": "1"}, {}, None) == (
        {"a": 1},
        ["x"],
        {"E": "1"},
    )

    # In a LocalPool that resolves programs (the worker's)
    (tmp_path / "hooked.ini").write_text("[local]\ninstallation = local\ncode = no\n")
    pool = LocalPool(_executor(), root=tmp_path, resolve_programs=True)
    ts = TaskSet(directory=tmp_path / "s", backend=pool)
    ts.add(
        Task(key="h", program="hooked", cmd=["x"], shell=True, return_files=["out.txt"])
    )
    (result,) = ts.run()
    assert result.files["out.txt"] == "resolved 1\n"


# ---- computational environment under PBS ---------------------------------


def test_pbs_computational_environment(tmp_path, monkeypatch):
    from seamm_exec.computational_environment import (
        computational_environment,
        running_scheduler,
    )

    nodefile = tmp_path / "nodes"
    nodefile.write_text("n1\nn1\nn2\nn2\n")
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.setenv("PBS_JOBID", "12.pbs01")
    monkeypatch.setenv("PBS_NODEFILE", str(nodefile))
    monkeypatch.setenv("NCPUS", "4")
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    assert running_scheduler() == "pbs"
    ce = computational_environment()
    assert ce["type"] == "pbs"
    assert ce["NTASKS"] == 4 and ce["NNODES"] == 2 and ce["CPUS_PER_TASK"] == 2
    assert ce["NODELIST"] == "n1:2,n2:2"
    assert ce["MEM_PER_CPU"] > 0
