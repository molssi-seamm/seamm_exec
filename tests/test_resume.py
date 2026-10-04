# -*- coding: utf-8 -*-

"""Resuming a flowchart from its checkpoint (phase 5)."""

import json
import os
import sqlite3

import pytest

import seamm
import seamm_exec.exec_flowchart as ef


@pytest.fixture(autouse=True)
def fresh_parsers(monkeypatch):
    """Each run registers its options on SEAMM's global argument parsers."""
    import seamm_util.argument_parser

    monkeypatch.setattr(seamm_util.argument_parser, "_parsers", {})
    monkeypatch.delenv(ef.RESUME_ENVIRONMENT, raising=False)


class Step(seamm.Node):
    """Adds a system named after itself, and counts its runs in a variable."""

    version = "2026.10.4"
    failing = set()
    runs = []

    def run(self):
        Step.runs.append(self.title)
        db = self.get_variable("_system_db")
        db.create_system(name=self.title).create_configuration(name=self.title)
        self.set_variable(f"ran_{self.title}", True)
        if self.title in Step.failing:
            raise RuntimeError(f"{self.title} failed")
        return self.next()


def make_flowchart(root, *titles):
    flowchart = seamm.Flowchart(directory=str(root))
    previous = flowchart.get_node("1")
    for title in titles:
        step = Step(flowchart=flowchart, title=title)
        flowchart.add_node(step)
        flowchart.add_edge(previous, step, edge_type="execution")
        previous = step
    flowchart.set_ids()
    return flowchart


def systems(root):
    db = sqlite3.connect(f"file:{root / 'seamm.db'}?mode=ro", uri=True)
    try:
        return [r[0] for r in db.execute("SELECT name FROM system ORDER BY id")]
    finally:
        db.close()


def execute(root, flowchart, options, cmdline=()):
    """Run the flowchart in root as exec_flowchart does."""
    plan = ef.plan_start(root, options, flowchart, list(cmdline))
    cwd = os.getcwd()
    try:
        os.chdir(root)
        ef.ExecFlowchart(flowchart, cmdline=list(cmdline), plan=plan).run(
            root=str(root)
        )
    finally:
        os.chdir(cwd)
    return plan


@pytest.fixture()
def failed_job(tmp_path):
    """A job whose second of three steps failed."""
    Step.failing = {"B"}
    Step.runs = []
    flowchart = make_flowchart(tmp_path, "A", "B", "C")
    with pytest.raises(RuntimeError):
        execute(tmp_path, flowchart, {"SEAMM": {}})
    assert Step.runs == ["A", "B"]
    assert systems(tmp_path) == ["A"]  # B's writes rolled back
    Step.failing = set()
    Step.runs = []
    return tmp_path, flowchart


def test_resume_reruns_from_the_failed_step(failed_job):
    root, _ = failed_job
    flowchart = make_flowchart(root, "A", "B", "C")  # read again: new uuids
    plan = execute(root, flowchart, {"SEAMM": {"resume": True}}, ["--resume"])
    assert plan["resume"] is not None
    assert "Resuming at step 2" in plan["message"]
    assert Step.runs == ["B", "C"]
    assert systems(root) == ["A", "B", "C"]
    assert seamm.read_checkpoint(root / "seamm.db")["state"] == "finished"
    assert json.loads((root / "checkpoint.json").read_text())["state"] == "finished"
    assert not (root / "previous").exists()


def test_resume_by_environment(failed_job, monkeypatch):
    root, _ = failed_job
    flowchart = make_flowchart(root, "A", "B", "C")  # read again: new uuids
    monkeypatch.setenv(ef.RESUME_ENVIRONMENT, "1")
    plan = execute(root, flowchart, {"SEAMM": {}})
    assert plan["resume"] is not None
    assert Step.runs == ["B", "C"]


def test_rerun_without_resume_starts_over(failed_job):
    root, _ = failed_job
    flowchart = make_flowchart(root, "A", "B", "C")  # read again: new uuids
    plan = execute(root, flowchart, {"SEAMM": {}})
    assert plan["resume"] is None
    assert "--resume" in plan["message"]
    assert Step.runs == ["A", "B", "C"]
    assert systems(root) == ["A", "B", "C"]
    previous = list((root / "previous").iterdir())
    assert len(previous) == 1
    # The set-aside database can still be resumed from.
    checkpoint = seamm.read_checkpoint(previous[0] / "seamm.db")
    assert checkpoint["state"] == "error"


def test_resume_refused_for_other_command_line(failed_job):
    root, _ = failed_job
    flowchart = make_flowchart(root, "A", "B", "C")  # read again: new uuids
    plan = execute(root, flowchart, {"SEAMM": {"resume": True}}, ["--n", "4"])
    assert plan["resume"] is None
    assert "command line" in plan["message"]
    assert Step.runs == ["A", "B", "C"]


def test_resume_of_a_finished_job_starts_over(tmp_path):
    Step.failing = set()
    flowchart = make_flowchart(tmp_path, "A")
    execute(tmp_path, flowchart, {"SEAMM": {}})
    Step.runs = []
    plan = execute(tmp_path, flowchart, {"SEAMM": {"resume": True}})
    assert plan["resume"] is None
    assert "finished" in plan["message"]
    assert Step.runs == ["A"]


def test_no_checkpoint_for_a_read_only_database(tmp_path):
    flowchart = make_flowchart(tmp_path, "A")
    plan = ef.plan_start(tmp_path, {"SEAMM": {"read_only": True}}, flowchart, [])
    assert plan == {"resume": None, "checkpointing": False, "message": None}
    plan = ef.plan_start(
        tmp_path, {"SEAMM": {"read_only": True, "resume": True}}, flowchart, []
    )
    assert plan["checkpointing"] is False
    assert "cannot resume" in plan["message"]


def test_first_run(tmp_path):
    flowchart = make_flowchart(tmp_path, "A")
    plan = ef.plan_start(tmp_path, {"SEAMM": {}}, flowchart, [])
    assert plan == {"resume": None, "checkpointing": True, "message": None}


def test_comparable_command_line():
    assert ef.comparable_command_line(["--resume", "--n", "3"]) == ["--n", "3"]


@pytest.mark.parametrize("value, expected", [("1", True), ("yes", True), ("0", False)])
def test_resume_requested_environment(monkeypatch, value, expected):
    monkeypatch.setenv(ef.RESUME_ENVIRONMENT, value)
    assert ef.resume_requested({"SEAMM": {}}) is expected


def test_resume_after_a_version_change(failed_job):
    """Resuming with other package versions works, with a note saying so."""
    root, _ = failed_job
    flowchart = make_flowchart(root, "A", "B", "C")  # read again: new uuids
    db = sqlite3.connect(root / "seamm.db")
    document = json.loads(db.execute("SELECT document FROM _checkpoint").fetchone()[0])
    document["versions"]["seamm"] = "2026.1.1"
    db.execute("UPDATE _checkpoint SET document = ?", (json.dumps(document),))
    db.commit()
    db.close()
    plan = execute(root, flowchart, {"SEAMM": {"resume": True}})
    assert plan["resume"] is not None
    assert "seamm 2026.1.1 ->" in plan["message"]
    assert Step.runs == ["B", "C"]
