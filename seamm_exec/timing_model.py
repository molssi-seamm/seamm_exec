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
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import time

import numpy as np

from .timing import DEFAULT_DIRECTORY, machine_class, read_timings

logger = logging.getLogger("seamm-exec")

#: Where the fitted models go: ``<timing directory>/models/<program>.json``
MODELS_SUBDIR = "models"
#: The model file's format
MODEL_VERSION = 2


@dataclass
class Spec:
    """What the cost model of a program is made of: plain data a code step
    declares and passes when it records a run (``record_timing(...,
    spec=...)``), written beside the records as ``<program>.spec.json`` so the
    fit reads it without importing the plug-in.

    Attributes
    ----------
    size : tuple of str
        Descriptor columns entering as ``log(size)``, most informative first
        (basis functions, electrons, atoms; valence electrons and the grid
        volume for plane waves -- a computed variable is written as a
        descriptor by the step).
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
    default_alpha : float
        The parallel exponent used when the records do not span core counts.
    slope_by : str or None
        A column whose values each get their own size exponents -- for a code
        with regimes that scale differently, such as MOPAC's MOZYME (roughly
        linear) and traditional SCF (roughly cubic). The class columns give
        each value its own intercept; this gives it its own slope as well.
    setup_by : str or None
        A column whose values each get a fixed cost per run, in units of one
        iteration: the work is ``units + setup``. For a code whose first step
        is much dearer than the rest -- MOPAC's MOZYME localizes the orbitals
        once, then runs fast cycles -- so a single point and a long
        optimization share one per-cycle cost.
    flags : dict or None
        The run's options as flags, each with its own factor learned by the fit
        (shrunk to none unless the records support it): ``{"column": the
        column holding them, space separated (ORCA's '!' line); "drop": regular
        expressions of words that are not options (task keywords, basis sets);
        "drop_columns": columns whose values are dropped from the words (the
        method and basis, which the model has already)}``. See
        :func:`flag_tokens`.
    """

    size: tuple = ("n_atoms",)
    klass: tuple = ()
    task: str | None = "task"
    units: str | None = None
    multiplier: str | None = None
    default_alpha: float = 0.8
    slope_by: str | None = None
    setup_by: str | None = None
    flags: dict | None = None

    def to_dict(self):
        return {
            "size": list(self.size),
            "klass": list(self.klass),
            "task": self.task,
            "units": self.units,
            "multiplier": self.multiplier,
            "default_alpha": self.default_alpha,
            "slope_by": self.slope_by,
            "setup_by": self.setup_by,
            "flags": self.flags,
        }

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        return cls(
            size=tuple(data.get("size") or ("n_atoms",)),
            klass=tuple(data.get("klass") or ()),
            task=data.get("task", "task"),
            units=data.get("units"),
            multiplier=data.get("multiplier"),
            default_alpha=float(data.get("default_alpha", 0.8)),
            slope_by=data.get("slope_by"),
            setup_by=data.get("setup_by"),
            flags=data.get("flags"),
        )


def flag_tokens(record, flags):
    """The option flags of a run, as upper-case words, per a spec's ``flags``.

    The words of ``flags["column"]``, less those matching a ``drop`` pattern
    and those that are another column's value (``drop_columns``, compared
    ignoring case, with "_" and "/" the same, and also as the parts of a
    value like "R2SCAN-D4"). An empty set without a spec or a value.
    """
    if not flags:
        return frozenset()
    text = str(record.get(flags.get("column", ""), "") or "")
    if not text:
        return frozenset()
    drops = [re.compile(p, re.IGNORECASE) for p in flags.get("drop", ())]
    same = set()
    for column in flags.get("drop_columns", ()):
        value = str(record.get(column, "") or "").upper().strip()
        if not value:
            continue
        for v in (value, *value.split(), *value.rsplit("-", 1)):
            same.update({v, v.replace("_", "/"), v.replace("/", "_")})
    words = set()
    for word in text.upper().split():
        if word in same or any(d.search(word) for d in drops):
            continue
        words.add(word)
    return frozenset(words)


DEFAULT_SPEC = Spec()

#: Specs for the code steps of 2026.10.6, which wrote records before the steps
#: declared their own: used only when a program has no ``<program>.spec.json``.
#: Remove once every step writes its spec.
FALLBACK_SPECS = {
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
        units="geometry_cycles",
        default_alpha=0.0,
        slope_by="regime",
        setup_by="regime",
    ),
    "vasp": Spec(
        size=("nelect", "grid", "volume"),
        klass=("model",),
        units="ionic_steps",
        multiplier="kpoints",
        default_alpha=0.5,
    ),
    "lammps": Spec(size=("n_atoms",), klass=(), task=None, units="md_steps"),
    "dftbplus": Spec(size=("n_atoms",), klass=("model",), units="scc_cycles"),
}


def spec_path(program, directory=None):
    return Path(directory or DEFAULT_DIRECTORY).expanduser() / f"{program}.spec.json"


def write_spec(program, spec, directory=None):
    """Write a program's spec beside its records, if it is not there or has
    changed. Called by ``record_timing`` when the step passes its spec; never
    raises."""
    try:
        if not isinstance(spec, Spec):
            spec = Spec.from_dict(spec)
        path = spec_path(program, directory)
        text = json.dumps(spec.to_dict(), indent=2) + "\n"
        if path.exists() and path.read_text() == text:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.debug(f"Could not write the timing spec of {program}: {e}")
        return None


def load_spec(program, directory=None):
    """The program's spec: the one its step wrote beside the records, else the
    fallback for the 2026.10.6 steps, else the default (atoms only)."""
    path = spec_path(program, directory)
    if path.exists():
        try:
            return Spec.from_dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Could not read the timing spec {path}: {e}")
    return FALLBACK_SPECS.get(program, DEFAULT_SPEC)


#: Residual quantiles stored in a model
QUANTILES = (0.5, 0.68, 0.84, 0.95, 0.99)
#: Shrinkage (in rows) of a machine class's offset toward the pooled fit
MACHINE_SHRINK = 5.0
#: Ridge penalty on the slopes and class/task offsets
#: |corr(log cores, log size)| above which the parallel exponent is not fitted
CORES_SIZE_CORRELATION = 0.7
#: How far outside the fitted size range (as a factor) a prediction is still made
EXTRAPOLATION = 1.5
RIDGE = 1e-3
#: The parallel start-up tried per machine, as fractions of the 5th percentile
#: of the net times of its smallest parallel runs. A parallel run pays a fixed
#: cost serial ones do not -- launching the MPI processes, about 9 s on Owl and
#: 3-4 s on TinkerCliffs for ORCA, nearly the same for 4 or 16 of them -- which
#: a power law in the cores cannot describe (tiny molecules ran slower on more
#: cores). A large fraction on a machine whose smallest parallel runs are long
#: hurts their fit and is not chosen.
PARALLEL_FRACTIONS = (0.0, 0.5, 0.75, 0.9)
#: Paired runs -- the same calculation on several core counts -- needed to
#: take the parallel exponent from them rather than from the main fit
PAIRED_MIN_GROUPS = 4
#: A flag (``Spec.flags``) gets a factor only with this many runs with it,
#: and as many without it ...
FLAG_MIN_ROWS = 10
#: ... and only when this many calculations -- the same machine, method class,
#: task and sizes -- ran both with and without it. A flag that always comes
#: with its own calculations (one campaign's settings, a molecule's elements)
#: cannot be told from them: on ARC's ORCA records every flag was like that,
#: and the factors fitted anyway took machine and campaign differences
#: (Owl's production predictions fell from 96 % to 92 % within 2x). The timing
#: benchmarks' toggle ladders make the contrasts.
FLAG_MIN_CONTRASTS = 3
#: The ridge penalty on a flag's factor, per row: a flag the records do not
#: clearly need stays near no effect
FLAG_RIDGE = 0.01
#: The added log-spread of a prediction (above the median) per flag the
#: records have never seen, and its most
FLAG_UNKNOWN_SPREAD = 0.15
FLAG_UNKNOWN_MAX = 0.5
#: The setups tried for each group of ``Spec.setup_by``, in iterations
SETUP_CANDIDATES = (0.0, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)
#: Each run's weight in the power-law fit is its net time (wall less start-up)
#: to this power, normalized. The cost exponent grows with size, so a single
#: power law fitted equally to many small runs bends low at the large end --
#: the runs that cost queue time and whose estimates set walltimes. Weighting by
#: time puts the fit where the time is; small runs still set the start-up time.
WEIGHT_POWER = 1.0
#: A model is refitted by :func:`predict` when its records have grown by this
#: fraction since the fit ...
REFIT_GROWTH = 0.2
#: ... or when it is older than this many days and the records have changed at all
REFIT_DAYS = 7.0


def models_directory(directory=None):
    return Path(directory or DEFAULT_DIRECTORY).expanduser() / MODELS_SUBDIR


def model_path(program, directory=None):
    return models_directory(directory) / f"{program}.json"


def records_state(program, directory=None):
    """``(bytes, mtime)`` over the program's record files, current and set
    aside: a cheap measure of how much the records have grown (no read)."""
    path = Path(directory or DEFAULT_DIRECTORY).expanduser()
    total = 0
    latest = 0.0
    for p in path.glob(f"{program}*.csv"):
        stem = p.stem
        if stem != program and not stem[len(program) + 1 :].replace("-", "").isdigit():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        latest = max(latest, st.st_mtime)
    return total, latest


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
    spec = spec or load_spec(program, directory)
    rows = []
    for row in read_timings(program, directory=directory, all_files=True):
        if str(row.get("schema", "")).strip() != "1":
            continue
        if row.get("state", "finished") not in ("finished", ""):
            continue
        wall = _num(row.get("wall"))
        from_code = False
        if wall is None or wall <= 0:
            # A task run in a queue bundle before seamm-exec 2026.10.8.1 has no
            # wall time (its start was not carried back from the node); its
            # code's own time stands in, a few percent short of the wall time,
            # and it says nothing about the start-up.
            wall = _num(row.get("code_seconds"))
            if wall is None or wall <= 0:
                continue
            from_code = True
        ntasks = _num(row.get("ntasks")) or 1.0
        cpus = _num(row.get("cpus_per_task")) or 1.0
        sizes = {}
        for name in spec.size:
            value = _num(row.get(name))
            if value is not None and value > 0:
                sizes[name] = value
        units = _num(row.get(spec.units)) if spec.units else None
        mult = _num(row.get(spec.multiplier)) if spec.multiplier else None
        row = dict(row)
        row["_wall"] = wall
        row["_wall_from_code"] = from_code
        row["_cores"] = max(1.0, ntasks * cpus)
        row["_units"] = units if units and units > 0 else 1.0
        row["_mult"] = mult if mult and mult > 0 else 1.0
        row["_size"] = sizes
        row["_class"] = " / ".join(str(row.get(k, "") or "") for k in spec.klass).strip(
            " /"
        )
        row["_task"] = str(row.get(spec.task, "") or "") if spec.task else ""
        row["_machine"] = str(row.get("machine", "") or "")
        row["_flags"] = flag_tokens(row, spec.flags)
        rows.append(row)
    return rows


def _quantile(values, q):
    values = np.asarray(values, dtype=float)
    return float(np.quantile(values, q)) if values.size else None


# ----------------------------------------------------------------------
# Fit
# ----------------------------------------------------------------------
def fit(program, directory=None, spec=None, min_rows=8, weight_power=None):
    """Fit the cost model of ``program`` to its records.

    Returns
    -------
    dict
        The model (what :func:`save_model` writes), with a ``report`` entry; or
        ``None`` if there are fewer than ``min_rows`` usable records.
    """
    spec = spec or load_spec(program, directory)
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

    # The flags with enough support to get a factor of their own
    flag_features = []
    vocabulary = sorted({t for r in rows for t in r["_flags"]})
    if spec.flags:
        counts = {}
        by_calculation = {}
        for r in rows:
            calc = (
                r["_machine"],
                r["_class"],
                r["_task"],
                tuple(round(r["_size"][n], 6) for n in features),
            )
            by_calculation.setdefault(calc, []).append(r["_flags"])
            for t in r["_flags"]:
                counts[t] = counts.get(t, 0) + 1
        contrasts = {
            t: sum(
                1
                for flags in by_calculation.values()
                if any(t in f for f in flags) and any(t not in f for f in flags)
            )
            for t in counts
        }
        flag_features = sorted(
            t
            for t, c in counts.items()
            if FLAG_MIN_ROWS <= c <= len(rows) - FLAG_MIN_ROWS
            and contrasts[t] >= FLAG_MIN_CONTRASTS
        )

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
            (r["_size"][features[0]] if features else 0.0, r["_wall"] - c)
            for r in mine
            for c in [_num(r.get("code_seconds"))]
            if c is not None and 0 <= c <= r["_wall"] and not r["_wall_from_code"]
        ]
        if len(gaps) >= max(3, len(mine) // 2):
            # The gap grows with the run (more output to copy and parse), so the
            # start-up is measured on the smallest fifth of the runs.
            gaps.sort()
            small = [g for _, g in gaps[: max(3, len(gaps) // 5)]]
            measured[machine] = max(0.0, float(np.median(small)))
    p05 = {}
    for machine in machines:
        mine = [r for r in rows if r["_machine"] == machine]
        if features:
            mine.sort(key=lambda r: r["_size"][features[0]])
        small = mine[: max(3, len(mine) // 5)]
        p05[machine] = _quantile([r["_wall"] for r in small], 0.05)

    fractions = (None,) if len(measured) == len(machines) else (0.0, 0.3, 0.6, 0.9)

    parallel = {m: 0.0 for m in machines}
    alpha_fixed = None
    fixed_t0 = None

    def search(features, zero, no_delta, setup=None, parallel_=None):
        best = None
        for fraction in fractions if fixed_t0 is None else ("fixed",):
            if fraction == "fixed":
                t0 = dict(fixed_t0)
            elif fraction is None:
                t0 = dict(measured)
            else:
                t0 = {m: measured.get(m, fraction * p05[m]) for m in machines}
            result = _fit_rows(
                rows,
                features,
                t0,
                spec,
                min_rows,
                zero,
                no_delta,
                weight_power,
                setup or {},
                parallel if parallel_ is None else parallel_,
                alpha_fixed,
                flag_features,
            )
            if result is not None and (best is None or result["score"] < best["score"]):
                best = result
                best["t0"] = t0
                best["t0_fraction"] = (
                    t0_fraction
                    if fraction == "fixed"
                    else "measured" if fraction is None else fraction
                )
        return best

    # No size exponent may be negative: a calculation does not get faster as
    # it grows. Size variables that are related but not collinear (basis
    # functions and electrons over a mix of basis sets) can split one effect
    # into a large positive and a negative exponent that fit the records well
    # and extrapolate the wrong way (more basis functions at the same electrons
    # predicted faster). So while any exponent is negative: drop the least
    # informative variable (the last in the spec's order) and fit again; a
    # group's own exponent that goes negative is pooled with the rest; a lone
    # variable whose exponent is still negative is held at zero.
    # The start-up of every run, from a fit without a parallel start-up; then
    # the parallel start-up on top of it (coordinate search per machine) and
    # the parallel exponent from paired runs, alternated twice: each changes
    # the other. The parallel start-up per machine is tried as fractions of
    # the 5th percentile of the net times of its smallest fifth of parallel
    # runs.
    provisional = search(features, set(), set())
    if provisional is None:
        return None
    fixed_t0 = provisional["t0"]
    t0_fraction = provisional["t0_fraction"]
    p05_parallel = {}
    for machine in machines:
        mine = [r for r in rows if r["_machine"] == machine and r["_cores"] > 1]
        if len(mine) < 3:
            continue
        if features:
            mine.sort(key=lambda r: r["_size"][features[0]])
        small = mine[: max(3, len(mine) // 5)]
        value = _quantile([r["_wall"] - fixed_t0[machine] for r in small], 0.05)
        if value and value > 0:
            p05_parallel[machine] = float(value)
    for _ in range(2):
        for machine in sorted(p05_parallel):
            scores = []
            for fraction in PARALLEL_FRACTIONS:
                trial_parallel = {
                    **parallel,
                    machine: fraction * p05_parallel[machine],
                }
                trial = search(features, set(), set(), None, trial_parallel)
                if trial is not None:
                    scores.append((trial["score"], fraction, trial_parallel[machine]))
            if scores:
                parallel[machine] = min(scores)[2]
        provisional = search(features, set(), set())
        if provisional is None:
            return None
        paired = _paired_alpha(rows, features, provisional["t0"], parallel)
        if paired is not None:
            alpha_fixed = paired

    zero, no_delta, notes = set(), set(), []
    if alpha_fixed is not None:
        notes.append(
            f"parallel exponent from {alpha_fixed['groups']} paired runs (the same "
            "calculation on several core counts)"
        )
    if any(parallel.values()):
        notes.append(
            "parallel start-up: "
            + ", ".join(
                f"{m.split(':')[0]} {v:.1f} s" for m, v in parallel.items() if v
            )
        )
    for _ in range(4 * (len(features) + 4)):
        best = search(features, zero, no_delta)
        if best is None:
            return None
        negative = _negative_slopes(best, features, zero)
        if not negative:
            break
        active = [n for n in features if n not in zero]
        if len(active) > 1:
            # Leave out the variable whose absence fits the records best
            # (ties to the later, less informative one in the spec's order).
            trials = []
            for order, candidate in enumerate(active):
                kept = [n for n in features if n != candidate]
                trial = search(kept, zero, no_delta)
                if trial is not None:
                    trials.append((trial["score"], -order, candidate))
            if not trials:
                return None
            dropped = min(trials)[2]
            features = [n for n in features if n != dropped]
            notes.append(f"left out {dropped}: with it a size exponent was negative")
            continue
        groups = sorted({g for _, g in negative if g is not None})
        if groups:
            no_delta.update(groups)
            notes.append(
                f"pooled the size exponent of {', '.join(groups)} "
                "(its own was negative)"
            )
            continue
        zero.update(n for n, _ in negative)
        notes.append(
            f"held the exponent of {', '.join(sorted(zero))} at zero "
            "(the fit gave a negative one)"
        )
    # A fixed cost per run for each group of spec.setup_by, in iterations:
    # coordinate search over the candidates, keeping the best score.
    setup = {}
    if spec.setup_by:
        groups = sorted({str(r.get(spec.setup_by, "") or "") for r in rows})
        for _ in range(2):
            for g in groups:
                scores = []
                for value in SETUP_CANDIDATES:
                    trial = search(features, zero, no_delta, {**setup, g: value})
                    if trial is not None:
                        scores.append((trial["score"], value))
                if scores:
                    setup[g] = min(scores)[1]
        best = search(features, zero, no_delta, setup)
    best["setup"] = setup
    best["vocabulary"] = vocabulary
    best["feature_log_means"] = {n: feature_log_means[n] for n in features}
    best["notes"] = notes
    model = _assemble(program, spec, features, best)
    size, mtime = records_state(program, directory)
    model["records_bytes"] = size
    model["records_mtime"] = mtime
    return model


def _negative_slopes(f, features, zero):
    """[(feature, group or None)] whose effective size exponent is negative:
    the shared one (group None, which is the reference group's), or a
    group's own (shared plus its delta)."""
    coefficients = dict(zip(f["columns"], f["beta"]))
    negative = []
    for n in features:
        if n in zero:
            continue
        base = coefficients.get(f"log {n}", 0.0)
        if base < -1e-9:
            negative.append((n, None))
        for g in f["delta_groups"]:
            if base + coefficients.get(f"log {n} @ {g}", 0.0) < -1e-9:
                negative.append((n, g))
    return negative


def _fit_rows(
    all_rows,
    features,
    t0,
    spec,
    min_rows,
    zero=(),
    no_delta=(),
    weight_power=None,
    setup=None,
    parallel=None,
    alpha_fixed=None,
    flag_features=(),
):
    """One ridge fit for a given start-up constant; the pieces for the model.

    ``zero``: size variables whose exponent is held at zero (kept as features,
    so a prediction still needs them and checks their range). ``no_delta``:
    groups of ``spec.slope_by`` that use the shared exponents. ``parallel``:
    the start-up of a parallel run per machine, added to ``t0`` when the run
    has more than one core. ``alpha_fixed``: the parallel exponent from paired
    runs ({"alpha", "slope", "size_mean"}), held fixed instead of fitted."""
    parallel = parallel or {}

    def start(r):
        return t0[r["_machine"]] + (
            parallel.get(r["_machine"], 0.0) if r["_cores"] > 1 else 0.0
        )

    rows = []
    for r in all_rows:
        net0 = r["_wall"] - t0[r["_machine"]]
        # A run whose time is nearly all start-up says nothing about the
        # power law (its remainder is noise); it is predicted by t0 alone.
        # The parallel start-up does not change which runs are fitted (a
        # candidate that dropped the small parallel runs would score better for
        # fitting fewer of them): one it overstates leaves a floor of net time,
        # which the score then penalizes.
        net = max(r["_wall"] - start(r), 0.1 * net0)
        if net0 > max(0.02, 0.1 * t0[r["_machine"]]):
            r = dict(r)
            extra = (setup or {}).get(str(r.get(spec.setup_by, "") or ""), 0.0)
            r["_work"] = r["_units"] + extra if spec.setup_by else r["_units"]
            r["_y"] = math.log(net / (r["_work"] * r["_mult"]))
            if alpha_fixed is not None:
                # Back to one core with the paired-run exponent
                r["_y"] += _alpha_at(alpha_fixed, r, features) * math.log(r["_cores"])
            r["_net"] = net
            rows.append(r)
    if len(rows) < min_rows:
        return None

    classes = sorted({r["_class"] for r in rows})
    tasks = sorted({r["_task"] for r in rows})
    machines = sorted({r["_machine"] for r in rows})
    ref_class = max(classes, key=lambda c: sum(1 for r in rows if r["_class"] == c))
    ref_task = max(tasks, key=lambda t: sum(1 for r in rows if r["_task"] == t))
    cores = np.array([r["_cores"] for r in rows])
    fit_alpha = len({round(c) for c in cores}) > 1 and alpha_fixed is None
    alpha_note = "from paired runs" if alpha_fixed is not None else ""
    if fit_alpha and features:
        # Cores chosen by size (4 for the small runs, 8 for the large) say
        # nothing about scaling: the fit would put the size's effect on the
        # cores. Then alpha is assumed, not fitted.
        log_size = np.log([r["_size"][features[0]] for r in rows])
        if np.std(log_size) > 0 and np.std(np.log(cores)) > 0:
            corr = float(np.corrcoef(np.log(cores), log_size)[0, 1])
            if abs(corr) > CORES_SIZE_CORRELATION:
                fit_alpha = False
                alpha_note = f"cores follow the size (r={corr:+.2f}); alpha assumed"

    # Groups with their own size exponents (spec.slope_by): the most common
    # value is the reference and uses the shared exponents; another gets its
    # own when it has enough runs spanning sizes.
    group_of = [
        str(r.get(spec.slope_by, "") or "") if spec.slope_by else "" for r in rows
    ]
    groups = sorted(set(group_of))
    ref_group = max(groups, key=group_of.count)
    fitted = [n for n in features if n not in zero]
    delta_groups = []
    for g in groups:
        if g == ref_group or g in no_delta:
            continue
        mine = [r for r, h in zip(rows, group_of) if h == g]
        if len(mine) >= 3 and all(
            np.std(np.log([r["_size"][n] for r in mine])) > 0 for n in fitted
        ):
            delta_groups.append(g)

    # Design matrix: intercept, log sizes (shared, then each group's own),
    # [log cores], class dummies, task dummies
    columns = ["intercept"] + [f"log {n}" for n in fitted]
    X = [np.ones(len(rows))]
    for n in fitted:
        X.append(np.log([r["_size"][n] for r in rows]))
    for g in delta_groups:
        mask = np.array([1.0 if h == g else 0.0 for h in group_of])
        for n in fitted:
            columns.append(f"log {n} @ {g}")
            X.append(mask * np.log([r["_size"][n] for r in rows]))
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
    for t in flag_features:
        columns.append(f"flag {t}")
        X.append(np.array([1.0 if t in r["_flags"] else 0.0 for r in rows]))
    X = np.column_stack(X)
    y = np.array([r["_y"] for r in rows])
    machine_index = np.array([machines.index(r["_machine"]) for r in rows])

    # Weights: net time to a power, normalized to the number of rows
    power = WEIGHT_POWER if weight_power is None else weight_power
    weights = np.array([r["_net"] for r in rows]) ** power
    weights *= len(rows) / weights.sum()

    # Alternate: weighted ridge on the shared part, shrunk means for the machines.
    offsets = np.zeros(len(machines))
    penalty = np.full(X.shape[1], RIDGE)
    penalty[0] = 0.0
    for i, name in enumerate(columns):
        if name.startswith("flag "):
            penalty[i] = FLAG_RIDGE * len(rows)
    XW = X * weights[:, None]
    for _ in range(4):
        target = y - offsets[machine_index]
        beta = np.linalg.solve(XW.T @ X + np.diag(penalty), XW.T @ target)
        resid = y - X @ beta
        for m in range(len(machines)):
            mask = machine_index == m
            n = mask.sum()
            mean = np.average(resid[mask], weights=weights[mask]) if n else 0.0
            offsets[m] = mean * n / (n + MACHINE_SHRINK) if n else 0.0
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
    if alpha_fixed is not None:
        alpha = alpha_fixed["alpha"]
        alpha_slope = alpha_fixed["slope"]
        alpha_size_mean = alpha_fixed["size_mean"]
        # The fit's predictions back on the run's own cores
        X_cores = np.array(
            [-_alpha_at(alpha_fixed, r, features) * math.log(r["_cores"]) for r in rows]
        )
    else:
        X_cores = np.zeros(len(rows))

    predicted = np.exp(X @ beta + offsets[machine_index] + X_cores) * np.array(
        [r["_work"] * r["_mult"] for r in rows]
    ) + np.array([start(r) for r in rows])
    wall = np.array([r["_wall"] for r in rows])
    # The score a start-up constant is chosen by: the log error of the wall
    # time itself, which both the constant and the power law must explain
    score = float((weights * np.log(predicted / wall) ** 2).sum())
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
        "alpha_note": alpha_note,
        "slope_groups": groups,
        "delta_groups": delta_groups,
        "feature_log_range": {
            n: [
                float(np.log(min(r["_size"][n] for r in rows))),
                float(np.log(max(r["_size"][n] for r in rows))),
            ]
            for n in features
        },
        "predicted": predicted,
        "ss_res": float((resid**2).sum()),
        "parallel": dict(parallel),
    }


def _alpha_at(alpha_fixed, r, features):
    """The paired-run parallel exponent at a run's size, within [0, 1]."""
    alpha = alpha_fixed["alpha"]
    if features and alpha_fixed.get("size_mean") is not None:
        alpha += alpha_fixed["slope"] * (
            math.log(r["_size"][features[0]]) - alpha_fixed["size_mean"]
        )
    return min(1.0, max(0.0, alpha))


def _paired_alpha(rows, features, t0, parallel):
    """The parallel exponent from paired runs, or None if there are too few.

    Runs that are the same calculation -- machine, method class, task, sizes and
    keywords -- on several core counts give the speed-up directly: with the
    start-up (and a parallel run's start-up) removed, the slope of log time
    against log cores is -alpha for that size. Production cannot show this,
    since its core counts follow its sizes (the main fit would put the size's
    effect on the cores). Per group alpha, weighted by its time; then
    alpha = a + b (log size - mean), the form :func:`predict` uses.
    """
    if not features:
        return None
    groups = {}
    for r in rows:
        key = (
            r["_machine"],
            r["_class"],
            r["_task"],
            tuple(round(r["_size"][n], 6) for n in features),
            str(r.get("keywords", "") or ""),
            r["_units"],
        )
        groups.setdefault(key, []).append(r)
    points = []
    for key, mine in groups.items():
        by_cores = {}
        for r in mine:
            start = t0[r["_machine"]] + (
                parallel.get(r["_machine"], 0.0) if r["_cores"] > 1 else 0.0
            )
            net = r["_wall"] - start
            if net > 0.5:
                by_cores.setdefault(r["_cores"], []).append(net)
        if len(by_cores) < 2:
            continue
        cores = np.array(sorted(by_cores))
        net = np.array([float(np.median(by_cores[c])) for c in cores])
        slope = np.polyfit(np.log(cores), np.log(net), 1)[0]
        points.append(
            (
                math.log(mine[0]["_size"][features[0]]),
                min(1.0, max(0.0, -float(slope))),
                float(net.sum()),
            )
        )
    if len(points) < PAIRED_MIN_GROUPS:
        return None
    s = np.array([p[0] for p in points])
    a = np.array([p[1] for p in points])
    w = np.array([p[2] for p in points])
    w = w / w.sum()
    mean = float(np.average(s, weights=w))
    if np.ptp(s) > 0:
        A = np.column_stack([np.ones_like(s), s - mean])
        coef = np.linalg.lstsq(A * np.sqrt(w)[:, None], a * np.sqrt(w), rcond=None)[0]
        alpha, slope = float(coef[0]), float(coef[1])
    else:
        alpha, slope = float(np.average(a, weights=w)), 0.0
    return {
        "alpha": alpha,
        "slope": slope,
        "size_mean": mean,
        "groups": len(points),
    }


#: Columns that say where or how a run was recorded, not what it computed
_NOT_DRIVERS = {
    "schema", "date", "machine", "cluster", "partition", "cpu_model", "cpu_cores",
    "gpu_model", "host", "program", "ntasks", "cpus_per_task", "mem_per_cpu",
    "ngpus", "nprocs", "wall", "estimated", "state", "timed_out", "attempts",
    "in_situ", "code_seconds", "terminated_normally", "benchmark", "model",
}  # fmt: skip


def _residual_drivers(
    rows, resid, spec, min_rows=20, threshold=math.log(1.3), modeled=()
):
    """Recorded values the model does not use that still go with a systematic
    error -- candidates for a cost driver to add (a size, class or flag): for
    each other column with 2-30 values, each value seen in at least
    ``min_rows`` runs whose mean log error is beyond ``threshold``; and flags
    too rare for a factor of their own, the same way. The 20 largest."""
    used = set(spec.size) | set(spec.klass)
    used |= {spec.task, spec.units, spec.multiplier, spec.slope_by, spec.setup_by}
    if spec.flags:
        used.add(spec.flags.get("column"))
    columns = {k for r in rows for k in r if not k.startswith("_")}
    columns -= used | _NOT_DRIVERS
    drivers = []
    for column in sorted(columns):
        values = {}
        for r, e in zip(rows, resid):
            v = r.get(column)
            if v not in (None, ""):
                values.setdefault(str(v), []).append(float(e))
        if not 2 <= len(values) <= 30 or all(_num(v) is not None for v in values):
            # Numbers (sizes, counts, memory) are not categories
            continue
        for value, errors in values.items():
            mean = float(np.mean(errors))
            if len(errors) >= min_rows and abs(mean) > threshold:
                drivers.append(
                    {"column": column, "value": value, "rows": len(errors)}
                    | {"factor": math.exp(mean)}
                )
    flagged = {}
    for r, e in zip(rows, resid):
        for t in r["_flags"]:
            flagged.setdefault(t, []).append(float(e))
    for t, errors in flagged.items():
        mean = float(np.mean(errors))
        # A flag without a factor (too rare, or never contrasted) whose runs
        # are still off: a toggle ladder for it would let the fit learn it
        if t not in modeled and len(errors) >= 5 and abs(mean) > threshold:
            drivers.append(
                {"column": "flag", "value": t, "rows": len(errors)}
                | {"factor": math.exp(mean)}
            )
    drivers.sort(key=lambda d: -abs(math.log(d["factor"])))
    return drivers[:20]


def _assemble(program, spec, features, f):
    """The model dictionary from a fit's pieces."""
    rows, columns, beta, resid = f["rows"], f["columns"], f["beta"], f["resid"]
    t0 = f["t0"]
    coefficients = {
        name: float(b)
        for name, b in zip(columns, beta)
        if name not in ("log cores", "log cores x log size")
    }
    for n in features:
        coefficients.setdefault(f"log {n}", 0.0)  # held at zero
    slope_deltas = {
        g: {
            n: coefficients.pop(f"log {n} @ {g}")
            for n in features
            if f"log {n} @ {g}" in coefficients
        }
        for g in f["delta_groups"]
    }
    flag_factors = {
        name[5:]: coefficients.pop(name)
        for name in list(coefficients)
        if name.startswith("flag ")
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
        "slope_by": spec.slope_by,
        "slope_groups": f["slope_groups"],
        "slope_deltas": slope_deltas,  # {group: {feature: added slope}}
        "setup_by": spec.setup_by,
        "setup": f.get("setup", {}),  # {group: fixed cost per run, in units}
        "notes": f.get("notes", []),
        "alpha": f["alpha"],
        "alpha_slope": f["alpha_slope"],
        "alpha_size_mean": f["alpha_size_mean"],
        "alpha_fitted": f["fit_alpha"],
        "alpha_note": f["alpha_note"],
        "feature_log_range": f["feature_log_range"],
        "classes": class_offsets,
        "reference_class": f["ref_class"],
        "flags": flag_factors,  # {flag: log factor}
        "flag_vocabulary": f.get("vocabulary", []),
        "flag_support": {
            t: int(sum(1 for r in rows if t in r["_flags"])) for t in flag_factors
        },
        "drivers": _residual_drivers(rows, resid, spec, modeled=flag_factors),
        "tasks": task_offsets,
        "reference_task": f["ref_task"],
        "spec": spec.to_dict(),
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
                "parallel_startup": float(f.get("parallel", {}).get(m, 0.0)),
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
    """Write the model, atomically (a reader never sees a partial file)."""
    path = model_path(model["program"], directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(model, indent=2))
    os.replace(tmp, path)
    return path


def refresh_if_stale(
    program,
    directory=None,
    growth=REFIT_GROWTH,
    max_age_days=REFIT_DAYS,
    min_rows=8,
):
    """Refit the program's model if its records have grown or it is old.

    Stale: no model but enough records for one; the records' bytes have grown
    by more than ``growth`` since the fit; or the model is older than
    ``max_age_days`` and the records have changed since. The refit runs under a
    non-blocking lock (another process refitting means this one uses the model
    as it is), and a new model replaces the old one only if it is fitted to at
    least as many rows and predicts within 2x at least as often, or nearly so.
    Never raises.

    Returns
    -------
    str
        ``"refitted"``, ``"fresh"``, ``"locked"``, ``"no records"`` or
        ``"failed"``.
    """
    try:
        model = load_model(program, directory)
        size, mtime = records_state(program, directory)
        if size == 0:
            return "no records"
        if model is not None:
            old_size = model.get("records_bytes", 0) or 0
            fitted = model.get("fitted", "")
            try:
                age_days = (
                    time.time() - datetime.fromisoformat(fitted).timestamp()
                ) / 86400.0
            except ValueError:
                age_days = float("inf")
            grown = size > (1.0 + growth) * old_size
            aged = age_days > max_age_days and mtime > (model.get("records_mtime") or 0)
            # A new spec (e.g. a step that now declares flags) or a model made
            # by an older version of the fit is refitted too
            respec = load_spec(program, directory).to_dict() != model.get("spec")
            outdated = model.get("model_version", 1) < MODEL_VERSION
            if not (grown or aged or respec or outdated):
                return "fresh"
        lock_path = model_path(program, directory).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock:
            if not _try_lock(lock):
                return "locked"
            try:
                new = fit(program, directory, min_rows=min_rows)
                if new is None:
                    return "no records"
                if model is not None:
                    enough = new["rows"] >= 0.9 * model["rows"]
                    as_good = new["report"]["within_2x"] >= (
                        model["report"]["within_2x"] - 0.1
                    )
                    if not (enough and as_good):
                        logger.warning(
                            f"The refitted {program} timing model is worse "
                            f"({new['rows']} rows, {new['report']['within_2x']:.0%} "
                            f"within 2x) than the one kept ({model['rows']} rows, "
                            f"{model['report']['within_2x']:.0%}); not replaced."
                        )
                        # Remember the records' state so this is not retried on
                        # every prediction until they grow again
                        model["records_bytes"] = size
                        model["records_mtime"] = mtime
                        save_model(model, directory)
                        return "failed"
                save_model(new, directory)
                logger.info(
                    f"Refitted the {program} timing model to {new['rows']} rows."
                )
                return "refitted"
            finally:
                _unlock(lock)
    except Exception as e:
        logger.warning(f"Could not refresh the {program} timing model: {e}")
        return "failed"


def _try_lock(fd):
    try:
        import fcntl

        fcntl.lockf(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):
        return False


def _unlock(fd):
    try:
        import fcntl

        fcntl.lockf(fd.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError, ValueError):
        pass


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
    refresh=True,
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
    refresh : bool
        Refit the model first if its records have grown by a fifth or it is a
        week old (:func:`refresh_if_stale`); a model given is used as it is.

    Returns
    -------
    dict or None
        ``seconds`` (at the quantile), ``median``, ``units``, ``machine_known``,
        ``quantile``, ``spread`` (the log-space spread used), ``model`` (its date
        and rows); None when there is no model or the descriptors lack every
        size variable.
    """
    if model is None:
        if refresh:
            refresh_if_stale(program, directory)
        model = load_model(program, directory)
    if model is None:
        return None
    sizes = {}
    for name in model["features"]:
        value = _num(descriptors.get(name))
        if value is not None and value > 0:
            sizes[name] = value
    if len(sizes) < len(model["features"]) or not sizes:
        # Every size variable is needed: a mean in its place is a guess
        return None
    margin = math.log(EXTRAPOLATION)
    for name, (low, high) in model.get("feature_log_range", {}).items():
        if name in sizes and not (
            low - margin <= math.log(sizes[name]) <= high + margin
        ):
            # Outside what the records cover: a power law extrapolated by
            # orders of magnitude is not an estimate (a model fitted to small
            # molecules gave 20-atom QZ fragments 11 hours and 20 s)
            return None
    group = None
    if model.get("slope_by"):
        group = str(descriptors.get(model["slope_by"], "") or "")
        if group not in model.get("slope_groups", [group]):
            # A regime the records have not seen
            return None
    deltas = model.get("slope_deltas", {}).get(group, {})
    coef = model["coefficients"]
    y = coef["intercept"]
    for name in model["features"]:
        slope = coef.get(f"log {name}", 0.0) + deltas.get(name, 0.0)
        y += slope * math.log(sizes[name])
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
    if model["class_columns"] and klass not in model["classes"]:
        # A method class the records have not seen (or none given)
        return None
    y += model["classes"].get(klass, 0.0)
    task = (
        str(descriptors.get(model["task_column"], "") or "")
        if model["task_column"]
        else ""
    )
    if model["task_column"] and task not in model["tasks"]:
        return None
    y += model["tasks"].get(task, 0.0)
    unknown = []
    flag_spec = (model.get("spec") or {}).get("flags")
    if flag_spec:
        flags = flag_tokens(descriptors, flag_spec)
        y += sum(model.get("flags", {}).get(t, 0.0) for t in flags)
        vocabulary = set(model.get("flag_vocabulary", []))
        unknown = sorted(t for t in flags if t not in vocabulary)

    machine = machine or machine_class()["machine"]
    info = model["machines"].get(machine)
    known = info is not None
    if known:
        y += info["offset"]
        t0 = info["t0"]
        if cores > 1:
            t0 += info.get("parallel_startup", 0.0)
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
    if unknown and quantile > 0.5:
        # Options the records have never seen may cost anything
        spread += min(FLAG_UNKNOWN_MAX, FLAG_UNKNOWN_SPREAD * len(unknown))
    work = units
    if model.get("setup_by"):
        group = str(descriptors.get(model["setup_by"], "") or "")
        work = units + model.get("setup", {}).get(group, 0.0)
    median = math.exp(y) * work * mult + t0
    seconds = math.exp(y + spread) * work * mult + t0
    return {
        "seconds": seconds,
        "median": median,
        "units": units,
        "machine_known": known,
        "machine": machine,
        "quantile": quantile,
        "spread": spread,
        "unknown_flags": unknown,
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
        + (
            ""
            if model["alpha_fitted"]
            else " [assumed"
            + (": " + model["alpha_note"] if model.get("alpha_note") else "")
            + "]"
        ),
    ]
    for group, deltas in sorted(model.get("slope_deltas", {}).items()):
        lines.append(
            f"  {model['slope_by']} {group}: "
            + ", ".join(
                f"{model['coefficients'][f'log {n}'] + deltas.get(n, 0.0):.3f} log({n})"
                for n in model["features"]
            )
        )
    for group, value in sorted(model.get("setup", {}).items()):
        lines.append(
            f"  {model['setup_by']} {group or '(none)'}: setup {value:g} "
            "iterations per run"
        )
    for note in model.get("notes", []):
        lines.append(f"  note: {note}")
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
    for name, offset in sorted(
        model.get("flags", {}).items(), key=lambda kv: -abs(kv[1])
    ):
        lines.append(
            f"  flag {name}: x{math.exp(offset):.2f} "
            f"({model.get('flag_support', {}).get(name, 0)} runs)"
        )
    for name, info in sorted(model["machines"].items(), key=lambda kv: kv[1]["offset"]):
        parallel = info.get("parallel_startup", 0.0)
        lines.append(
            f"  machine {name or '(unknown)'}: x{math.exp(info['offset']):.2f} "
            f"({info['rows']} rows, t0 {info['t0']:.2f} s"
            + (f", parallel start-up {parallel:.1f} s" if parallel else "")
            + ")"
        )
    for d in model.get("drivers", []):
        lines.append(
            f"  possible cost driver (not in the model): {d['column']} = "
            f"{d['value']}: x{d['factor']:.2f} ({d['rows']} runs)"
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
