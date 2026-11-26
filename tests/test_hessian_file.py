# -*- coding: utf-8 -*-

"""The Hessian file (save_hessian / load_hessian), Paul 2026-11-26: keep the
Hessian with its masses, units, route and geometry."""

import numpy as np
import pytest

from seamm_exec.hessian import frequencies, load_hessian, save_hessian


def test_round_trip(tmp_path):
    k = 0.5
    H = np.zeros((6, 6))
    H[0, 0] = H[3, 3] = k
    H[0, 3] = H[3, 0] = -k
    xyz = [[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]]
    path = tmp_path / "hessian.npz"
    save_hessian(
        path,
        H,
        [1.008, 1.008],
        xyz,
        ["H", "H"],
        route="analytic over MDI",
        settings="sha256:abc",
        model="ORCA:CMP@R2SCAN-3C",
    )
    data = load_hessian(path)
    assert np.array_equal(data["hessian"], H)
    assert data["symbols"] == ["H", "H"]
    assert np.allclose(data["xyz"], xyz)
    info = data["info"]
    assert info["units"]["hessian"] == "E_h/bohr^2"
    assert info["route"] == "analytic over MDI" and info["version"] == 1
    # it reads back with plain numpy too
    with np.load(path) as raw:
        assert frequencies(raw["hessian"], raw["masses"]).max() > 4000


def test_shape_is_checked(tmp_path):
    with pytest.raises(ValueError, match="shape"):
        save_hessian(tmp_path / "h.npz", np.zeros((5, 5)), [1.0], [[0, 0, 0]], ["H"])
