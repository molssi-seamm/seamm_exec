# -*- coding: utf-8 -*-

"""The shared timing files (seamm_exec.timing)."""

import subprocess
import sys

from seamm_exec.timing import append_timing, read_timings, timing_path


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


def test_columns_the_file_lacks_are_dropped(tmp_path):
    append_timing("code", {"a": 1}, directory=tmp_path)
    append_timing("code", {"a": 2, "b": 3}, directory=tmp_path)
    assert read_timings("code", directory=tmp_path) == [{"a": "1"}, {"a": "2"}]


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
