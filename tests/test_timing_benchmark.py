# -*- coding: utf-8 -*-
"""The seed benchmark flowchart (seamm_exec.timing_benchmark)."""

import json
import shutil
import subprocess
import sys

import pytest
import yaml

from seamm_exec import timing_benchmark as tb


def test_spec_quick():
    text = tb.build_spec(("orca", "mopac"), "quick")
    spec = yaml.safe_load(text)
    steps = spec["steps"]
    names = [list(s)[0] if isinstance(s, dict) else s for s in steps]
    # Every molecule up to the largest any code runs at this tier is built once:
    # water to the 902-atom alkane (the 3002-atom one is 'full' only)
    assert names.count("FromSMILESStep") == 7
    assert names.count("ORCA") > 0 and names.count("MOPAC") > 0
    # ORCA takes its method from a Model Chemistry step; MP2 is among them
    orca_mcs = [
        s["Model Chemistry"]["model chemistry"] for s in steps if "Model Chemistry" in s
    ]
    assert "ORCA:MP2@MP2/def2-SVP" in orca_mcs
    # MOPAC sets its Hamiltonian on the sub-step, both PM7 and PM6-ORG
    mopac = [s["MOPAC"]["steps"][0] for s in steps if "MOPAC" in s]
    hams = {list(sub.values())[0]["hamiltonian"] for sub in mopac}
    assert hams == {"PM7", "PM6-ORG"}
    text_lines = text.splitlines()
    i = next(i for i, ln in enumerate(text_lines) if json.dumps("C" * 100) in ln)
    after = "\n".join(text_lines[i:])
    assert "ORCA" not in after  # ORCA stops at caffeine in the quick tier
    assert 'MOZYME: "never"' in after  # both MOPAC regimes from 302 atoms
    assert "Optimization" in text


def test_spec_orca_only_full():
    spec = yaml.safe_load(tb.build_spec(("orca",), "full"))
    names = [list(s)[0] for s in spec["steps"]]
    assert "MOPAC" not in names
    # hectane and beyond are past every ORCA chemistry's limit: five molecules
    assert names.count("FromSMILESStep") == 5


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
def test_build_with_installed_plugins(tmp_path):
    try:
        import from_smiles_step  # noqa: F401
        import mopac_step  # noqa: F401
        import orca_step  # noqa: F401
    except ImportError:
        pytest.skip("from-smiles-step, orca-step and mopac-step not all installed")
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
