# -*- coding: utf-8 -*-

"""The SEAMM root for a step's tasks: from a sub-step too (orca_step's Energy)."""

from types import SimpleNamespace

from seamm_exec.tasks import TaskSet, node_root


def node(options=None, parent=None):
    return SimpleNamespace(
        global_options=options if options is not None else {},
        parent=parent,
        directory="/tmp/x",
        flowchart=SimpleNamespace(executor=SimpleNamespace(name="local")),
    )


def test_a_step_with_the_root():
    assert node_root(node({"root": "/my/root"})) == "/my/root"


def test_a_sub_step_takes_its_steps_root():
    step = node({"root": "/my/root"})
    sub_step = node({}, parent=step)
    assert node_root(sub_step) == "/my/root"
    assert node_root(node({}, parent=sub_step)) == "/my/root"


def test_no_root_anywhere_is_the_runs_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAMM_ROOT", str(tmp_path))
    monkeypatch.setattr("seamm_util.argument_parser._parsers", {}, raising=False)
    assert node_root(node({}, parent=node({}))) == str(tmp_path)


def test_a_cycle_ends(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAMM_ROOT", str(tmp_path))
    monkeypatch.setattr("seamm_util.argument_parser._parsers", {}, raising=False)
    a = node({})
    b = node({}, parent=a)
    a.parent = b
    assert node_root(b) == str(tmp_path)


def test_task_set_of_a_sub_step_reads_the_steps_root(tmp_path):
    step = node({"root": str(tmp_path)})
    ts = TaskSet(node({}, parent=step), directory=tmp_path / "step")
    assert ts.root == str(tmp_path)
