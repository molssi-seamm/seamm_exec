# -*- coding: utf-8 -*-

"""Tests for the model-chemistry Evaluator: the path rule, the batch path with
a fake provider, and the helpers."""

import logging
import types

import numpy as np
import pytest

import seamm_exec
from seamm_exec import (
    AnalysisError,
    Evaluator,
    Geometry,
    Task,
    check_properties,
    choose_path,
    mdi_method_and_basis,
    structure_data,
)


class FakeProvider:
    """A program whose 'calculation' writes E = sum of coordinates and
    gradients = 1 for every coordinate, through a real shell task."""

    calls = []

    @classmethod
    def get_task(cls, configuration, model_chemistry, *, key, properties, options):
        data = structure_data(configuration)
        energy = float(data["coordinates"].sum()) + float(options.get("shift", 0))
        cls.calls.append(key)
        fail = "exit 3" if options.get("fail") else "true"
        return Task(
            key=key,
            program="fake",
            cmd=[fail, "&&", "echo", str(energy), ">", "energy.txt"],
            shell=True,
            return_files=["energy.txt"],
            config={"installation": "local"},
        )

    @classmethod
    def analyze_task(
        cls, result, model_chemistry, configuration, *, properties, options
    ):
        text = result.files.get("energy.txt")
        data = {}
        if text:
            data["energy"] = float(text)
            data["gradients"] = np.ones((configuration.n_atoms, 3))
        if options.get("drop"):
            data.pop("gradients", None)
        check_properties(data, properties, f"'{result.key}'")
        return data


def _node(tmp_path, provider=FakeProvider):
    return types.SimpleNamespace(
        directory=str(tmp_path / "step"),
        global_options={"root": str(tmp_path)},
        logger=logging.getLogger("test"),
        variable_exists=lambda name: False,
        flowchart=types.SimpleNamespace(
            executor=seamm_exec.Local(),
            plugin_manager=types.SimpleNamespace(get=lambda name: provider),
            root_directory=str(tmp_path),
        ),
    )


MC = {
    "level": "FAKE:X@Y",
    "method": "Y",
    "basis": None,
    "step": "fake",
    "options": {"mdi_capable": False},
}


# ---- the path rule ----------------------------------------------------------


class Both:
    get_task = analyze_task = get_mdi_engine_command = staticmethod(lambda: None)


class MdiOnly:
    get_mdi_engine_command = staticmethod(lambda: None)


def test_choose_path():
    queue = types.SimpleNamespace(tasks="queue")
    pool = types.SimpleNamespace(tasks="pool")
    mdi = {"options": {"mdi_capable": True}}
    prefers = {"options": {"mdi_capable": True, "prefers_batch": True}}
    no_mdi = {"options": {"mdi_capable": False}}
    # Local: MDI for a warm engine...
    assert choose_path(mdi, Both, None) == "mdi"
    assert choose_path(mdi, Both, pool) == "mdi"
    # ...unless the program prefers tasks (ORCA)
    assert choose_path(prefers, Both, None) == "batch"
    # A queue sends tasks out whenever the program has them
    assert choose_path(mdi, Both, queue) == "batch"
    # ...but an MDI-only program stays on MDI
    assert choose_path(mdi, MdiOnly, queue) == "mdi"
    # No MDI engine: tasks
    assert choose_path(no_mdi, Both, None) == "batch"
    with pytest.raises(ValueError, match="neither over MDI nor as tasks"):
        choose_path({"level": "x", "options": {}}, MdiOnly, None)


def test_mdi_method_and_basis():
    assert mdi_method_and_basis(
        {
            "method": "wB97X",
            "basis": "def2-TZVP",
            "options": {"mdi_method_arg": "WB97X", "mdi_basis_arg": "def2-TZVP"},
        }
    ) == ("WB97X", "def2-TZVP")
    assert mdi_method_and_basis({"method": "PM6", "basis": None, "options": {}}) == (
        "PM6",
        None,
    )


# ---- the batch path -----------------------------------------------------------


def test_batch_path_results_failures_and_restart(tmp_path):
    FakeProvider.calls = []
    with Evaluator(_node(tmp_path), MC) as evaluator:
        assert evaluator.path == "batch"
        evaluator.submit(Geometry([1, 1], [[0, 0, 0], [0, 0, 1]]), key="a")
        evaluator.submit(Geometry([1], [[1, 1, 1]]), key="b", options={"shift": 10})
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="bad", options={"fail": 1})
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="part", options={"drop": 1})
        results = {r.key: r for r in evaluator.results()}
    assert results["a"].ok and results["a"].energy == 1.0
    assert results["a"].gradients.shape == (2, 3)
    assert results["b"].energy == 13.0
    assert not results["bad"].ok and "return code 3" in results["bad"].reason
    assert not results["part"].ok and "has no gradients" in results["part"].reason
    assert all(r.path == "batch" for r in results.values())

    # A rerun restores the finished ones, and runs the failed ones again
    with Evaluator(_node(tmp_path), MC) as evaluator:
        evaluator.submit(Geometry([1, 1], [[0, 0, 0], [0, 0, 1]]), key="a")
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="bad", options={"fail": 1})
        again = {r.key: r for r in evaluator.results()}
    assert again["a"].restored and again["a"].energy == 1.0
    assert not again["bad"].ok


def test_duplicate_keys_are_refused(tmp_path):
    evaluator = Evaluator(_node(tmp_path), MC)
    evaluator.submit(Geometry([1], [[0, 0, 0]]), key="x")
    with pytest.raises(ValueError, match="Duplicate key"):
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="x")


def test_mdi_path_refuses_fragment_options(tmp_path):
    evaluator = Evaluator(_node(tmp_path), MC, path="mdi")
    evaluator.submit(Geometry([1], [[0, 0, 0]]), options={"ghost_atoms": [0]})
    with pytest.raises(ValueError, match="cannot take 'ghost_atoms'"):
        list(evaluator.results())


# ---- helpers -----------------------------------------------------------------


def test_geometry_and_structure_data():
    g = Geometry([8, 1, 1], [[0, 0, 0], [0, 0, 1], [0, 1, 0]], charge=-1)
    data = structure_data(g)
    assert data["symbols"] == ["O", "H", "H"]
    assert data["charge"] == -1 and data["multiplicity"] == 1
    assert data["periodicity"] == 0 and data["cell"] is None
    assert data["coordinates"].shape == (3, 3)
    box = Geometry([8], [[0, 0, 0]], cell=np.eye(3) * 4)
    assert structure_data(box)["periodicity"] == 3
    assert structure_data(box)["cell"][0, 0] == 4.0


def test_check_properties():
    check_properties({"energy": 1.0}, ("energy", "stress"), "x")  # stress optional
    with pytest.raises(AnalysisError, match="x has no gradients"):
        check_properties({"energy": 1.0}, ("energy", "gradients"), "x")
