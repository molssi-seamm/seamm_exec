# -*- coding: utf-8 -*-
"""Comparing the estimates of recent runs with their times
(seamm_exec.timing_check, ``timing_model check``)."""

from datetime import datetime, timedelta, timezone

import pytest

from seamm_exec import timing_model as tm
from seamm_exec.timing import append_timing
from seamm_exec.timing_check import check, parse_since, report

OWL = "owl:normal_q:AMD EPYC 9454 48-Core Processor"
TC = "tinkercliffs:normal_q:AMD EPYC 7702 64-Core Processor"
NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


def _row(machine, wall, estimated, method="r2SCAN", n_atoms=20, minutes=10, **kw):
    return {
        "date": (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
        "machine": machine,
        "program": "orca",
        "task": "gradient",
        "method_class": "meta-GGA",
        "method": method,
        "basis": "def2-TZVPPD",
        "n_atoms": n_atoms,
        "wall": wall,
        "estimated": estimated,
        **kw,
    }


def test_parse_since():
    assert parse_since("90m", now=NOW) == NOW - timedelta(minutes=90)
    assert parse_since("2h", now=NOW) == NOW - timedelta(hours=2)
    assert parse_since("1.5d", now=NOW) == NOW - timedelta(days=1.5)
    # A date with its offset is taken as given; without one, as local time
    assert parse_since("2026-10-16T10:30+00:00") == datetime(
        2026, 10, 16, 10, 30, tzinfo=timezone.utc
    )
    local = parse_since("2026-10-16T10:30")
    assert local.tzinfo is not None
    assert local == datetime(2026, 10, 16, 10, 30).astimezone()
    with pytest.raises(ValueError):
        parse_since("yesterday")


def test_good_estimates_are_not_flagged():
    rows = [_row(OWL, 100 + i, 110 + i) for i in range(5)]
    rows += [_row(TC, 200 + i, 190 + i) for i in range(5)]
    summary = check("orca", rows=rows)
    assert summary["checked"] == 10 and len(summary["groups"]) == 2
    assert summary["columns"] == ["method_class", "task", "method", "basis", "n_atoms"]
    for group in summary["groups"]:
        assert group["flags"] == [] and 0.9 < group["ratio"] < 1.15
    assert "0 flagged" in report(summary)


def test_estimates_far_from_the_times_are_flagged():
    rows = [_row(OWL, 135, 1289) for _ in range(4)]  # the formula, 2026-10-16
    rows += [_row(OWL, 900, 150, method="revDSD") for _ in range(4)]
    summary = check("orca", rows=rows)
    flags = {g["key"][2]: g["flags"] for g in summary["groups"]}
    assert flags["r2SCAN"] == ["estimates 9.5x the time"]
    assert flags["revDSD"] == ["estimates 0.17x the time"]
    # The limit applies both ways: 10x lets both through
    groups = check("orca", rows=rows, limit=10)["groups"]
    assert all(g["flags"] == [] for g in groups)


def test_the_same_estimate_on_machines_of_different_speed_is_flagged():
    """The fallback formula knows nothing of the machine: Owl and TinkerCliffs
    got 1289 s for the same calculation, which took 135 and 232 s."""
    rows = [_row(OWL, 135, 1289) for _ in range(3)]
    rows += [_row(TC, 232, 1289) for _ in range(3)]
    # Another calculation the model estimated per machine
    rows += [_row(OWL, 360, 400, n_atoms=30), _row(TC, 630, 700, n_atoms=30)]
    summary = check("orca", rows=rows, limit=100)
    flags = {(g["machine"], g["key"][-1]): g["flags"] for g in summary["groups"]}
    assert flags[(OWL, "20")] == ["same estimate as on tinkercliffs:EPYC 7702"]
    assert flags[(TC, "20")] == ["same estimate as on owl:EPYC 9454"]
    assert flags[(OWL, "30")] == [] and flags[(TC, "30")] == []
    # ... but not when the machines' times are alike as well
    rows = [_row(OWL, 135, 140), _row(TC, 138, 140)]
    assert all(g["flags"] == [] for g in check("orca", rows=rows)["groups"])


def test_since_timed_out_and_missing_estimates():
    rows = [
        _row(OWL, 100, 100, minutes=200),  # too old
        _row(OWL, 100, 100),
        _row(OWL, 100, 100, timed_out="True"),
        _row(OWL, 100, ""),
        _row(OWL, "", 100),  # not finished
        _row(OWL, 100, 100, date="not a date"),
    ]
    summary = check("orca", rows=rows, since=NOW - timedelta(hours=1))
    assert summary["checked"] == 1
    assert summary["timed_out"] == 1 and summary["no_estimate"] == 1
    text = report(summary)
    assert "1 timed out (left out)" in text and "1 without an estimate" in text


def test_grouping_columns_and_sources():
    rows = [_row(OWL, 100, 100, estimate_source="model") for _ in range(2)]
    rows += [_row(OWL, 100, 900, estimate_source="formula")]
    summary = check("orca", rows=rows, by=["method"])
    assert summary["columns"] == ["method"] and len(summary["groups"]) == 1
    group = summary["groups"][0]
    assert group["sources"] == {"model": 2, "formula": 1}
    assert group["distinct"] == 2
    assert "formula 1 model 2" in report(summary)
    # Columns no record has are left out
    assert check("orca", rows=rows, by=["method", "nbf"])["columns"] == ["method"]


def test_command_line(tmp_path, capsys):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in range(3):
        append_timing("orca", _row(OWL, 135, 1289, date=now), directory=tmp_path)
        append_timing("orca", _row(TC, 232, 1289, date=now), directory=tmp_path)
    status = tm.main(["--directory", str(tmp_path), "check", "orca", "--since", "1h"])
    out = capsys.readouterr().out
    assert status == 1
    assert "estimates 9.5x the time; same estimate as on" in out
    assert "6 runs in 2 groups; 2 flagged" in out
    # Nothing recent: nothing flagged
    status = tm.main(
        ["--directory", str(tmp_path), "check", "--since", "2000-01-01T00:00"]
    )
    assert status == 1  # 'all' records are newer than 2000
    status = tm.main(["--directory", str(tmp_path), "check", "orca", "--since", "0m"])
    assert status == 0
    assert "no runs with an estimate since 0m" in capsys.readouterr().out
