# -*- coding: utf-8 -*-
"""The seed benchmark flowchart (seamm_exec.timing_benchmark)."""

import json
import shutil
import subprocess
import sys

import pytest
import yaml

from seamm_exec import timing_benchmark as tb

# Stand-ins for what code steps declare (seamm_exec itself knows no code)
MOLECULES = (
    ("water", "O", 3),
    ("ethanol", "CCO", 9),
    ("toluene", "Cc1ccccc1", 15),
    ("caffeine", "Cn1cnc2c1c(=O)n(C)c(=O)n2C", 24),
    ("icosane", "C" * 20, 62),
    ("hectane", "C" * 100, 302),
    ("alkane-300", "C" * 300, 902),
    ("alkane-1000", "C" * 1000, 3002),
)
SYSTEMS = [
    {"name": n, "size": z, "steps": [{"FromSMILESStep": {"smiles string": s}}]}
    for n, s, z in MOLECULES
]
ORCA = {
    "program": "orca",
    "step": "ORCA",
    "parallel": True,
    "systems": SYSTEMS[:6],
    "chemistries": {
        "ORCA:DFT@B3LYP/def2-SVP": {"quick": 24, "full": 62},
        "ORCA:MP2@MP2/def2-SVP": {"quick": 15, "full": 24},
        "ORCA:DFT@REVDSD-PBEP86-D4_2021/def2-TZVPPD": {"quick": 15, "full": 24},
    },
    "tasks": {
        "Energy": {"quick": 62, "full": 302},
        "Optimization": {"quick": 9, "full": 15},
    },
    "variants": {"Energy": [{}, {"results": {"gradients": {}}}]},
}
MOPAC = {
    "program": "mopac",
    "step": "MOPAC",
    "parallel": False,
    "systems": SYSTEMS,
    "chemistries": {"PM7": {"quick": 902, "full": 3002}},
    "parameter": "hamiltonian",
    "tasks": {"Energy": {"quick": 902, "full": 3002}},
    "variants": {
        "Energy": [{}, {"MOZYME": "never", "_min_size": 300, "_max_size": 902}]
    },
}


@pytest.fixture(autouse=True)
def declared(monkeypatch):
    monkeypatch.setattr(
        tb, "declarations", lambda refresh=False: {"orca": ORCA, "mopac": MOPAC}
    )


def test_spec_quick():
    text = tb.build_spec(("orca", "mopac"), "quick")
    spec = yaml.safe_load(text)
    steps = spec["steps"]
    names = [list(s)[0] if isinstance(s, dict) else s for s in steps]
    # Each code builds its own systems up to its tier limit: ORCA water to
    # caffeine (4), MOPAC water to the 902-atom alkane (7)
    assert names.count("FromSMILESStep") == 11
    assert names.count("ORCA") > 0 and names.count("MOPAC") > 0
    orca_mcs = [
        s["Model Chemistry"]["model chemistry"] for s in steps if "Model Chemistry" in s
    ]
    assert "ORCA:MP2@MP2/def2-SVP" in orca_mcs
    assert "ORCA:DFT@REVDSD-PBEP86-D4_2021/def2-TZVPPD" in orca_mcs
    assert (
        text.count("REVDSD-PBEP86-D4_2021") == 8
    )  # 3 x (energy, gradient) + 2 optimizations
    assert text.count('{results: {"gradients": {}}}') == 10
    mopac = [s["MOPAC"]["steps"][0] for s in steps if "MOPAC" in s]
    assert {list(sub.values())[0]["hamiltonian"] for sub in mopac} == {"PM7"}
    assert 'MOZYME: "never"' in text  # the second regime from 302 atoms
    assert "Optimization" in text
    assert "ORCA" not in text[text.index(json.dumps("C" * 100)) :]


def test_spec_orca_only_full():
    spec = yaml.safe_load(tb.build_spec(("orca",), "full"))
    names = [list(s)[0] for s in spec["steps"]]
    assert "MOPAC" not in names
    assert names.count("FromSMILESStep") == 5  # icosane is the largest at full


def test_unknown_code():
    with pytest.raises(ValueError, match="declares a timing benchmark"):
        tb.build_spec(("vasp",))


def test_discovery_finds_installed_declarations(monkeypatch):
    """The real discovery: every step found through the entry points that
    exports TIMING_BENCHMARK, keyed by its program."""
    monkeypatch.undo()
    found = tb.declarations(refresh=True)
    for program, declaration in found.items():
        assert declaration["program"] == program
        assert declaration["systems"] and declaration["chemistries"]
        assert declaration["tasks"] and declaration["step"]


def test_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(tb, "build_flowchart", lambda spec, out: out)
    monkeypatch.setattr(tb.psutil, "cpu_count", lambda logical=False: 8)
    monkeypatch.setattr(tb, "performance_cores", lambda: 8)
    set_id = tb.run(
        ("orca", "mopac"), cores=(1, 4, 8, 16), directory=tmp_path, dry_run=True
    )
    out = capsys.readouterr().out
    assert set_id
    assert "16 core" not in out  # above the machine's 8 physical cores
    assert out.count("orca:") == 3 and out.count("mopac:") == 1  # mopac once, serial
    assert "orca-step --ncores 4" in out and "mopac-step --ncores 1" in out
    assert ".flow --ncores 4 orca-step" in out  # SEAMM's own cap, then the plug-in's


@pytest.mark.skipif(
    shutil.which("seamm-flowchart") is None, reason="no seamm-flowchart"
)
def test_build_with_installed_plugins(tmp_path, monkeypatch):
    monkeypatch.undo()  # the installed steps' own declarations
    if not {"orca", "mopac"} <= set(tb.declarations(refresh=True)):
        pytest.skip("orca-step and mopac-step with benchmarks not both installed")
    try:
        import from_smiles_step  # noqa: F401
    except ImportError:
        pytest.skip("from-smiles-step not installed")
    path = tb.build_flowchart(
        tb.build_spec(("orca", "mopac"), "quick"), tmp_path / "b.flow"
    )
    assert path.exists()
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "seamm_exec.timing_benchmark",
            "--build-only",
            "-o",
            str(tmp_path / "c.flow"),
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_performance_cores_cap(tmp_path, capsys, monkeypatch):
    """An Apple-silicon Mac with 5 performance cores sweeps to 5 only."""
    monkeypatch.setattr(tb, "build_flowchart", lambda spec, out: out)
    monkeypatch.setattr(tb.psutil, "cpu_count", lambda logical=False: 11)
    monkeypatch.setattr(tb, "performance_cores", lambda: 5)
    tb.run(("orca",), cores=(1, 4, 8, 16), directory=tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert "skipped: this machine has 5 performance core(s)" in out
    assert "8 core" not in out and "4 core" in out
