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
    record_timing,
    structure_descriptors,
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


def test_record_timing_without_a_task(tmp_path):
    path = record_timing(
        "code", 12.3456, {"n_atoms": 7}, ntasks=8, estimated=10.0, directory=tmp_path
    )
    (row,) = read_timings("code", directory=tmp_path)
    assert path == timing_path("code", tmp_path)
    assert row["wall"] == "12.346" and row["ntasks"] == "8" and row["n_atoms"] == "7"
    assert row["state"] == "finished" and row["attempts"] == "1"
    assert row["machine"] == machine_class()["machine"]


def test_structure_descriptors():
    from types import SimpleNamespace

    conf = SimpleNamespace(
        atoms=SimpleNamespace(atomic_numbers=[8, 1, 1, 17]),
        charge=-1,
        spin_multiplicity=1,
        periodicity=3,
        volume=1000.0,
    )
    d = structure_descriptors(conf)
    assert d["n_atoms"] == 4 and d["n_heavy"] == 2 and d["n_ghosts"] == 0
    assert d["n_electrons"] == 8 + 1 + 1 + 17 + 1
    assert d["periodicity"] == 3 and d["volume"] == 1000.0
    d = structure_descriptors(conf, atom_indices=[0, 1, 2, 3], ghost_atoms={3})
    assert d["n_atoms"] == 3 and d["n_heavy"] == 1 and d["n_ghosts"] == 1
    assert "n_electrons" not in d  # a subset: the charge is not the subset's
    assert structure_descriptors(object()) == {}


def test_benchmark_rows_are_marked(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAMM_TIMING_BENCHMARK", "set-1")
    record_timing("code", 1.0, {"n_atoms": 3}, directory=tmp_path)
    monkeypatch.delenv("SEAMM_TIMING_BENCHMARK")
    record_timing("code", 2.0, {"n_atoms": 3}, directory=tmp_path)
    rows = read_timings("code", directory=tmp_path)
    assert rows[0]["benchmark"] == "set-1" and rows[1]["benchmark"] == ""


def test_neighbour_count_tells_a_chain_from_a_cluster():
    """Along a chain an atom has few neighbours within 8 Å; in dense 3D matter
    many more, and the count is about what the density says."""
    import numpy as np

    from seamm_exec.timing import neighbour_count

    chain = [(1.5 * i, 0.0, 0.0) for i in range(600)]
    assert abs(neighbour_count(chain) - 10.0) < 0.5  # 5 each way within 8 Å
    rng = np.random.default_rng(1)
    density = 0.1  # atoms / Å^3, about water's
    side = (6000 / density) ** (1 / 3)
    cluster = rng.uniform(0, side, (6000, 3))
    inside = 4 / 3 * np.pi * 8.0**3 * density  # ~214, less at the surface
    count = neighbour_count(cluster)
    assert 0.5 * inside < count < inside
    assert count > 10 * neighbour_count(chain)
    # Periodic: the same random box, no surface, by minimum image
    assert abs(neighbour_count(cluster, cell=np.eye(3) * side) - inside) < 0.1 * inside
    # A cell narrower than twice the radius needs explicit images: one atom in
    # a 5 Å cube, against the lattice points within 8 Å counted directly
    small = neighbour_count([(0.0, 0.0, 0.0)], cell=np.eye(3) * 5.0)
    big = [
        (5.0 * i, 5.0 * j, 5.0 * k)
        for i in range(-3, 4)
        for j in range(-3, 4)
        for k in range(-3, 4)
    ]
    assert neighbour_count([(0, 0, 0)]) is None
    centre = sum(1 for x, y, z in big if 0 < x * x + y * y + z * z <= 64.0)
    assert small == centre


def test_structure_descriptors_include_neighbours():
    from seamm_exec import Geometry, structure_descriptors

    g = Geometry([6] * 50, [(1.5 * i, 0.0, 0.0) for i in range(50)])
    d = structure_descriptors(g)
    assert 9.0 < d["neighbours"] < 10.0
