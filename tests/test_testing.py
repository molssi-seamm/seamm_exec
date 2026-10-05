# -*- coding: utf-8 -*-

"""The helpers for testing a code step's run path (seamm_exec.testing)."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from seamm_exec.testing import fake_program, find_program, run_spec, table_value

SOURCE = Path(__file__).resolve().parents[1]


def test_run_spec_runs_the_water_step(tmp_path):
    job = run_spec(
        tmp_path,
        """\
        title: Water only
        steps:
        - Water: {}
        """,
        inis={"mopac": {"code": "/nowhere/mopac"}},
        source=SOURCE,
    )
    db = sqlite3.connect(f"file:{job / 'seamm.db'}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT name FROM system").fetchall() == [("water",)]
        (n,) = db.execute("SELECT COUNT(*) FROM atom").fetchone()
    finally:
        db.close()
    assert n == 3
    # The ini written in the run's own root, never the user's
    text = (tmp_path / "root" / "mopac.ini").read_text()
    assert "[local]" in text and "installation = local" in text
    assert "code = /nowhere/mopac" in text


def test_run_spec_reports_a_failed_build(tmp_path):
    with pytest.raises(AssertionError, match="build"):
        run_spec(tmp_path, "title: x\nsteps:\n- NoSuchStep: {}\n", source=SOURCE)


def test_fake_program_copies_and_prints(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "code.out").write_text("the output\n")
    (data / "code.aux").write_text("aux\n")
    fake = fake_program(
        tmp_path / "bin" / "code",
        files=[data / "code.aux"],
        stdout=data / "code.out",
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "code.inp").write_text("input\n")
    result = subprocess.run(
        f"{fake} code.inp > code.out", shell=True, cwd=run, capture_output=True
    )
    assert result.returncode == 0
    assert (run / "code.out").read_text() == "the output\n"
    assert (run / "code.aux").read_text() == "aux\n"
    # No input: fails, as the program would
    (run / "empty.inp").write_text("")
    result = subprocess.run([fake, "empty.inp"], cwd=run, capture_output=True)
    assert result.returncode != 0


def test_find_program(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_EXE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert find_program("codex", root=tmp_path) is None
    exe = tmp_path / "opt" / "codex"
    exe.parent.mkdir()
    exe.write_text("")
    (tmp_path / "codex.ini").write_text(
        f"[local]\ninstallation = local\ncode = {exe}\n"
    )
    assert find_program("codex", root=tmp_path) == str(exe)
    # From a conda environment
    conda = tmp_path / "conda"
    env_exe = conda / "envs" / "seamm-codex" / "bin" / "codex"
    env_exe.parent.mkdir(parents=True)
    env_exe.write_text("")
    (tmp_path / "codex.ini").write_text(
        "[local]\ninstallation = conda\ncode = codex\n"
        f"conda = {conda / 'bin' / 'conda'}\nconda-environment = seamm-codex\n"
    )
    assert find_program("codex", root=tmp_path) == str(env_exe)
    monkeypatch.setenv("CODEX_EXE", "/explicit/codex")
    assert find_program("codex", root=tmp_path) == "/explicit/codex"


def test_table_value():
    text = (
        "   | Enthalpy of Formation |  -55.03 | kcal/mol |\n"
        "   │ Total energy          │ -76.32100225 │ E_h     │\n"
    )
    assert table_value(text, "Enthalpy of Formation") == -55.03
    assert table_value(text, "Total energy") == -76.32100225
    with pytest.raises(AssertionError):
        table_value(text, "Dipole Moment")
