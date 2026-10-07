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


# ---- from the phase 3 review -------------------------------------------------


class PickyProvider(FakeProvider):
    """Runs only molecules as tasks; refuses 'bad' inputs outright."""

    @classmethod
    def can_run_task(cls, configuration, model_chemistry, *, options):
        return configuration.periodicity == 0

    @classmethod
    def get_task(cls, configuration, model_chemistry, *, key, properties, options):
        if options.get("refuse"):
            raise ValueError("cannot make this one")
        return super().get_task(
            configuration,
            model_chemistry,
            key=key,
            properties=properties,
            options=options,
        )


def test_structures_a_program_cannot_run_as_tasks(tmp_path, monkeypatch):
    box = Geometry([1], [[0, 0, 0]], cell=np.eye(3) * 5)
    molecule = Geometry([1], [[1, 0, 0]])

    # Without an MDI engine: a failed result, the rest still runs
    evaluator = Evaluator(_node(tmp_path, PickyProvider), MC)
    evaluator.submit(molecule, key="m")
    evaluator.submit(box, key="box")
    evaluator.submit(molecule, key="r", options={"refuse": 1})
    results = {r.key: r for r in evaluator.results()}
    assert results["m"].ok and results["m"].energy == 1.0
    assert not results["box"].ok and "has no MDI engine" in results["box"].reason
    assert not results["r"].ok and "cannot make this one" in results["r"].reason

    # With an MDI engine: the periodic structure goes there
    mc = dict(MC, options={"mdi_capable": True})

    class WithEngine(PickyProvider):
        get_mdi_engine_command = staticmethod(lambda *a, **k: None)

    evaluator = Evaluator(_node(tmp_path / "2", WithEngine), mc, path="batch")
    routed = {}

    def fake_mdi(pending, fallback=False):
        from seamm_exec import EvaluatorResult

        for key in pending:
            routed[key] = True
            yield EvaluatorResult(key=key, ok=True, energy=-1.0, path="mdi")

    monkeypatch.setattr(evaluator, "_mdi_results", fake_mdi)
    evaluator.submit(molecule, key="m")
    evaluator.submit(box, key="box")
    results = {r.key: r for r in evaluator.results()}
    assert results["m"].path == "batch" and results["box"].path == "mdi"
    assert routed == {"box": True}


# ---- from the design session's review ---------------------------------------


def test_a_refused_structure_without_a_local_engine_fails_alone(tmp_path):
    """On a queue target with the code only on the cluster, the structure the
    program cannot run as a task has no engine here: a failed result, and the
    others finish."""
    mc = dict(MC, options={"mdi_capable": True})

    class NoLocalCode(PickyProvider):
        @staticmethod
        def get_mdi_engine_command(*args, **kwargs):
            raise RuntimeError("No orca.ini here")

    evaluator = Evaluator(_node(tmp_path, NoLocalCode), mc, path="batch")
    evaluator.submit(Geometry([1], [[1, 0, 0]]), key="m")
    evaluator.submit(Geometry([1], [[0, 0, 0]], cell=np.eye(3) * 5), key="box")
    results = {r.key: r for r in evaluator.results()}
    assert results["m"].ok
    assert not results["box"].ok
    assert results["box"].reason.startswith("cannot run here")


def test_periodic_results_need_stress(tmp_path):
    class NoStress(FakeProvider):
        @classmethod
        def can_run_task(cls, configuration, model_chemistry, *, options):
            return True

    evaluator = Evaluator(
        _node(tmp_path, NoStress), MC, properties=("energy", "gradients", "stress")
    )
    evaluator.submit(Geometry([1], [[0, 0, 0]], cell=np.eye(3) * 5), key="box")
    evaluator.submit(Geometry([1], [[1, 0, 0]]), key="m")
    results = {r.key: r for r in evaluator.results()}
    assert not results["box"].ok and "no stress" in results["box"].reason
    assert results["m"].ok  # a molecule has no stress
    with pytest.raises(AnalysisError, match="has no stress"):
        check_properties({"energy": 1.0}, ("energy", "stress"), "x", periodic=True)


def test_resources_reach_get_task(tmp_path):
    """The Evaluator's resources are passed to the provider's get_task on the
    batch path, and omitted when not given (providers without the argument
    keep working)."""
    seen = []

    class Provider(FakeProvider):
        @classmethod
        def get_task(cls, configuration, model_chemistry, **kwargs):
            seen.append(kwargs.get("resources"))
            kwargs.pop("resources", None)
            return FakeProvider.get_task(configuration, model_chemistry, **kwargs)

    geometry = Geometry([8, 1, 1], [[0, 0, 0], [0.96, 0, 0], [-0.24, 0.93, 0]])
    resources = seamm_exec.Resources(ntasks=4, mem_per_cpu=1_500_000_000)
    with Evaluator(
        _node(tmp_path, Provider), MC, path="batch", resources=resources
    ) as evaluator:
        evaluator.submit(geometry, key="a")
        assert [r.ok for r in evaluator.results()] == [True]
    assert seen == [resources]

    seen.clear()
    with Evaluator(_node(tmp_path / "b", Provider), MC, path="batch") as evaluator:
        evaluator.submit(geometry, key="a")
        list(evaluator.results())
    assert seen == [None]


def test_evaluator_cancel_forwards_to_its_task_set():
    from types import SimpleNamespace

    from seamm_exec.evaluator import Evaluator

    ev = Evaluator.__new__(Evaluator)
    assert ev.cancelled is False
    calls = []
    ev._task_set = SimpleNamespace(cancel=lambda: calls.append("cancelled"))
    ev.cancel()
    assert ev.cancelled is True and calls == ["cancelled"]
    # Without a task set yet (nothing submitted), cancel just sets the flag
    ev2 = Evaluator.__new__(Evaluator)
    ev2.cancel()
    assert ev2.cancelled


def test_batch_path_passes_the_task_to_analyze_task(tmp_path):
    """A program's analyze_task that takes task= gets the Task that produced
    the result (it records the run's timing from it); one that does not take
    it is called as before."""
    from seamm_exec.evaluator import _takes_task
    from seamm_exec.tasks import Task

    seen = {}

    class Recording(FakeProvider):
        @classmethod
        def analyze_task(
            cls, result, model_chemistry, configuration, *, properties, options, task
        ):
            seen[result.key] = task
            return FakeProvider.analyze_task(
                result,
                model_chemistry,
                configuration,
                properties=properties,
                options=options,
            )

    assert _takes_task(Recording.analyze_task)
    assert not _takes_task(FakeProvider.analyze_task)
    assert _takes_task(lambda result, **kwargs: None)

    with Evaluator(_node(tmp_path, provider=Recording), MC) as evaluator:
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="a")
        results = {r.key: r for r in evaluator.results()}
    assert results["a"].ok
    assert isinstance(seen["a"], Task)
    assert seen["a"].key == "a"


def test_batch_path_without_the_task_when_a_provider_forwards_kwargs(tmp_path):
    """A provider whose analyze_task takes **kwargs but hands them to a function
    that does not take task= (mopac-step before 2026.10.7) is called without
    it, once the first call has refused it."""
    calls = []

    class Forwarding(FakeProvider):
        @classmethod
        def analyze_task(cls, result, model_chemistry, configuration, **kwargs):
            calls.append(sorted(kwargs))
            if "task" in kwargs:
                raise TypeError(
                    "analyze_task() got an unexpected keyword argument 'task'"
                )
            return FakeProvider.analyze_task(
                result, model_chemistry, configuration, **kwargs
            )

    with Evaluator(_node(tmp_path, provider=Forwarding), MC) as evaluator:
        evaluator.submit(Geometry([1], [[0, 0, 0]]), key="a")
        evaluator.submit(Geometry([1], [[0, 0, 1]]), key="b")
        results = {r.key: r for r in evaluator.results()}
    assert results["a"].ok and results["b"].ok
    # Refused once, then called without the task for the rest
    assert calls[0] == ["options", "properties", "task"]
    assert calls[1:] == [["options", "properties"]] * (len(calls) - 1)
