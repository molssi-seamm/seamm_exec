# -*- coding: utf-8 -*-

"""The shared timing files (seamm_exec.timing)."""

import subprocess
import sys

from seamm_exec.timing import (
    COMMON_COLUMNS,
    append_timing,
    machine_class,
    read_timings,
    record_task_timing,
    timing_path,
)


def test_rows_and_header(tmp_path):
    append_timing("code", {"atoms": 3, "cores": 4, "wall": 1.5}, directory=tmp_path)
    append_timing("code", {"atoms": 5, "wall": 2.0}, directory=tmp_path)
    rows = read_timings("code", directory=tmp_path)
    assert rows == [
        {"atoms": "3", "cores": "4", "wall": "1.5"},
        {"atoms": "5", "cores": "", "wall": "2.0"},
    ]


def test_a_row_is_one_line(tmp_path):
    append_timing("code", {"input": "line 1\nline 2", "wall": 1}, directory=tmp_path)
    text = timing_path("code", tmp_path).read_text()
    assert text.count("\n") == 2  # header and one row


def test_new_columns_begin_a_new_file(tmp_path):
    append_timing("code", {"a": 1}, directory=tmp_path)
    append_timing("code", {"a": 2, "b": 3}, directory=tmp_path)
    append_timing("code", {"b": 4}, directory=tmp_path)
    # The current file has the wider header; the old one was set aside
    assert read_timings("code", directory=tmp_path) == [
        {"a": "2", "b": "3"},
        {"a": "", "b": "4"},
    ]
    rows = read_timings("code", directory=tmp_path, all_files=True)
    assert rows == [{"a": "1"}, {"a": "2", "b": "3"}, {"a": "", "b": "4"}]
    assert len(list(tmp_path.glob("code-*.csv"))) == 1


def test_machine_class():
    machine = machine_class()
    assert machine["cpu_model"] and machine["host"]
    assert machine["machine"].endswith(machine["cpu_model"])
    assert "cluster" in machine and "partition" in machine
    with_gpu = machine_class(gpu_model="A30")
    assert with_gpu["machine"] == machine["machine"] + ":A30"
    assert with_gpu["gpu_model"] == "A30"


def _task_and_result(**kwargs):
    from seamm_exec import Resources, Task, TaskResult

    task = Task(
        key="t1",
        program="code",
        cmd=["x"],
        resources=Resources(ntasks=4, mem_per_cpu=2_000_000_000),
        estimated_seconds=12.5,
    )
    result = TaskResult(
        key="t1",
        state="finished",
        returncode=0,
        attempts=1,
        in_situ=True,
        history=[
            {"attempt": 0, "started": 100.0, "finished": 101.5},
            {"attempt": 1, "started": 200.0, "finished": 230.25},
        ],
        **kwargs,
    )
    return task, result


def test_record_task_timing(tmp_path):
    task, result = _task_and_result()
    path = record_task_timing(
        task,
        result,
        {"task": "energy", "n_atoms": 3, "nbf": 24, "x": 1.23456789, "ok": True},
        directory=tmp_path,
    )
    assert path == timing_path("code", tmp_path)
    (row,) = read_timings("code", directory=tmp_path)
    assert row["schema"] == "1"
    assert row["program"] == "code"
    assert row["ntasks"] == "4" and row["mem_per_cpu"] == "2000000000"
    assert row["wall"] == "30.250"  # the last attempt
    assert row["estimated"] == "12.5"
    assert row["state"] == "finished" and row["timed_out"] == "0"
    assert row["in_situ"] == "1"
    assert row["machine"] == machine_class()["machine"]
    assert row["task"] == "energy" and row["n_atoms"] == "3" and row["nbf"] == "24"
    assert row["x"] == "1.23457" and row["ok"] == "1"
    # The common columns come first, in order
    header = timing_path("code", tmp_path).read_text().splitlines()[0].split(",")
    assert header[: len(COMMON_COLUMNS)] == list(COMMON_COLUMNS)


def test_restored_result_is_not_recorded(tmp_path):
    task, result = _task_and_result(restored=True)
    assert record_task_timing(task, result, {"n": 1}, directory=tmp_path) is None
    assert not timing_path("code", tmp_path).exists()


def test_descriptors_cannot_override_common_columns(tmp_path):
    task, result = _task_and_result()
    record_task_timing(task, result, {"program": "other", "n": 1}, directory=tmp_path)
    (row,) = read_timings("code", directory=tmp_path)
    assert row["program"] == "code" and row["n"] == "1"


def test_large_file_is_set_aside(tmp_path):
    for i in range(20):
        append_timing(
            "code", {"i": i, "pad": "x" * 100}, directory=tmp_path, max_bytes=500
        )
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "code.csv" in files and any(f.startswith("code-") for f in files)
    rows = read_timings("code", directory=tmp_path)
    assert rows and int(rows[-1]["i"]) == 19


def test_concurrent_writers(tmp_path):
    """Eight processes appending at once: every row whole, none lost."""
    code = (
        "import sys; from seamm_exec.timing import append_timing\n"
        "for i in range(100):\n"
        "    append_timing('code', {'writer': sys.argv[1], 'i': i, 'pad': 'y' * 200},"
        f" directory={str(tmp_path)!r})\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(w)]) for w in range(8)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    rows = read_timings("code", directory=tmp_path)
    assert len(rows) == 800
    assert all(r["pad"] == "y" * 200 for r in rows)
    for w in range(8):
        mine = [int(r["i"]) for r in rows if r["writer"] == str(w)]
        assert mine == list(range(100))


def test_concurrent_rotation_keeps_every_row(tmp_path):
    """Writers racing at rotation: no set-aside file is overwritten, no row lost."""
    code = (
        "import sys; from seamm_exec.timing import append_timing\n"
        "for i in range(25):\n"
        "    append_timing('code', {'writer': sys.argv[1], 'i': i, 'pad': 'z' * 100},"
        f" directory={str(tmp_path)!r}, max_bytes=1000)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(w)]) for w in range(4)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    rows = []
    for path in sorted(tmp_path.glob("code*.csv")):
        rows += read_timings(path.stem, directory=tmp_path)
    assert len(rows) == 100
    for w in range(4):
        mine = sorted(int(r["i"]) for r in rows if r["writer"] == str(w))
        assert mine == list(range(25))
