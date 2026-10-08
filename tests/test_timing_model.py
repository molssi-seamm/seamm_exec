# -*- coding: utf-8 -*-
"""The cost model fitted to the timing records (seamm_exec.timing_model)."""

import math
import random
import subprocess
import sys

from seamm_exec import timing_model as tm
from seamm_exec.timing import append_timing

MACHINES = {"fast:q:CPU-A": -0.4, "slow:q:CPU-B": 0.4}
CLASSES = {"global hybrid": 0.0, "MP2": 1.2}
TASKS = {"energy": (1, 1), "opt": (8, 25)}  # units range


def _synthetic(tmp_path, n=400, seed=1, alpha=0.7, b_nbf=2.6):
    """Rows from a known law: t = t0 + units * exp(a + b log nbf - alpha log cores
    + class + machine) * noise."""
    rng = random.Random(seed)
    for i in range(n):
        machine = rng.choice(list(MACHINES))
        klass = rng.choice(list(CLASSES))
        task = rng.choice(list(TASKS))
        units = rng.randint(*TASKS[task])
        nbf = rng.choice([24, 60, 120, 240, 480, 960])
        cores = rng.choice([1, 2, 4, 8, 16])
        log_unit = -9.0 + b_nbf * math.log(nbf) - alpha * math.log(cores)
        log_unit += CLASSES[klass] + MACHINES[machine] + rng.gauss(0, 0.15)
        wall = 0.8 + units * math.exp(log_unit)
        append_timing(
            "orca",
            {
                "schema": 1,
                "machine": machine,
                "program": "orca",
                "ntasks": cores,
                "cpus_per_task": 1,
                "wall": f"{wall:.3f}",
                "state": "finished",
                "task": task,
                "method_class": klass,
                "nbf": nbf,
                "n_electrons": nbf // 2,
                "n_atoms": nbf // 10,
                "scf_runs": units,
            },
            directory=tmp_path,
        )


def test_fit_recovers_the_law(tmp_path):
    _synthetic(tmp_path)
    model = tm.fit("orca", directory=tmp_path)
    assert model is not None and model["rows"] >= 380
    assert model["features"] == ["nbf"]  # electrons and atoms are collinear with it
    assert abs(model["coefficients"]["log nbf"] - 2.6) < 0.15
    assert model["alpha_fitted"] and abs(model["alpha"] - 0.7) < 0.1
    # Class and machine effects (differences, since each set is centred)
    assert abs(model["classes"]["MP2"] - model["classes"]["global hybrid"] - 1.2) < 0.15
    fast = model["machines"]["fast:q:CPU-A"]["offset"]
    slow = model["machines"]["slow:q:CPU-B"]["offset"]
    assert abs((slow - fast) - 0.8) < 0.15
    assert model["report"]["within_2x"] > 0.9
    assert model["report"]["r2_log"] > 0.95
    # The unit distribution per task
    assert model["task_units_quantiles"]["energy"]["0.95"] == 1.0
    assert model["task_units_quantiles"]["opt"]["0.5"] > 10


def test_predict(tmp_path):
    _synthetic(tmp_path)
    model = tm.fit("orca", directory=tmp_path)
    path = tm.save_model(model, directory=tmp_path)
    assert path.exists()
    d = {"task": "energy", "method_class": "global hybrid", "nbf": 240}
    p50 = tm.predict(
        "orca", d, ntasks=4, machine="fast:q:CPU-A", quantile=0.5, directory=tmp_path
    )
    p95 = tm.predict(
        "orca", d, ntasks=4, machine="fast:q:CPU-A", quantile=0.95, directory=tmp_path
    )
    truth = 0.8 + math.exp(-9.0 + 2.6 * math.log(240) - 0.7 * math.log(4) - 0.4)
    assert abs(p50["median"] / truth - 1) < 0.25
    assert p95["seconds"] > p50["seconds"] > 0
    assert p50["machine_known"] is True and p50["units"] == 1.0
    # More cores: faster; MP2: slower; an optimization: many units
    p_more = tm.predict(
        "orca", d, ntasks=16, machine="fast:q:CPU-A", quantile=0.5, directory=tmp_path
    )
    assert p_more["median"] < p50["median"]
    p_mp2 = tm.predict(
        "orca",
        {**d, "method_class": "MP2"},
        ntasks=4,
        machine="fast:q:CPU-A",
        quantile=0.5,
        directory=tmp_path,
    )
    assert p_mp2["median"] > 2 * p50["median"]
    p_opt = tm.predict(
        "orca",
        {**d, "task": "opt"},
        ntasks=4,
        machine="fast:q:CPU-A",
        quantile=0.95,
        directory=tmp_path,
    )
    assert p_opt["units"] >= 20 and p_opt["seconds"] > 10 * p95["seconds"]
    given = tm.predict(
        "orca",
        {**d, "task": "opt"},
        ntasks=4,
        machine="fast:q:CPU-A",
        quantile=0.5,
        units=10,
        directory=tmp_path,
    )
    assert given["units"] == 10
    # An unknown machine: no offset, wider spread
    unknown = tm.predict(
        "orca", d, ntasks=4, machine="new:cluster", quantile=0.95, directory=tmp_path
    )
    assert unknown["machine_known"] is False
    assert unknown["spread"] > p95["spread"]
    # No size variable, or no model: None
    assert tm.predict("orca", {"task": "energy"}, directory=tmp_path) is None
    assert tm.predict("nothing", d, directory=tmp_path) is None


def test_too_few_rows(tmp_path):
    _synthetic(tmp_path, n=5)
    assert tm.fit("orca", directory=tmp_path) is None


def test_serial_program_keeps_the_default_alpha(tmp_path):
    for i in range(30):
        append_timing(
            "mopac",
            {
                "schema": 1,
                "machine": "m",
                "program": "mopac",
                "ntasks": 1,
                "cpus_per_task": 1,
                "wall": f"{0.5 + 1e-4 * (10 * (i + 1)) ** 2.5:.3f}",
                "state": "finished",
                "task": "energy",
                "hamiltonian": "PM7",
                "regime": "scf",
                "n_basis": 10 * (i + 1),
                "n_atoms": 3 * (i + 1),
                "scf_runs": 1,
            },
            directory=tmp_path,
        )
    model = tm.fit("mopac", directory=tmp_path)
    assert model["alpha_fitted"] is False and model["alpha"] == 0.0
    assert model["classes"] == {"PM7 / scf": 0.0}
    assert abs(model["coefficients"]["log n_basis"] - 2.5) < 0.3


def test_command_line(tmp_path):
    _synthetic(tmp_path, n=120)
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "seamm_exec.timing_model",
            "--directory",
            str(tmp_path),
            "fit",
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "orca:" in out.stdout and "within 2x" in out.stdout
    assert (tmp_path / "models" / "orca.json").exists()
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "seamm_exec.timing_model",
            "--directory",
            str(tmp_path),
            "predict",
            "orca",
            "task=energy",
            "method_class=MP2",
            "nbf=480",
            "--ntasks",
            "8",
            "--machine",
            "fast:q:CPU-A",
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "95% quantile" in out.stdout and "known" in out.stdout


def test_size_dependent_parallel_exponent(tmp_path):
    """Small molecules do not speed up with cores, large ones do: the fitted
    exponent grows with size and predict() uses the size's exponent."""
    rng = random.Random(3)
    for i in range(400):
        nbf = rng.choice([24, 60, 120, 240, 480, 960])
        cores = rng.choice([1, 2, 4, 8])
        alpha = 0.1 + 0.25 * (math.log(nbf) - math.log(150))  # ~0 small .. ~0.6 big
        log_unit = -9.0 + 2.5 * math.log(nbf) - alpha * math.log(cores)
        wall = 0.5 + math.exp(log_unit + rng.gauss(0, 0.1))
        append_timing(
            "orca",
            {
                "schema": 1,
                "machine": "m",
                "program": "orca",
                "ntasks": cores,
                "cpus_per_task": 1,
                "wall": f"{wall:.4f}",
                "state": "finished",
                "task": "energy",
                "method_class": "global hybrid",
                "nbf": nbf,
                "scf_runs": 1,
            },
            directory=tmp_path,
        )
    model = tm.fit("orca", directory=tmp_path)
    assert model["alpha_slope"] > 0.15
    small = {"task": "energy", "method_class": "global hybrid", "nbf": 24}
    big = {"task": "energy", "method_class": "global hybrid", "nbf": 960}
    s1 = tm.predict("orca", small, ntasks=1, machine="m", quantile=0.5, model=model)
    s8 = tm.predict("orca", small, ntasks=8, machine="m", quantile=0.5, model=model)
    b1 = tm.predict("orca", big, ntasks=1, machine="m", quantile=0.5, model=model)
    b8 = tm.predict("orca", big, ntasks=8, machine="m", quantile=0.5, model=model)
    assert s1["median"] / s8["median"] < 1.5  # little gain for the small one
    assert b1["median"] / b8["median"] > 2.5  # a real gain for the big one


def test_predict_refits_when_the_records_grow(tmp_path, monkeypatch):
    _synthetic(tmp_path, n=200, seed=5)
    d = {"task": "energy", "method_class": "global hybrid", "nbf": 240}
    # No model yet: the first prediction fits one
    assert tm.load_model("orca", tmp_path) is None
    assert tm.predict("orca", d, machine="fast:q:CPU-A", directory=tmp_path) is not None
    first = tm.load_model("orca", tmp_path)
    assert first["rows"] >= 190
    # Fresh: predicting again does not refit
    assert tm.refresh_if_stale("orca", tmp_path) == "fresh"
    # A tenth more records: still fresh; a third more: refitted to more rows
    _synthetic(tmp_path, n=20, seed=6)
    assert tm.refresh_if_stale("orca", tmp_path) == "fresh"
    _synthetic(tmp_path, n=80, seed=7)
    tm.predict("orca", d, machine="fast:q:CPU-A", directory=tmp_path)
    second = tm.load_model("orca", tmp_path)
    assert second["rows"] > first["rows"] and second["fitted"] >= first["fitted"]
    # Old and changed: refitted even without growth
    second["fitted"] = "2020-01-01T00:00:00+00:00"
    second["records_mtime"] = 0
    tm.save_model(second, tmp_path)
    assert tm.refresh_if_stale("orca", tmp_path) == "refitted"
    # Old but unchanged records: not refitted
    model = tm.load_model("orca", tmp_path)
    model["fitted"] = "2020-01-01T00:00:00+00:00"
    tm.save_model(model, tmp_path)
    assert tm.refresh_if_stale("orca", tmp_path) == "fresh"


def test_refit_keeps_a_better_old_model(tmp_path, monkeypatch):
    _synthetic(tmp_path, n=200, seed=8)
    tm.save_model(tm.fit("orca", tmp_path), tmp_path)
    old = tm.load_model("orca", tmp_path)
    # Pretend the records grew, and make the refit come out worse
    old["records_bytes"] = 1
    tm.save_model(old, tmp_path)
    worse = dict(old)
    worse["rows"] = 50
    monkeypatch.setattr(tm, "fit", lambda *a, **k: worse)
    assert tm.refresh_if_stale("orca", tmp_path) == "failed"
    kept = tm.load_model("orca", tmp_path)
    assert kept["rows"] == old["rows"]
    assert kept["records_bytes"] > 1  # the state was noted, so no retry storm
    assert tm.refresh_if_stale("orca", tmp_path) == "fresh"


def test_refit_failure_leaves_the_model_alone(tmp_path, monkeypatch):
    _synthetic(tmp_path, n=200, seed=9)
    tm.save_model(tm.fit("orca", tmp_path), tmp_path)
    old = tm.load_model("orca", tmp_path)
    old["records_bytes"] = 1
    tm.save_model(old, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(tm, "fit", boom)
    assert tm.refresh_if_stale("orca", tmp_path) == "failed"
    d = {"task": "energy", "method_class": "global hybrid", "nbf": 240}
    assert tm.predict("orca", d, machine="fast:q:CPU-A", directory=tmp_path) is not None


def test_refit_skipped_while_another_process_holds_the_lock(tmp_path):
    """lockf locks are per process, so the holder must be another process."""
    import time as _time

    _synthetic(tmp_path, n=200, seed=10)
    tm.save_model(tm.fit("orca", tmp_path), tmp_path)
    old = tm.load_model("orca", tmp_path)
    old["records_bytes"] = 1
    tm.save_model(old, tmp_path)
    lock_path = tm.model_path("orca", tmp_path).with_suffix(".lock")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time; fd = open(sys.argv[1], 'w'); "
            "fcntl.lockf(fd.fileno(), fcntl.LOCK_EX); print('held', flush=True); "
            "time.sleep(30)",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert tm.refresh_if_stale("orca", tmp_path) == "locked"
    finally:
        holder.kill()
        holder.wait()
    _time.sleep(0.1)
    assert tm.refresh_if_stale("orca", tmp_path) == "refitted"


def test_spec_sidecar_round_trip_and_use(tmp_path):
    """A step's spec, passed when it records, is written beside the records and
    read by the fit; a program without one uses the fallback or default."""
    from seamm_exec.timing import record_timing

    spec = {
        "size": ["n_basis", "n_atoms"],
        "klass": ["hamiltonian"],
        "task": "task",
        "units": "scf_runs",
        "multiplier": None,
        "default_alpha": 0.0,
    }
    for i in range(12):
        record_timing(
            "mycode",
            1.0 + 0.001 * (10 * (i + 1)) ** 2.5,
            {
                "n_basis": 10 * (i + 1),
                "n_atoms": 3 * (i + 1),
                "hamiltonian": "X",
                "task": "energy",
                "scf_runs": 1,
            },
            directory=tmp_path,
            spec=spec,
        )
    path = tm.spec_path("mycode", tmp_path)
    assert path.exists()
    loaded = tm.load_spec("mycode", tmp_path)
    assert loaded.size == ("n_basis", "n_atoms") and loaded.klass == ("hamiltonian",)
    assert loaded.default_alpha == 0.0
    model = tm.fit("mycode", tmp_path)
    assert model["features"][0] == "n_basis" and model["spec"]["units"] == "scf_runs"
    assert model["alpha"] == 0.0  # the spec's default, no core spread
    # Unchanged spec: the file is not rewritten
    mtime = path.stat().st_mtime
    tm.write_spec("mycode", spec, tmp_path)
    assert path.stat().st_mtime == mtime
    # No sidecar: the fallback table, else the default
    assert tm.load_spec("orca", tmp_path).size[0] == "nbf"
    assert tm.load_spec("unknown", tmp_path) == tm.DEFAULT_SPEC


def _cores_by_size(tmp_path, n=300, seed=3):
    """Rows where the cores were chosen by the size: 4 for the small runs, 8
    for the large (science's label jobs on TinkerCliffs)."""
    rng = random.Random(seed)
    for i in range(n):
        nbf = rng.choice([100, 150, 200, 250, 400, 500, 600, 700])
        cores = 4 if nbf < 300 else 8
        log_unit = -9.0 + 2.6 * math.log(nbf) - 0.5 * math.log(cores)
        wall = 1.0 + math.exp(log_unit + rng.gauss(0, 0.15))
        append_timing(
            "orca",
            {
                "schema": 1,
                "machine": "tc:normal_q:EPYC",
                "program": "orca",
                "ntasks": cores,
                "cpus_per_task": 1,
                "wall": f"{wall:.3f}",
                "state": "finished",
                "task": "gradient",
                "method_class": "global double-hybrid",
                "nbf": nbf,
                "n_electrons": nbf // 6,
                "scf_runs": 1,
            },
            directory=tmp_path,
        )


def test_cores_chosen_by_size_do_not_fit_alpha(tmp_path):
    """When the core count follows the size, the parallel exponent cannot be
    told from the size's effect: it is assumed, and no size slope is fitted
    (the first TinkerCliffs fit gave alpha = 0 + 3.5 per log unit, i.e. perfect
    scaling for anything large, and a 35-minute fragment 10 minutes)."""
    _cores_by_size(tmp_path)
    model = tm.fit("orca", directory=tmp_path)
    assert model is not None
    assert not model["alpha_fitted"]
    assert "cores follow the size" in model["alpha_note"]
    assert model["alpha_slope"] == 0.0
    assert model["alpha"] == model["spec"]["default_alpha"]
    assert "assumed" in tm.report_text(model)
    low, high = model["feature_log_range"]["nbf"]
    assert round(math.exp(low)) == 100 and round(math.exp(high)) == 700


def test_predict_refuses_what_the_records_do_not_cover(tmp_path):
    _cores_by_size(tmp_path)
    model = tm.fit("orca", directory=tmp_path)
    inside = {
        "task": "gradient",
        "method_class": "global double-hybrid",
        "nbf": 644,
        "n_electrons": 108,
    }
    assert tm.predict("orca", inside, ntasks=4, model=model) is not None
    # Just beyond the range is still interpolation; far beyond is not
    near = dict(inside, nbf=1000, n_electrons=160)
    assert tm.predict("orca", near, ntasks=4, model=model) is not None
    far = dict(inside, nbf=1100, n_electrons=180)
    assert tm.predict("orca", far, ntasks=4, model=model) is None
    small = dict(inside, nbf=24, n_electrons=10)
    assert tm.predict("orca", small, ntasks=4, model=model) is None
    # A size variable missing, a method class or task the records lack
    assert (
        tm.predict("orca", {k: v for k, v in inside.items() if k != "nbf"}, model=model)
        is None
    )
    assert tm.predict("orca", dict(inside, method_class="MP2"), model=model) is None
    assert tm.predict("orca", dict(inside, method_class=""), model=model) is None
    assert tm.predict("orca", dict(inside, task="freq"), model=model) is None


def _rows(tmp_path, program, entries):
    """Write synthetic records: each entry a dict of descriptors plus wall."""
    for e in entries:
        row = {
            "schema": 1,
            "machine": "m:q:CPU",
            "program": program,
            "ntasks": e.pop("ntasks", 1),
            "cpus_per_task": 1,
            "state": "finished",
            "task": e.pop("task", "energy"),
        }
        row.update(e)
        row["wall"] = f"{row['wall']:.4f}"
        append_timing(program, row, directory=tmp_path)


def test_no_size_exponent_is_negative(tmp_path):
    """Two size variables that are related but not collinear can split one
    effect into a large positive and a negative exponent; the fit leaves out a
    variable rather than keep a negative exponent, and says so."""
    rng = random.Random(5)
    entries = []
    for i in range(120):
        a = rng.choice([20, 40, 80, 160, 320, 640])
        b = a * rng.choice([0.5, 0.7, 1.0, 1.4, 2.0])
        wall = 1.0 + math.exp(-8 + 3.0 * math.log(a) - 1.0 * math.log(b))
        entries.append(
            {"size_a": a, "size_b": b, "wall": wall * rng.uniform(0.95, 1.05)}
        )
    _rows(tmp_path, "testcode", entries)
    spec = tm.Spec(size=("size_a", "size_b"), klass=(), units=None)
    model = tm.fit("testcode", directory=tmp_path, spec=spec)
    slopes = [model["coefficients"][f"log {n}"] for n in model["features"]]
    assert all(s >= 0 for s in slopes)
    assert any("left out" in note for note in model["notes"])
    assert "note:" in tm.report_text(model)


def test_each_regime_has_its_own_exponent(tmp_path):
    """slope_by gives each value of a column its own size exponent (MOPAC's
    MOZYME, roughly linear, and traditional SCF, roughly cubic)."""
    rng = random.Random(7)
    entries = []
    for regime, a, b in (("linear", -3.0, 1.0), ("cubic", -14.0, 3.0)):
        for n in (100, 200, 400, 800, 1600) * 4:
            code = math.exp(a + b * math.log(n) + rng.gauss(0, 0.05))
            entries.append(
                {"regime": regime, "n": n, "wall": 0.5 + code, "code_seconds": code}
            )
    _rows(tmp_path, "testcode", entries)
    spec = tm.Spec(size=("n",), klass=("regime",), units=None, slope_by="regime")
    model = tm.fit("testcode", directory=tmp_path, spec=spec)
    base = model["coefficients"]["log n"]
    other = {g: base + d["n"] for g, d in model["slope_deltas"].items()}
    reference = [g for g in model["slope_groups"] if g not in other][0]
    other[reference] = base
    assert abs(other["linear"] - 1.0) < 0.15
    assert abs(other["cubic"] - 3.0) < 0.15
    d = {"task": "energy", "regime": "cubic", "n": 800}
    assert tm.predict("testcode", d, model=model) is not None
    assert tm.predict("testcode", dict(d, regime="other"), model=model) is None


def test_large_runs_weigh_more(tmp_path):
    """The cost exponent grows with size; weighting runs by their time keeps
    the large end, which sets walltimes, from being predicted low."""
    rng = random.Random(9)
    entries = []
    for n in (20, 40, 80, 160, 320, 640) * 6:
        x = math.log(n)
        wall = 1.0 + math.exp(-6 + 1.0 * x + 0.15 * x * x + rng.gauss(0, 0.03))
        entries.append({"n": n, "wall": wall})
    _rows(tmp_path, "testcode", entries)
    spec = tm.Spec(size=("n",), klass=(), units=None)
    largest = 1.0 + math.exp(-6 + math.log(640) + 0.15 * math.log(640) ** 2)
    ratios = {}
    for power in (0.0, tm.WEIGHT_POWER):
        model = tm.fit("testcode", directory=tmp_path, spec=spec, weight_power=power)
        p = tm.predict("testcode", {"task": "energy", "n": 640}, model=model)
        ratios[power] = p["median"] / largest
    assert abs(ratios[tm.WEIGHT_POWER] - 1) < abs(ratios[0.0] - 1)
    assert ratios[tm.WEIGHT_POWER] > 0.85


def test_a_fixed_setup_per_run(tmp_path):
    """A code whose first step is much dearer than the rest (MOZYME localizes
    the orbitals once): with setup_by the fit finds the setup in iterations,
    so a single point and a long optimization share one per-cycle cost."""
    rng = random.Random(11)
    entries = []
    for n in (200, 400, 800, 1600) * 3:
        for task, cycles in (("energy", 0), ("opt", 40), ("opt", 150)):
            per_cycle = math.exp(-9 + 1.0 * math.log(n) + rng.gauss(0, 0.03))
            code = (max(cycles, 1) + 20) * per_cycle  # setup = 20 cycles
            entries.append(
                {
                    "task": task,
                    "regime": "mozyme",
                    "n": n,
                    "cycles": cycles,
                    "wall": 0.5 + code,
                    "code_seconds": code,
                }
            )
    _rows(tmp_path, "testcode", entries)
    plain = tm.Spec(size=("n",), klass=("regime",), units="cycles")
    with_setup = tm.Spec(
        size=("n",), klass=("regime",), units="cycles", setup_by="regime"
    )
    errors = {}
    for name, spec in (("plain", plain), ("setup", with_setup)):
        model = tm.fit("testcode", directory=tmp_path, spec=spec)
        worst = 0.0
        # optimizations far shorter and longer than the records': where a
        # per-cycle cost that has absorbed the setup goes wrong
        for task, cycles in (("energy", 0), ("opt", 5), ("opt", 600)):
            d = {"task": task, "regime": "mozyme", "n": 1600}
            p = tm.predict("testcode", d, model=model, units=max(cycles, 1))
            truth = 0.5 + (max(cycles, 1) + 20) * math.exp(-9 + math.log(1600))
            worst = max(worst, abs(math.log(p["median"] / truth)))
        errors[name] = worst
        if name == "setup":
            assert model["setup"]["mozyme"] == 20.0
            assert "setup 20 iterations" in tm.report_text(model)
    assert errors["setup"] < 0.1 < errors["plain"]


def test_code_time_stands_in_for_a_missing_wall_time(tmp_path):
    """Tasks run in queue bundles before 2026.10.8.1 have no wall time; their
    code's own time is used, and not for the start-up estimate."""
    rows = []
    for n in (100, 200, 400, 800) * 3:
        code = math.exp(-9 + 2.0 * math.log(n))
        rows.append({"n": n, "wall": 1.0 + code, "code_seconds": code})
        rows.append({"n": n, "wall": 0.0, "code_seconds": code})
    _rows(tmp_path, "testcode", rows)
    spec = tm.Spec(size=("n",), klass=(), units=None)
    loaded = tm.load_rows("testcode", tmp_path, spec)
    assert len(loaded) == len(rows)
    assert sum(r["_wall_from_code"] for r in loaded) == len(rows) // 2
    model = tm.fit("testcode", directory=tmp_path, spec=spec)
    assert abs(model["machines"]["m:q:CPU"]["t0"] - 1.0) < 0.05
