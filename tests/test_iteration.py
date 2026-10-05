# -*- coding: utf-8 -*-

"""One iteration of a parallel Loop as a task, and its merge (phase 6)."""

import json
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


class FakeReferences:
    def __init__(self):
        self.cited = []

    def cite(self, raw=None, alias=None, module=None, level=1, note=None):
        self.cited.append((alias, module))


def test_citations_merged_but_not_seamm(tmp_path):
    citations(tmp_path / "references.db", [("SEAMM", "seamm", 1), ("mopac", "m", 2)])
    references = FakeReferences()
    assert it.merge_citations(tmp_path, references) == 2
    assert references.cited == [("mopac", "m"), ("mopac", "m")]


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
