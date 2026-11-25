# -*- coding: utf-8 -*-

"""One iteration of a parallel Loop as a task, and its merge (phase 6)."""

import json
import sys
import time
import sqlite3

import pytest

import seamm_exec.exec_flowchart as ef
from seamm_exec import computational_environment
from seamm_exec import iteration as it


def test_resolver_gives_run_flowchart_and_the_share(monkeypatch):
    config, cmd, env = it.resolve(
        {}, ["{code}", "x.flow"], {"A": "1"}, {"NTASKS": 2, "MEM_PER_CPU": 10}, None
    )
    assert config["code"].endswith("run_flowchart")
    assert cmd == ["{code}", "x.flow"]
    assert env["A"] == "1"
    assert json.loads(env[it.CE_ENVIRONMENT]) == {"NTASKS": 2, "MEM_PER_CPU": 10}
    # Its share of threads, not the pool's one per core asked for
    assert env["OMP_NUM_THREADS"] == "2"
    # A code named in seamm.ini wins
    config, _, _ = it.resolve({"code": "/opt/rf"}, [], {}, {}, None)
    assert config["code"] == "/opt/rf"


def test_computational_environment_honours_the_share(monkeypatch):
    monkeypatch.setenv(it.CE_ENVIRONMENT, json.dumps({"NTASKS": 3, "MEM_PER_NODE": 7}))
    ce = computational_environment()
    assert ce["NTASKS"] == 3
    assert ce["MEM_PER_NODE"] == 7


def test_iteration_task(tmp_path):
    job = tmp_path / "job"
    evaluator = job / "3" / "iter_1" / it.EVALUATOR_DIRECTORY
    evaluator.mkdir(parents=True)
    (job / "target.json").write_text("{}")
    task = it.iteration_task(
        "iteration_1",
        evaluator,
        job,
        command_line=["--n", "4", "a b", "{x}"],
        cores=2,
        memory=4 * 10**9,
        placement="inline",
    )
    assert task.program == it.PROGRAM
    assert task.cmd[:2] == ["{code}", "../../../flowchart.flow"]
    assert task.cmd[2:6] == ["--n", "4", "'a b'", "'{{x}}'"]
    assert task.env["SEAMM_RESUME"] == "1"
    assert task.env[it.PARENT_JOB_ENVIRONMENT] == "../../.."
    assert task.env["SEAMM_TARGET"] == ""
    assert task.env[it.READ_ENVIRONMENT] == "../../.."
    assert task.resources.ntasks == 2
    assert task.resources.mem_per_cpu == 2 * 10**9
    assert task.keep == ["."] and task.in_situ and task.shell
    assert not (evaluator / "target.json").exists()
    # Separate placement: the child's codes go to the job's target
    task = it.iteration_task("iteration_1", evaluator, job, placement="separate")
    assert "SEAMM_TARGET" not in task.env
    assert (evaluator / "target.json").exists()


def make_iteration(job, name, run_id, appended=None, files=None):
    """An iteration directory with Write Structure's record of its appends."""
    evaluator = job / "3" / name / it.EVALUATOR_DIRECTORY
    evaluator.mkdir(parents=True)
    for relative, data in (files or {}).items():
        path = evaluator / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if appended is not None:
        step = job / "3" / name / "2"
        step.mkdir(parents=True)
        sizes = {str(evaluator / k): v for k, v in appended.items()}
        (step / it.APPENDED_RECORD).write_text(
            json.dumps({"run": run_id, "sizes": sizes})
        )
    for own in ("job.out", "seamm.db", "baseline.db"):
        (evaluator / own).write_text("own")
    return evaluator


def test_files_appended_and_copied_in_order(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "all.sdf").write_bytes(b"before\n")
    first = make_iteration(
        job,
        "iter_1",
        "r1",
        appended={"all.sdf": -1},
        files={"all.sdf": b"one\n", "summary.txt": b"1", "sub/x.txt": b"x1"},
    )
    second = make_iteration(
        job,
        "iter_2",
        "r2",
        appended={"all.sdf": -1},
        files={"all.sdf": b"two\n", "summary.txt": b"2"},
    )
    plans = [
        it.plan_files(first, job, "r1"),
        it.plan_files(second, job, "r2"),
    ]
    assert plans[0] == {
        "append": {"all.sdf": [0, 7]},
        "copy": ["sub/x.txt", "summary.txt"],
    }
    it.merge_files(first, job, plans[0])
    # Interrupted and redone: the same files
    it.merge_files(first, job, plans[0])
    plans[1] = it.plan_files(second, job, "r2")
    it.merge_files(second, job, plans[1])
    assert (job / "all.sdf").read_bytes() == b"before\none\ntwo\n"
    assert (job / "summary.txt").read_bytes() == b"2"
    assert (job / "sub" / "x.txt").read_bytes() == b"x1"
    # The evaluator's own files stay where they are
    assert not (job / "baseline.db").exists()


def test_appends_of_another_run_are_copies(tmp_path):
    """A record from an earlier run of the iteration is not this run's."""
    job = tmp_path / "job"
    job.mkdir()
    evaluator = make_iteration(
        job, "iter_1", "old", appended={"all.sdf": -1}, files={"all.sdf": b"x"}
    )
    plan = it.plan_files(evaluator, job, "new")
    assert plan == {"append": {}, "copy": ["all.sdf"]}


def bib(alias):
    return f"@misc{{{alias}, title = {{{alias}}}}}"


def citations(path, rows):
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE citation (id INTEGER PRIMARY KEY, alias TEXT, raw TEXT, doi TEXT)"
    )
    db.execute(
        "CREATE TABLE context (id INTEGER PRIMARY KEY, reference_id INTEGER, "
        "module TEXT, note TEXT, count INTEGER, level INTEGER)"
    )
    for i, (alias, module, count) in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO citation VALUES (?, ?, ?, NULL)", (i, alias, bib(alias))
        )
        db.execute(
            "INSERT INTO context VALUES (?, ?, ?, 'note', ?, 1)", (i, i, module, count)
        )
    db.commit()
    db.close()


def test_citations_merged_but_not_seamm(tmp_path):
    import reference_handler

    citations(tmp_path / "references.db", [("SEAMM", "seamm", 1), ("mopac", "m", 2)])
    job = reference_handler.Reference_Handler(str(tmp_path / "job.db"))
    assert it.merge_citations(tmp_path, job) == 2
    rows = job.conn.execute(
        "SELECT c.alias, x.module, x.count FROM citation c "
        "JOIN context x ON x.reference_id = c.id"
    ).fetchall()
    assert rows == [("mopac", "m", 2)]


def _cite_each_use(evaluator, references):
    """The merge as it was: one ``cite()``, and one commit, per use."""
    for alias, raw, module, note, level, count in it._citation_rows(evaluator):
        for _ in range(max(1, int(count or 1))):
            references.cite(raw=raw, alias=alias, module=module, level=level, note=note)


def _dump(path):
    db = sqlite3.connect(str(path))
    try:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("citation", "context")
        }
    finally:
        db.close()


def test_citation_merge_same_as_citing_each_use(tmp_path):
    """Merging many iterations' citations gives the job the same references as
    citing each use, with one commit per iteration once the job has them all."""
    import reference_handler

    # Iterations as a parallel loop's: the same citations, many uses each, and one
    # a later iteration adds; one also changes its text (a newer program version).
    evaluators = []
    for k in range(1, 5):
        evaluator = tmp_path / f"iter_{k}"
        evaluator.mkdir()
        it_refs = reference_handler.Reference_Handler(str(evaluator / "references.db"))
        it_refs.cite(raw=bib("SEAMM"), alias="SEAMM", module="seamm", note="n")
        for _ in range(30 + k):
            it_refs.cite(raw=bib("xtb"), alias="xtb", module="xtb_step", note="n")
            it_refs.cite(raw=bib("GFN2"), alias="GFN2", module="xtb_step", note="m")
            it_refs.cite(
                raw=bib("GFN2"), alias="GFN2", module="xtb_step", note="m", level=2
            )
        if k >= 3:
            for _ in range(k):
                it_refs.cite(raw=bib("D4"), alias="D4", module="xtb_step", note="n")
        program = bib("program") + (" v2" if k == 4 else "")
        it_refs.cite(raw=program, alias="program", module="p", note="n")
        it_refs.conn.close()
        evaluators.append(evaluator)

    results = {}
    for name, merge in (("each use", _cite_each_use), ("merged", it.merge_citations)):
        path = tmp_path / f"{name}.db"
        job = reference_handler.Reference_Handler(str(path))
        job.cite(raw=bib("SEAMM"), alias="SEAMM", module="seamm", note="n")
        job.cite(raw=bib("xtb"), alias="xtb", module="xtb_step", note="n")
        commits = []
        for evaluator in evaluators:
            statements = []
            job.conn.set_trace_callback(statements.append)
            merge(evaluator, job)
            job.conn.set_trace_callback(None)
            commits.append(sum(s.strip().upper() == "COMMIT" for s in statements))
        job.conn.close()
        results[name] = (_dump(path), commits)

    assert results["merged"][0] == results["each use"][0]
    counts = {
        (row[1], row[2], row[3], row[5]): row[4]
        for row in results["merged"][0]["context"]
    }
    assert counts[(1, "seamm", "n", 1)] == 1
    assert counts[(2, "xtb_step", "n", 1)] == 1 + 31 + 32 + 33 + 34
    # The citations the job already has cost a single commit, not one per use
    assert results["merged"][1][1] == 1
    assert results["each use"][1][1] == 3 * 32 + 1


def test_merge_state_round_trip():
    state = {"touched": {("system", 3): 1, ("table", "t", "index", "x"): 2}}
    data = json.loads(json.dumps(it.encode_merge_state(state)))
    assert it.decode_merge_state(data) == state


def test_export_path(tmp_path):
    evaluator = tmp_path / "3" / "iter_1" / it.EVALUATOR_DIRECTORY
    evaluator.mkdir(parents=True)
    assert (
        it.export_path(evaluator / "t.csv", evaluator, tmp_path) == tmp_path / "t.csv"
    )
    step = tmp_path / "3" / "iter_1" / "2" / "t.csv"
    assert it.export_path(step, evaluator, tmp_path) == step


def test_child_mode_never_runs_from_the_top(tmp_path, monkeypatch):
    """Without the checkpoint its parent wrote, an iteration refuses to run."""
    import seamm

    monkeypatch.setenv(it.PARENT_JOB_ENVIRONMENT, "../../..")
    monkeypatch.delenv(ef.RESUME_ENVIRONMENT, raising=False)
    flowchart = seamm.Flowchart()
    flowchart.set_ids()
    (tmp_path / "seamm.db").touch()
    options = {"SEAMM": {"resume": True, "database": "seamm.db"}}
    with pytest.raises(RuntimeError, match="cannot run"):
        ef.plan_start(tmp_path, options, flowchart, [])


def test_child_mode_reports_a_finished_iteration(tmp_path, monkeypatch):
    import seamm

    monkeypatch.setenv(it.PARENT_JOB_ENVIRONMENT, "../../..")
    flowchart = seamm.Flowchart()
    flowchart.set_ids()
    db = sqlite3.connect(str(tmp_path / "seamm.db"))
    db.execute("CREATE TABLE _checkpoint (id INTEGER PRIMARY KEY, document TEXT)")
    db.execute("CREATE TABLE _checkpoint_variables (name TEXT, kind TEXT, data TEXT)")
    document = {
        "format": seamm.checkpoint.FORMAT,
        "state": "finished",
        "position": [],
        "iteration": {"done": True, "break": True, "skip": False},
        "run_id": "r",
    }
    db.execute("INSERT INTO _checkpoint VALUES (1, ?)", (json.dumps(document),))
    db.commit()
    db.close()
    options = {"SEAMM": {"resume": True, "database": "seamm.db"}}
    plan = ef.plan_start(tmp_path, options, flowchart, [])
    assert plan["resume"] is None
    assert plan["iteration"] == {"done": True, "break": True, "skip": False}
    outcome = it.iteration_outcome(tmp_path)
    assert outcome["break"] and outcome["run_id"] == "r"


def test_citations_merged_again_give_the_same_counts(tmp_path):
    """Redone after an interruption, the planned merge sets, not adds, counts."""
    import reference_handler

    evaluator = tmp_path / "it"
    evaluator.mkdir()
    citations(evaluator / "references.db", [("SEAMM", "seamm", 1), ("mopac", "m", 2)])
    job = reference_handler.Reference_Handler(str(tmp_path / "references.db"))
    job.cite(raw=bib("mopac"), alias="mopac", module="m", level=1, note="note")
    before = it.plan_citations(evaluator, job)
    for _ in range(2):
        it.merge_citations(evaluator, job, before)
    count = job.conn.execute("SELECT count FROM context").fetchall()
    assert count == [(3,)]


def test_iteration_time_is_an_estimate(tmp_path):
    """The Loop's "time per iteration" is an estimate: as a hard walltime it got no
    margin and no guards, and the same walltime again after a timeout (DMC pilot)."""
    job = tmp_path / "job"
    job.mkdir()
    evaluator = job / "loop" / "_evaluator"
    evaluator.mkdir(parents=True)
    task = it.iteration_task("iteration_1", evaluator, job, cores=4, walltime=2700)
    assert task.estimated_seconds == 2700 and task.resources.walltime is None
    task = it.iteration_task("iteration_1", evaluator, job, estimated_seconds=1800)
    assert task.estimated_seconds == 1800 and task.resources.walltime is None


def test_separate_iterations_size_their_tasks(tmp_path, monkeypatch):
    """With separate tasks the evaluator is small, and the loop's cores and memory
    per iteration are the size its codes plan with and its tasks get (Paul,
    2026-10-30; ChemAI 5469: a 286 GB idle evaluator, 32 GB ORCA tasks)."""
    job = tmp_path / "job"
    evaluator = job / "3" / "iter_1" / it.EVALUATOR_DIRECTORY
    evaluator.mkdir(parents=True)
    task = it.iteration_task(
        "iteration_1", evaluator, job, cores=4, memory=286 * 10**9, placement="separate"
    )
    assert task.resources.ntasks == 1
    assert task.resources.mem_per_cpu == it.SEPARATE_EVALUATOR_MEMORY
    task_ce = json.loads(task.env[it.TASK_CE_ENVIRONMENT])
    assert task_ce == {
        "NTASKS": 4,
        "CPUS_PER_TASK": 1,
        "MEM_PER_CPU": 286 * 10**9 // 4,
        "MEM_PER_NODE": 286 * 10**9,
    }
    # In the evaluator, the codes see the tasks' size
    monkeypatch.setenv(it.TASK_CE_ENVIRONMENT, task.env[it.TASK_CE_ENVIRONMENT])
    monkeypatch.setenv(it.CE_ENVIRONMENT, json.dumps({"NTASKS": 1}))
    ce = computational_environment()
    assert ce["NTASKS"] == 4 and ce["MEM_PER_NODE"] == 286 * 10**9
    # Inline iterations are unchanged
    task = it.iteration_task(
        "iteration_1", evaluator, job, cores=4, memory=8 * 10**9, placement="inline"
    )
    assert task.resources.ntasks == 4 and it.TASK_CE_ENVIRONMENT not in task.env


def test_kill_marker():
    from seamm_exec.kill import is_killed

    assert not is_killed(None)


def test_signal_spares_a_killed_evaluator_for_cleanup(tmp_path, monkeypatch):
    """On SIGTERM a task group whose directory has the kill marker (an iteration's
    evaluator of a killed job) gets SIGTERM, to cancel its own queue jobs; others
    get SIGKILL as before."""
    import signal
    import subprocess

    from seamm_exec import local_pool
    from seamm_exec.kill import KILL_MARKER

    killed = tmp_path / "killed"
    killed.mkdir()
    (killed / KILL_MARKER).touch()
    script = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (open('got_term', 'w').close(),"
        " sys.exit(0)))\n"
        "time.sleep(30)\n"
    )
    procs = {}
    for name, directory in (("killed", killed), ("other", tmp_path)):
        p = subprocess.Popen(
            [sys.executable, "-c", script], cwd=directory, start_new_session=True
        )
        procs[name] = p
        local_pool._register(p.pid, str(directory))
    time.sleep(1.0)  # the handlers are installed
    monkeypatch.setattr(local_pool, "KILL_GRACE", 10.0)
    monkeypatch.setitem(local_pool._previous_handlers, signal.SIGTERM, lambda *a: None)
    try:
        local_pool._on_signal(signal.SIGTERM, None)
        assert procs["killed"].wait(10) == 0
        assert (killed / "got_term").exists()
        assert procs["other"].wait(10) == -signal.SIGKILL
    finally:
        for p in procs.values():
            local_pool._unregister(p.pid)
            if p.poll() is None:
                p.kill()
