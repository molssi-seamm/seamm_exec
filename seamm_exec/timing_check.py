# -*- coding: utf-8 -*-

"""How well the estimates of recent runs matched their times.

Every timing record carries the estimate the task was given (``estimated``,
before a target's ``estimate_scale``) and the time it took (``wall``). After an
update, or whenever packing or walltimes look wrong, this compares them over
the newest records, grouped by machine and calculation, and flags

- groups whose estimates are far from the times (by default more than 2x), and
- calculations estimated the same on machines whose times differ: the sign
  that the estimates came from a step's fallback formula, which knows nothing of
  the machine, instead of from the fitted model.

Command line::

    python -m seamm_exec.timing_model check [program ...] [--since 2h]
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
import re

from .timing import read_timings

#: A group is flagged when its estimates are further than this factor from the
#: times (the geometric mean of estimate / wall)
LIMIT = 2.0
#: Estimates closer than this fraction count as the same
SAME = 0.01
#: ... while times further apart than this factor are different
DIFFERENT = 1.25
#: Columns that describe the calculation, when the records have them
CALCULATION_COLUMNS = ("task", "method", "basis", "n_atoms")


def parse_since(text, now=None):
    """The time ``text`` names, as an aware datetime.

    ``text`` is either a span back from now -- a number and m, h or d (``90m``,
    ``2h``, ``1d``) -- or a date and time, ``2026-10-16T10:30``, in local time
    unless it gives its offset. The records' dates are in UTC.
    """
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"\s*(\d+(?:\.\d*)?)\s*([mhd])\s*", text or "")
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now - timedelta(**{unit: float(m.group(1))})
    when = datetime.fromisoformat(text.strip())
    if when.tzinfo is None:
        when = when.astimezone()  # local time
    return when


def _number(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _date(row):
    try:
        when = datetime.fromisoformat(row.get("date") or "")
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _timed_out(row):
    return str(row.get("timed_out") or "").strip().lower() in ("1", "true", "yes")


def check(program, directory=None, since=None, by=None, limit=LIMIT, rows=None):
    """Compare the estimates of ``program``'s recent runs with their times.

    Parameters
    ----------
    program : str
    directory : str, optional
        The timing directory.
    since : datetime, optional
        Only runs recorded at or after this (aware) time.
    by : sequence of str, optional
        The columns describing a calculation; default the method class, task,
        method, basis and atom count, those the records have. The machine is
        always the first.
    limit : float
        The factor beyond which a group's estimates are flagged.
    rows : list of dict, optional
        The records, instead of reading them.

    Returns
    -------
    dict
        ``groups``: a list of dicts, one per machine and calculation, with
        ``machine``, ``key`` (the calculation's values), ``rows``, ``wall``
        (mean seconds), ``estimated`` (mean seconds), ``ratio`` (geometric mean
        of estimate / wall), ``low`` and ``high`` (its 10th and 90th
        percentiles), ``distinct`` (the number of different estimates),
        ``sources`` (rows per ``estimate_source``, when recorded) and ``flags``;
        ``columns`` (the calculation's columns), ``checked``, ``no_estimate``
        and ``timed_out`` (counts of runs).
    """
    if rows is None:
        rows = read_timings(program, directory, all_files=since is not None)
    if by is None:
        from .timing_model import load_spec

        spec = load_spec(program, directory)
        by = [*spec.klass, *CALCULATION_COLUMNS]
    columns = []
    for name in by:
        if name not in columns and name != "machine":
            if any(row.get(name) not in (None, "") for row in rows):
                columns.append(name)

    groups = defaultdict(list)
    no_estimate = timed_out = 0
    for row in rows:
        if since is not None:
            when = _date(row)
            if when is None or when < since:
                continue
        wall = _number(row.get("wall"))
        if wall is None:
            continue
        if _timed_out(row):
            timed_out += 1
            continue
        estimated = _number(row.get("estimated"))
        if estimated is None:
            no_estimate += 1
            continue
        key = tuple(str(row.get(c) or "") for c in columns)
        source = str(row.get("estimate_source") or "")
        groups[(row.get("machine") or "", key)].append((wall, estimated, source))

    result = []
    for (machine, key), values in groups.items():
        walls = [w for w, _, _ in values]
        estimates = [e for _, e, _ in values]
        logs = sorted(math.log(e / w) for w, e, _ in values)
        n = len(logs)
        sources = defaultdict(int)
        for _, _, source in values:
            if source:
                sources[source] += 1
        ratio = math.exp(sum(logs) / n)
        flags = []
        if ratio > limit or ratio < 1 / limit:
            flags.append(f"estimates {ratio:.2g}x the time")
        result.append(
            {
                "machine": machine,
                "key": key,
                "rows": n,
                "wall": sum(walls) / n,
                "estimated": sum(estimates) / n,
                "ratio": ratio,
                "low": math.exp(logs[int(0.1 * (n - 1))]),
                "high": math.exp(logs[int(0.9 * (n - 1) + 0.5)]),
                "distinct": len({round(e, 1) for e in estimates}),
                "sources": dict(sources),
                "flags": flags,
            }
        )

    # The same calculation estimated the same on machines that differ in speed
    by_key = defaultdict(list)
    for group in result:
        by_key[group["key"]].append(group)
    for key, same in by_key.items():
        for i, a in enumerate(same):
            for b in same[i + 1 :]:
                close = abs(a["estimated"] - b["estimated"]) <= SAME * max(
                    a["estimated"], b["estimated"]
                )
                apart = max(a["wall"], b["wall"]) > DIFFERENT * min(
                    a["wall"], b["wall"]
                )
                if close and apart:
                    for g, other in ((a, b), (b, a)):
                        g["flags"].append(
                            "same estimate as on "
                            + (_short_machine(other["machine"]) or "(unknown)")
                        )

    result.sort(key=lambda g: (g["key"], g["machine"]))
    return {
        "program": program,
        "columns": columns,
        "groups": result,
        "checked": sum(g["rows"] for g in result),
        "no_estimate": no_estimate,
        "timed_out": timed_out,
    }


def _short_machine(machine):
    """``cluster:partition:cpu`` shortened to the cluster and a short CPU name."""
    parts = machine.split(":")
    cpu = parts[-1]
    m = re.search(r"(EPYC|Xeon|Core|Apple)\D*\s*([\w-]+)", cpu)
    if m:
        cpu = f"{m.group(1)} {m.group(2)}"
    return ":".join([*parts[:-2], cpu]) if len(parts) >= 3 else machine


def report(summary, min_rows=1):
    """The text of a :func:`check`: a table of the groups, flags last."""
    columns = summary["columns"]
    header = ["machine", *columns, "rows", "wall s", "est s", "est/wall", "10-90%"]
    sources = sorted({s for g in summary["groups"] for s in g["sources"]})
    if sources:
        header.append("source")
    header.append("")
    table = [header]
    shown = [g for g in summary["groups"] if g["rows"] >= min_rows]
    for g in shown:
        line = [
            _short_machine(g["machine"]) or "(unknown)",
            *g["key"],
            str(g["rows"]),
            f"{g['wall']:.0f}",
            f"{g['estimated']:.0f}",
            f"{g['ratio']:.2f}",
            f"{g['low']:.2f}-{g['high']:.2f}",
        ]
        if sources:
            line.append(
                " ".join(f"{s} {g['sources'][s]}" for s in sorted(g["sources"]))
            )
        line.append("<-- " + "; ".join(g["flags"]) if g["flags"] else "")
        table.append(line)
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    numeric = {"rows", "wall s", "est s", "est/wall"}
    lines = [f"{summary['program']}:"]
    for r in table:
        cells = [
            c.rjust(w) if header[i] in numeric else c.ljust(w)
            for i, (c, w) in enumerate(zip(r, widths))
        ]
        lines.append("  " + "  ".join(cells).rstrip())
    flagged = sum(1 for g in shown if g["flags"])
    lines.append(
        f"  {summary['checked']} runs in {len(summary['groups'])} groups"
        + (f" ({len(shown)} shown)" if len(shown) < len(summary["groups"]) else "")
        + f"; {flagged} flagged"
        + (
            f"; {summary['no_estimate']} without an estimate"
            if summary["no_estimate"]
            else ""
        )
        + (
            f"; {summary['timed_out']} timed out (left out)"
            if summary["timed_out"]
            else ""
        )
    )
    return "\n".join(lines)
