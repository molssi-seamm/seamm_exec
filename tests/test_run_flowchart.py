# -*- coding: utf-8 -*-

"""run_flowchart: the exit status (#40) and rerunning in place (#41)."""

import json
import os
from pathlib import Path

import pytest

import seamm
import seamm_exec.exec_flowchart as ef


@pytest.fixture(autouse=True)
def fresh_parsers(monkeypatch):
    """Each run registers its options on SEAMM's global argument parsers."""
    import seamm_util.argument_parser

    monkeypatch.setattr(seamm_util.argument_parser, "_parsers", {})


def _run(tmp_path, monkeypatch, *extra):
    flow = tmp_path / "empty.flow"
    if not flow.exists():
        seamm.Flowchart().write(str(flow))
    monkeypatch.setattr(
        "sys.argv", ["run_flowchart", str(flow), "--standalone", *extra]
    )
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        ef.run(wdir=str(tmp_path))
    finally:
        os.chdir(cwd)


def _state(tmp_path):
    text = (tmp_path / "job_data.json").read_text()
    return json.loads(text.split("\n", 1)[1])["state"]


def test_finished_flowchart_exits_normally(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert _state(tmp_path) == "finished"


def test_failed_flowchart_exits_1(tmp_path, monkeypatch):
    """The failure is recorded in job_data.json and the exit status is 1."""

    def fail(self, root=None, job_id=None):
        raise RuntimeError("step failed")

    monkeypatch.setattr(ef.ExecFlowchart, "run", fail)
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, monkeypatch)
    assert e.value.code == 1
    assert _state(tmp_path) == "error"


def _previous_run(root):
    """What an earlier run leaves: its job database and a step's tasks."""
    for name in ("seamm.db", "seamm.db-wal", "seamm.db-shm", "references.db"):
        (root / name).write_text(f"old {name}")
    manifest = root / "2" / "tasks" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    return manifest


def test_rerun_in_place_archives_the_database(tmp_path):
    manifest = _previous_run(tmp_path)
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        moved = ef.archive_previous_database(tmp_path, {"SEAMM": {}})
    finally:
        os.chdir(cwd)
    assert moved is not None and moved.parent == tmp_path / "previous"
    for name in ("seamm.db", "seamm.db-wal", "seamm.db-shm", "references.db"):
        assert not (tmp_path / name).exists()
        assert (moved / name).read_text() == f"old {name}"
    # The step directories and their task manifests stay.
    assert manifest.read_text() == "{}"


def test_rerun_through_run_flowchart(tmp_path, monkeypatch):
    _previous_run(tmp_path)
    _run(tmp_path, monkeypatch)
    assert _state(tmp_path) == "finished"
    archived = list((tmp_path / "previous").iterdir())
    assert len(archived) == 1
    assert (archived[0] / "seamm.db").read_text() == "old seamm.db"
    assert "earlier run" in (tmp_path / "job.out").read_text()


@pytest.mark.parametrize(
    "options, checkpoint",
    [
        ({"SEAMM": {"read_only": True}}, False),
        ({"SEAMM": {"database": ":memory:"}}, False),
        ({"SEAMM": {"database": "/somewhere/else/seamm.db"}}, False),
        ({"SEAMM": {}}, True),
    ],
)
def test_database_kept(tmp_path, options, checkpoint):
    """Not for a database elsewhere, read-only or in memory, nor with a
    checkpoint to resume from."""
    _previous_run(tmp_path)
    if checkpoint:
        (tmp_path / "checkpoint.json").write_text("{}")
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert ef.archive_previous_database(tmp_path, options) is None
    finally:
        os.chdir(cwd)
    assert (tmp_path / "seamm.db").exists()
    assert not (tmp_path / "previous").exists()


def test_first_run_has_nothing_to_archive(tmp_path):
    assert ef.archive_previous_database(Path(tmp_path), {"SEAMM": {}}) is None
