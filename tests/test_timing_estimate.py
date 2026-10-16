# -*- coding: utf-8 -*-
"""Where a task's estimate came from: the model's reasons for not predicting,
seamm_exec.timing_model.estimate, missing_sizes, and the record's
estimate_source."""

import csv
import logging

import pytest

from seamm_exec import Resources, Task
from seamm_exec import timing_model as tm
from seamm_exec.timing import record_task_timing, record_timing, timing_path

from .test_timing_model import _synthetic

MACHINE = "fast:q:CPU-A"
GOOD = {"task": "energy", "method_class": "MP2", "nbf": 240, "n_electrons": 120}


@pytest.fixture
def model(tmp_path):
    _synthetic(tmp_path, n=200)
    model = tm.fit("orca", tmp_path)
    tm.save_model(model, tmp_path)
    return model


@pytest.fixture(autouse=True)
def _fresh_log_memory(monkeypatch):
    monkeypatch.setattr(tm, "_logged_reasons", set())


def _why(model, **changes):
    why = []
    descriptors = {**GOOD, **changes}
    descriptors = {k: v for k, v in descriptors.items() if v is not None}
    result = tm.predict("orca", descriptors, model=model, machine=MACHINE, why=why)
    return result, why


def test_predict_says_why_not(model):
    result, why = _why(model)
    assert result is not None and why == []
    assert model["features"][0] == "nbf"
    result, why = _why(model, nbf=None)
    assert result is None and why == ["missing nbf"]
    result, why = _why(model, nbf=10**6)
    assert result is None and why[0].startswith("nbf = 1e+06 outside the records'")
    result, why = _why(model, method_class="CCSD(T)")
    assert why == ["method class 'CCSD(T)' not in the records"]
    result, why = _why(model, task="freq")
    assert why == ["task 'freq' not in the records"]
    why = []
    assert tm.predict("vasp", GOOD, directory=model and "/nonexistent", why=why) is None
    assert why == ["no model of vasp"]


def test_estimate_sources(tmp_path, model, caplog):
    got = tm.estimate("orca", GOOD, fallback=999.0, directory=tmp_path, machine=MACHINE)
    assert got["source"] == "model" and got["reason"] is None
    assert got["seconds"] != 999.0
    caplog.set_level(logging.INFO, logger="seamm-exec")
    lacking = {k: v for k, v in GOOD.items() if k != "nbf"}
    got = tm.estimate("orca", lacking, fallback=999.0, directory=tmp_path)
    assert got == {"seconds": 999.0, "source": "formula", "reason": "missing nbf"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "missing nbf" in warnings[0].getMessage()
    # Once per process and reason
    tm.estimate("orca", lacking, fallback=999.0, directory=tmp_path)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    # A calculation the records lack is no fault of the step: info, not a warning
    caplog.clear()
    got = tm.estimate(
        "orca", {**GOOD, "task": "freq"}, fallback=5.0, directory=tmp_path
    )
    assert got["source"] == "formula"
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    # No model and no formula
    got = tm.estimate("vasp", GOOD, directory=tmp_path)
    assert got == {"seconds": None, "source": "none", "reason": "no model of vasp"}


def test_estimate_survives_a_failing_model(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(tm, "predict", boom)
    got = tm.estimate("orca", GOOD, fallback=7.0)
    assert got == {
        "seconds": 7.0,
        "source": "formula",
        "reason": "the model failed: broken",
    }


def test_missing_sizes():
    spec = tm.Spec(size=("nbf", "neighbours", "n_electrons"))
    assert tm.missing_sizes(spec, GOOD) == ["neighbours"]
    assert tm.missing_sizes(spec.to_dict(), {**GOOD, "neighbours": 12.5}) == []
    assert tm.missing_sizes(spec, {**GOOD, "neighbours": 0}) == ["neighbours"]


def _rows(tmp_path):
    with open(timing_path("orca", tmp_path), newline="") as fd:
        return list(csv.DictReader(fd))


def test_records_carry_the_source(tmp_path):
    record_timing("orca", 10.0, {"nbf": 24}, estimated=12.0, directory=tmp_path)
    record_timing(
        "orca",
        10.0,
        {"nbf": 24},
        estimated=12.0,
        estimate_source="model",
        directory=tmp_path,
    )
    record_timing("orca", 10.0, {"nbf": 24}, directory=tmp_path)
    task = Task(
        key="t",
        program="orca",
        resources=Resources(ntasks=2),
        estimated_seconds=30.0,
        estimate_source="formula",
    )
    result = type("R", (), {"state": "finished", "history": [], "attempts": 1})()
    record_task_timing(task, result, {"nbf": 24}, directory=tmp_path)
    assert [r["estimate_source"] for r in _rows(tmp_path)] == [
        "",
        "model",
        "none",
        "formula",
    ]
