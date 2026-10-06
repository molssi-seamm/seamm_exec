# -*- coding: utf-8 -*-

"""Fit a cost model to the timing records and predict the time of a calculation.

The model (campaign ``docs/developer_guide/campaigns/2026-10-05``) is a product
of separable factors, fitted as a sum in log space::

    log((t - t0) / units) = a_class + sum_i b_i log(size_i) - alpha log(cores)
                            + s_machine + task_offset + noise

- ``t0``: a start-up constant per machine class, taken from the smallest runs
  and subtracted outside the log, so sub-second runs do not bend the power law.
- ``units``: the iterations the run took (SCF runs, ionic steps, MD steps ...),
  so the fit learns a unit cost; the distribution of ``units`` per task is kept
  and entered at the requested quantile when predicting.
- ``size_i``: the program's size variables (basis functions, electrons, atoms;
  for plane waves the valence electrons and the grid volume).
- ``alpha``: the parallel exponent, fitted when the records span core counts.
- ``s_machine``: a per-machine-class offset, shrunk toward zero when a class
  has few rows (the calibration a new machine gets from a few standard runs).

The fit is ridge regression in numpy; the model is a JSON file,
``~/.seamm.d/timing/models/<program>.json``, and :func:`predict` evaluates it
with numpy alone. The prediction is a *quantile* of the residual distribution
(the 95th for a queue walltime), never the mean.

Command line::

    python -m seamm_exec.timing_model fit [program ...] [--directory DIR]
    python -m seamm_exec.timing_model predict orca task=energy nbf=240 ... [-q 0.95]
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path

import numpy as np

from .timing import DEFAULT_DIRECTORY, machine_class, read_timings

logger = logging.getLogger("seamm-exec")

#: Where the fitted models go: ``<timing directory>/models/<program>.json``
MODELS_SUBDIR = "models"
#: The model file's format
MODEL_VERSION = 1


@dataclass
class Spec:
    """What the cost model of a program is made of.

    Attributes
    ----------
    size : tuple of str
        Descriptor columns entering as ``log(size)``; a derived one may be
        computed by :attr:`derived`.
    klass : tuple of str
        Columns whose joined values are the method class (an intercept each).
    task : str or None
        The column naming the kind of task (an offset each, and the unit
        distribution).
    units : str or None
        The column holding the iterations the run took (SCF runs, ionic steps,
        MD steps). Missing or zero counts as one.
    multiplier : str or None
        A column the time is proportional to besides ``units`` (k-points).
    derived : dict
        ``{name: function(row) -> float or None}`` for computed size variables.
    default_alpha : float
        The parallel exponent used when the records do not span core counts.
    """

    size: tuple = ("n_atoms",)
    klass: tuple = ()
    task: str | None = "task"
    units: str | None = None
    multiplier: str | None = None
    derived: dict = field(default_factory=dict)
    default_alpha: float = 0.8


def _vasp_grid(row):
    volume = _num(row.get("volume"))
    encut = _num(row.get("encut"))
    if volume is None or encut is None or volume <= 0 or encut <= 0:
        return None
    return volume * (encut / 500.0) ** 1.5


SPECS = {
    "orca": Spec(
        size=("nbf", "n_electrons", "n_atoms"),
        klass=("method_class",),
        units="scf_runs",
    ),
    "gaussian": Spec(
        size=("nbf", "n_electrons", "n_atoms"), klass=("method",), units="scf_runs"
    ),
    "psi4": Spec(
        size=("nbf", "n_electrons", "n_atoms"), klass=("method",), units="scf_runs"
    ),
    "mopac": Spec(
        size=("n_basis", "n_atoms"),
        klass=("hamiltonian", "regime"),
        units="scf_runs",
        default_alpha=0.0,
    ),
    "vasp": Spec(
        size=("nelect", "grid"),
        klass=("model",),
        units="ionic_steps",
        multiplier="kpoints",
        derived={"grid": _vasp_grid},
        default_alpha=0.5,
    ),
    "lammps": Spec(size=("n_atoms",), klass=(), task=None, units="md_steps"),
    "dftbplus": Spec(size=("n_atoms",), klass=("model",), units="scc_cycles"),
}
DEFAULT_SPEC = Spec()

#: Residual quantiles stored in a model
QUANTILES = (0.5, 0.68, 0.84, 0.95, 0.99)
#: Shrinkage (in rows) of a machine class's offset toward the pooled fit
MACHINE_SHRINK = 5.0
#: Ridge penalty on the slopes and class/task offsets
RIDGE = 1e-3


def models_directory(directory=None):
    return Path(directory or DEFAULT_DIRECTORY).expanduser() / MODELS_SUBDIR


def model_path(program, directory=None):
    return models_directory(directory) / f"{program}.json"


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------
def _num(value):
    """A float from a record's text, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if value == "" or value.lower() in ("none", "nan"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_rows(program, directory=None, spec=None):
    """The records of ``program`` usable for a fit: schema 1, finished, a wall
    time, as dicts with the model's variables as floats added
    (``_wall``, ``_cores``, ``_units``, ``_mult``, ``_size``, ``_class``,
    ``_task``, ``_machine``)."""
    spec = spec or SPECS.get(program, DEFAULT_SPEC)
    rows = []
    for row in read_timings(program, directory=directory, all_files=True):
        if str(row.get("schema", "")).strip() != "1":
            continue
        if row.get("state", "finished") not in ("finished", ""):
            continue
        wall = _num(row.get("wall"))
        if wall is None or wall <= 0:
            continue
        ntasks = _num(row.get("ntasks")) or 1.0
        cpus = _num(row.get("cpus_per_task")) or 1.0
        sizes = {}
        for name in spec.size:
            value = (
                spec.derived[name](row) if name in spec.derived else _num(row.get(name))
            )
            if value is not None and value > 0:
                sizes[name] = value
        units = _num(row.get(spec.units)) if spec.units else None
        mult = _num(row.get(spec.multiplier)) if spec.multiplier else None
        row = dict(row)
        row["_wall"] = wall
        row["_cores"] = max(1.0, ntasks * cpus)
        row["_units"] = units if units and units > 0 else 1.0
        row["_mult"] = mult if mult and mult > 0 else 1.0
        row["_size"] = sizes
        row["_class"] = " / ".join(str(row.get(k, "") or "") for k in spec.klass).strip(
            " /"
        )
        row["_task"] = str(row.get(spec.task, "") or "") if spec.task else ""
        row["_machine"] = str(row.get("machine", "") or "")
        rows.append(row)
    return rows


def _quantile(values, q):
    values = np.asarray(values, dtype=float)
    return float(np.quantile(values, q)) if values.size else None


# ----------------------------------------------------------------------
# Fit
# ----------------------------------------------------------------------
def fit(program, directory=None, spec=None, min_rows=8):
    """Fit the cost model of ``program`` to its records.

    Returns
    -------
    dict
        The model (what :func:`save_model` writes), with a ``report`` entry; or
        ``None`` if there are fewer than ``min_rows`` usable records.
    """
    spec = spec or SPECS.get(program, DEFAULT_SPEC)
    rows = load_rows(program, directory, spec)
    if len(rows) < min_rows:
        logger.info(f"{program}: {len(rows)} usable rows, fewer than {min_rows}")
        return None

    # Size features: those present in most rows; rows lacking one are dropped.
    counts = {name: sum(1 for r in rows if name in r["_size"]) for name in spec.size}
    features = [n for n in spec.size if counts[n] >= 0.8 * len(rows)]
    if not features:
        features = [max(counts, key=counts.get)] if counts else []
    rows = [r for r in rows if all(n in r["_size"] for n in features)]
    if len(rows) < min_rows:
        return None

    # Size variables that are (nearly) the same information -- electrons and
    # basis functions of one basis set, atoms and basis functions -- cannot be
    # told apart by the fit and would split a slope arbitrarily; keep the first
    # of any pair whose log values correlate above 0.98.
    logs = {n: np.log([r["_size"][n] for r in rows]) for n in features}
    kept = []
    for n in features:
        if all(
            abs(np.corrcoef(logs[n], logs[k])[0, 1]) < 0.98 or np.std(logs[n]) == 0
            for k in kept
        ):
            if np.std(logs[n]) > 0 or not kept:
                kept.append(n)
    features = kept
    feature_log_means = {n: float(logs[n].mean()) for n in features}

    # Start-up constant per machine. Where the code reports its own time
    # (``code_seconds``) the constant is measured: the median of wall minus the
    # code's time over the machine's runs (process start, the executor's
    # bookkeeping, file copies). Otherwise it is a fraction of the 5th
    # percentile of the smallest fifth of the runs, the fraction chosen by the
    # fit below as the one that predicts the wall times best.
    machines = sorted({r["_machine"] for r in rows})
    measured = {}
    for machine in machines:
        mine = [r for r in rows if r["_machine"] == machine]
        gaps = [
            r["_wall"] - c
            for r in mine
            for c in [_num(r.get("code_seconds"))]
            if c is not None and 0 <= c <= r["_wall"]
        ]
        if len(gaps) >= max(3, len(mine) // 2):
            measured[machine] = max(0.0, float(np.median(gaps)))
    p05 = {}
    for machine in machines:
        mine = [r for r in rows if r["_machine"] == machine]
        if features:
            mine.sort(key=lambda r: r["_size"][features[0]])
        small = mine[: max(3, len(mine) // 5)]
        p05[machine] = _quantile([r["_wall"] for r in small], 0.05)

    best = None
    fractions = (None,) if len(measured) == len(machines) else (0.0, 0.3, 0.6, 0.9)
    for fraction in fractions:
        if fraction is None:
            t0 = dict(measured)
        else:
            t0 = {m: measured.get(m, fraction * p05[m]) for m in machines}
        result = _fit_rows(rows, features, t0, spec, min_rows)
        if result is not None and (best is None or result["score"] < best["score"]):
            best = result
            best["t0"] = t0
            best["t0_fraction"] = "measured" if fraction is None else fraction
    if best is None:
        return None
    best["feature_log_means"] = feature_log_means
    return _assemble(program, spec, features, best)


def _fit_rows(all_rows, features, t0, spec, min_rows):
    """One ridge fit for a given start-up constant; the pieces for the model."""
    rows = []
    for r in all_rows:
        net = r["_wall"] - t0[r["_machine"]]
        # A run whose time is nearly all start-up says nothing about the
        # power law (its remainder is noise); it is predicted by t0 alone.
        if net > max(0.02, 0.1 * t0[r["_machine"]]):
            r = dict(r)
            r["_y"] = math.log(net / (r["_units"] * r["_mult"]))
            rows.append(r)
    if len(rows) < min_rows:
        return None

    classes = sorted({r["_class"] for r in rows})
    tasks = sorted({r["_task"] for r in rows})
    machines = sorted({r["_machine"] for r in rows})
    ref_class = max(classes, key=lambda c: sum(1 for r in rows if r["_class"] == c))
    ref_task = max(tasks, key=lambda t: sum(1 for r in rows if r["_task"] == t))
    cores = np.array([r["_cores"] for r in rows])
    fit_alpha = len({round(c) for c in cores}) > 1

    # Design matrix: intercept, log sizes, [log cores], class dummies, task dummies
    columns = ["intercept"] + [f"log {n}" for n in features]
    X = [np.ones(len(rows))]
    for n in features:
        X.append(np.log([r["_size"][n] for r in rows]))
    if fit_alpha:
        columns.append("log cores")
        X.append(np.log(cores))
        if features:
            # Parallel efficiency grows with the size of the calculation: a
            # small molecule gains nothing from more cores, a large one nearly
            # everything. alpha = a0 + a1 (log size - mean log size).
            log_size = np.log([r["_size"][features[0]] for r in rows])
            columns.append("log cores x log size")
            X.append(np.log(cores) * (log_size - log_size.mean()))
    for c in classes:
        if c != ref_class:
            columns.append(f"class {c}")
            X.append(np.array([1.0 if r["_class"] == c else 0.0 for r in rows]))
    for t in tasks:
        if t != ref_task:
            columns.append(f"task {t}")
            X.append(np.array([1.0 if r["_task"] == t else 0.0 for r in rows]))
    X = np.column_stack(X)
    y = np.array([r["_y"] for r in rows])
    machine_index = np.array([machines.index(r["_machine"]) for r in rows])

    # Alternate: ridge on the shared part, shrunk means for the machines.
    offsets = np.zeros(len(machines))
    penalty = np.full(X.shape[1], RIDGE)
    penalty[0] = 0.0
    for _ in range(4):
        target = y - offsets[machine_index]
        beta = np.linalg.solve(X.T @ X + np.diag(penalty), X.T @ target)
        resid = y - X @ beta
        for m in range(len(machines)):
            mask = machine_index == m
            n = mask.sum()
            offsets[m] = resid[mask].mean() * n / (n + MACHINE_SHRINK) if n else 0.0
        # Keep the offsets centred: their mean belongs to the intercept
        shift = offsets.mean()
        offsets -= shift
        beta[0] += shift
    resid = y - X @ beta - offsets[machine_index]

    alpha = (
        -float(beta[columns.index("log cores")]) if fit_alpha else spec.default_alpha
    )
    alpha = min(1.0, max(0.0, alpha))
    alpha_slope = 0.0
    alpha_size_mean = None
    if fit_alpha and "log cores x log size" in columns:
        alpha_slope = -float(beta[columns.index("log cores x log size")])
        alpha_size_mean = float(np.log([r["_size"][features[0]] for r in rows]).mean())

    predicted = np.exp(X @ beta + offsets[machine_index]) * np.array(
        [r["_units"] * r["_mult"] for r in rows]
    ) + np.array([t0[r["_machine"]] for r in rows])
    wall = np.array([r["_wall"] for r in rows])
    # The score a start-up constant is chosen by: the log error of the wall
    # time itself, which both the constant and the power law must explain
    score = float((np.log(predicted / wall) ** 2).sum())
    return {
        "score": score,
        "rows": rows,
        "columns": columns,
        "beta": beta,
        "resid": resid,
        "y": y,
        "offsets": offsets,
        "machines": machines,
        "machine_index": machine_index,
        "classes": classes,
        "tasks": tasks,
        "ref_class": ref_class,
        "ref_task": ref_task,
        "alpha": alpha,
        "alpha_slope": alpha_slope,
        "alpha_size_mean": alpha_size_mean,
        "fit_alpha": fit_alpha,
        "predicted": predicted,
        "ss_res": float((resid**2).sum()),
    }


def _assemble(program, spec, features, f):
    """The model dictionary from a fit's pieces."""
    rows, columns, beta, resid = f["rows"], f["columns"], f["beta"], f["resid"]
    t0 = f["t0"]
    coefficients = {
        name: float(b)
        for name, b in zip(columns, beta)
        if name not in ("log cores", "log cores x log size")
    }
    class_offsets = {c: coefficients.pop(f"class {c}", 0.0) for c in f["classes"]}
    task_offsets = {t: coefficients.pop(f"task {t}", 0.0) for t in f["tasks"]}

    def quantiles(values):
        return {str(q): _quantile(values, q) for q in QUANTILES}

    per_task_resid = {}
    per_task_units = {}
    for t in f["tasks"]:
        mask = np.array([r["_task"] == t for r in rows])
        if mask.sum() >= 5:
            per_task_resid[t] = quantiles(resid[mask])
        per_task_units[t] = quantiles([r["_units"] for r in rows if r["_task"] == t])

    ratio = f["predicted"] / np.array([r["_wall"] for r in rows])
    within = lambda x: float(np.mean((ratio < x) & (ratio > 1 / x)))  # noqa: E731
    y = f["y"]
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
    offsets, machines, machine_index = f["offsets"], f["machines"], f["machine_index"]

    return {
        "model_version": MODEL_VERSION,
        "program": program,
        "fitted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": len(rows),
        "features": features,
        "feature_log_means": f["feature_log_means"],
        "coefficients": coefficients,  # intercept and log-size slopes
        "alpha": f["alpha"],
        "alpha_slope": f["alpha_slope"],
        "alpha_size_mean": f["alpha_size_mean"],
        "alpha_fitted": f["fit_alpha"],
        "classes": class_offsets,
        "reference_class": f["ref_class"],
        "tasks": task_offsets,
        "reference_task": f["ref_task"],
        "units_column": spec.units,
        "multiplier_column": spec.multiplier,
        "size_columns": list(spec.size),
        "class_columns": list(spec.klass),
        "task_column": spec.task,
        "machines": {
            m: {
                "offset": float(offsets[i]),
                "rows": int((machine_index == i).sum()),
                "t0": float(t0[m]),
            }
            for i, m in enumerate(machines)
        },
        "machine_offset_sd": float(np.std(offsets)) if len(machines) > 1 else 0.3,
        "t0_pooled": float(np.median(list(t0.values()))),
        "t0_fraction": f["t0_fraction"],
        "residual_quantiles": quantiles(resid),
        "residual_sd": float(resid.std()),
        "task_residual_quantiles": per_task_resid,
        "task_units_quantiles": per_task_units,
        "report": {
            "r2_log": 1.0 - f["ss_res"] / ss_tot,
            "within_1.3x": within(1.3),
            "within_2x": within(2.0),
            "machines": len(machines),
            "classes": len(f["classes"]),
            "tasks": len(f["tasks"]),
        },
    }


def save_model(model, directory=None):
    path = model_path(model["program"], directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=2))
    return path


_cache = {}


def load_model(program, directory=None):
    """The fitted model of ``program``, or None if there is none."""
    path = model_path(program, directory)
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _cache.get(key)
    if cached is None or cached[0] != mtime:
        try:
            cached = (mtime, json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read the timing model {path}: {e}")
            return None
        _cache[key] = cached
    return cached[1]


# ----------------------------------------------------------------------
# Predict
# ----------------------------------------------------------------------
def _interp_quantile(quantiles, q):
    """A residual quantile by interpolation in the stored table."""
    points = sorted((float(k), v) for k, v in quantiles.items() if v is not None)
    if not points:
        return 0.0
    if q <= points[0][0]:
        return points[0][1]
    if q >= points[-1][0]:
        return points[-1][1]
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if q0 <= q <= q1:
            return v0 + (v1 - v0) * (q - q0) / (q1 - q0)
    return points[-1][1]


def predict(
    program,
    descriptors,
    ntasks=1,
    cpus_per_task=1,
    machine=None,
    quantile=0.95,
    units=None,
    directory=None,
    model=None,
):
    """The predicted wall time of a calculation.

    Parameters
    ----------
    program : str
    descriptors : dict
        The calculation's descriptors, as its timing record would carry them
        (the size columns, the class columns, the task; the multiplier).
    ntasks, cpus_per_task : int
    machine : str, optional
        The machine class key; default this machine's. An unknown class gets no
        offset and a wider spread.
    quantile : float
        Of the residual distribution: 0.5 for a median estimate (packing),
        0.95 for a queue walltime.
    units : float, optional
        The iterations the run will take, if known (an optimization's steps);
        else the task's distribution at the same quantile.
    directory, model : optional
        The timing directory, or a model already loaded.

    Returns
    -------
    dict or None
        ``seconds`` (at the quantile), ``median``, ``units``, ``machine_known``,
        ``quantile``, ``spread`` (the log-space spread used), ``model`` (its date
        and rows); None when there is no model or the descriptors lack every
        size variable.
    """
    model = model or load_model(program, directory)
    if model is None:
        return None
    spec = SPECS.get(program, DEFAULT_SPEC)
    sizes = {}
    for name in model["features"]:
        value = (
            spec.derived[name](descriptors)
            if name in spec.derived
            else _num(descriptors.get(name))
        )
        if value is not None and value > 0:
            sizes[name] = value
    if not sizes:
        return None
    coef = model["coefficients"]
    y = coef["intercept"]
    means = model.get("feature_log_means", {})
    for name in model["features"]:
        # A size variable not given takes the records' mean (log) value
        value = math.log(sizes[name]) if name in sizes else means.get(name, 0.0)
        y += coef.get(f"log {name}", 0.0) * value
    cores = max(1.0, float(ntasks or 1) * float(cpus_per_task or 1))
    alpha = model["alpha"]
    first = model["features"][0] if model["features"] else None
    if model.get("alpha_size_mean") is not None and first in sizes:
        alpha += model.get("alpha_slope", 0.0) * (
            math.log(sizes[first]) - model["alpha_size_mean"]
        )
    alpha = min(1.0, max(0.0, alpha))
    y -= alpha * math.log(cores)
    klass = " / ".join(
        str(descriptors.get(k, "") or "") for k in model["class_columns"]
    ).strip(" /")
    y += model["classes"].get(klass, 0.0)
    task = (
        str(descriptors.get(model["task_column"], "") or "")
        if model["task_column"]
        else ""
    )
    y += model["tasks"].get(task, 0.0)

    machine = machine or machine_class()["machine"]
    info = model["machines"].get(machine)
    known = info is not None
    if known:
        y += info["offset"]
        t0 = info["t0"]
    else:
        t0 = model["t0_pooled"]

    if units is None:
        table = model["task_units_quantiles"].get(task) or model[
            "task_units_quantiles"
        ].get(model["reference_task"], {})
        units = _interp_quantile(table, quantile) if table else 1.0
        units = max(1.0, units or 1.0)
    mult = 1.0
    if model["multiplier_column"]:
        mult = _num(descriptors.get(model["multiplier_column"])) or 1.0

    resid_table = (
        model["task_residual_quantiles"].get(task) or model["residual_quantiles"]
    )
    spread = _interp_quantile(resid_table, quantile)
    if not known:
        # An unknown machine: widen by the spread of the machine offsets
        z = {0.5: 0.0, 0.68: 0.47, 0.84: 1.0, 0.95: 1.64, 0.99: 2.33}
        spread += model["machine_offset_sd"] * _interp_quantile(
            {str(k): v for k, v in z.items()}, quantile
        )
    median = math.exp(y) * units * mult + t0
    seconds = math.exp(y + spread) * units * mult + t0
    return {
        "seconds": seconds,
        "median": median,
        "units": units,
        "machine_known": known,
        "machine": machine,
        "quantile": quantile,
        "spread": spread,
        "model": {"fitted": model["fitted"], "rows": model["rows"]},
    }


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------
def report_text(model):
    r = model["report"]
    lines = [
        f"{model['program']}: {model['rows']} rows, {r['machines']} machine class(es), "
        f"{r['classes']} method class(es), {r['tasks']} task(s)",
        f"  R^2 (log unit time) {r['r2_log']:.3f}; within 1.3x {r['within_1.3x']:.0%}, "
        f"within 2x {r['within_2x']:.0%}",
        "  log(unit time) = "
        + " + ".join(
            [f"{model['coefficients']['intercept']:.3f}"]
            + [
                f"{model['coefficients'][f'log {n}']:.3f} log({n})"
                for n in model["features"]
            ]
        )
        + f" - alpha log(cores), alpha = {model['alpha']:.2f}"
        + (
            f" {model['alpha_slope']:+.2f} (log {model['features'][0]} - "
            f"{model['alpha_size_mean']:.2f})"
            if model.get("alpha_size_mean") is not None and model.get("alpha_slope")
            else ""
        )
        + ("" if model["alpha_fitted"] else " [assumed]"),
    ]
    for name, offset in sorted(model["classes"].items(), key=lambda kv: kv[1]):
        if name != model["reference_class"]:
            lines.append(f"  class {name or '(none)'}: x{math.exp(offset):.2f}")
    for name, offset in sorted(model["tasks"].items(), key=lambda kv: kv[1]):
        if name != model["reference_task"]:
            units = model["task_units_quantiles"].get(name, {}).get("0.5")
            lines.append(
                f"  task {name or '(none)'}: x{math.exp(offset):.2f} per unit"
                + (f", median {units:.0f} units" if units else "")
            )
    for name, info in sorted(model["machines"].items(), key=lambda kv: kv[1]["offset"]):
        lines.append(
            f"  machine {name or '(unknown)'}: x{math.exp(info['offset']):.2f} "
            f"({info['rows']} rows, t0 {info['t0']:.2f} s)"
        )
    q = model["residual_quantiles"]
    lines.append(
        f"  spread: 68% within x{math.exp(q['0.84']):.2f}, 95% within "
        f"x{math.exp(q['0.95']):.2f} of the median"
    )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m seamm_exec.timing_model",
        description="Fit the cost models to the timing records, or predict a time.",
    )
    parser.add_argument(
        "--directory", help="the timing directory (default ~/.seamm.d/timing)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_fit = sub.add_parser(
        "fit", help="fit the model of each program (default: all with records)"
    )
    p_fit.add_argument("programs", nargs="*")
    p_fit.add_argument("--min-rows", type=int, default=8)
    p_pred = sub.add_parser("predict", help="predict a calculation's time")
    p_pred.add_argument("program")
    p_pred.add_argument("pairs", nargs="*", help="descriptors as key=value")
    p_pred.add_argument("-q", "--quantile", type=float, default=0.95)
    p_pred.add_argument("--ntasks", type=int, default=1)
    p_pred.add_argument("--cpus-per-task", type=int, default=1)
    p_pred.add_argument("--machine")
    p_pred.add_argument("--units", type=float)
    args = parser.parse_args(argv)

    if args.command == "fit":
        directory = Path(args.directory or DEFAULT_DIRECTORY).expanduser()
        programs = args.programs or sorted(
            {p.stem.split("-")[0] for p in directory.glob("*.csv")}
        )
        for program in programs:
            model = fit(program, args.directory, min_rows=args.min_rows)
            if model is None:
                print(f"{program}: not enough usable records to fit")
                continue
            path = save_model(model, args.directory)
            print(report_text(model))
            print(f"  written to {path}\n")
        return 0

    descriptors = dict(pair.split("=", 1) for pair in args.pairs)
    result = predict(
        args.program,
        descriptors,
        ntasks=args.ntasks,
        cpus_per_task=args.cpus_per_task,
        machine=args.machine,
        quantile=args.quantile,
        units=args.units,
        directory=args.directory,
    )
    if result is None:
        print(f"No model for {args.program}, or no size variable given.")
        return 1
    print(
        f"{result['seconds']:.1f} s at the {result['quantile']:.0%} quantile "
        f"(median {result['median']:.1f} s, {result['units']:.0f} unit(s); machine "
        f"{result['machine']} "
        f"{'known' if result['machine_known'] else 'unknown, widened'}; "
        f"model of {result['model']['fitted'][:10]} on {result['model']['rows']} rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
