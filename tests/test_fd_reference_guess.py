# -*- coding: utf-8 -*-

"""Finite-difference Hessians made reliably (2026-11-26, after the warm-engine
Hessian of 4-hydroxy-TEMPO, SEAMM job 6074): every displaced point starts from
the reference structure's orbitals, with a tight SCF; every point is checked for
a state change; a replacement Hessian is tested against gradient differences;
the zero modes are reported for information."""

import types

import numpy as np
import pytest

from seamm_exec.hessian import (
    EngineSession,
    HessianDriver,
    HessianRefused,
    _RestartingEngine,
    check_fd_points,
    check_hessian,
    frequencies,
    zero_modes,
)

from .test_hessian import BOHR, K, MC, Configuration, HarmonicEngine, make_node

#: How much the previous SCF's starting point leaks into the forces
LEAK = 1.0e-3


class PathEngine(HarmonicEngine):
    """A harmonic engine whose forces depend on where its SCF started: the
    previous calculation's solution, as a warm engine autostarting from the
    last orbitals. Without the guess commands its finite differences depend on
    the order of the points."""

    def __init__(self):
        super().__init__(analytic=False)
        self.guess = np.zeros(15)
        self.forces_calls = 0

    def forces(self, units="hartree/bohr"):
        self.forces_calls += 1
        f = -(K @ self.x) + LEAK * self.guess
        self.guess = self.x.copy()  # the next SCF starts from this one
        return f


class GuessEngine(PathEngine):
    """... with SAVE_GUESS / RESTORE_GUESS / TIGHT_SCF, an optional <S**2>, and
    optionally a point that falls into another state."""

    def __init__(self, spin=None, odd_point=None):
        super().__init__()
        self.saved = None
        self.tight = False
        self.tight_calls = []
        self.restores = 0
        self._spin = spin
        self.odd_point = odd_point  # (coordinate, sign) with another state

    def save_guess(self):
        self.saved = self.guess.copy()
        return True

    def restore_guess(self):
        self.guess = self.saved.copy()
        self.restores += 1
        return True

    def tight_scf(self, tight=True):
        self.tight = tight
        self.tight_calls.append(tight)
        return True

    def _is_odd(self):
        if self.odd_point is None or self.saved is None:
            return False
        j, sign = self.odd_point
        d = self.x - self.saved
        return abs(d[j] - sign * 0.01) < 1e-9

    def energy(self, units="kJ/mol"):
        e = 0.5 * self.x @ K @ self.x
        if self._is_odd() and self._spin is None:
            e -= 0.01  # another, lower state
        return e * 2625.4996394799 if units == "kJ/mol" else e

    def spin(self):
        if self._spin is None:
            return None, None
        if self._is_odd():
            return 0.95, 0.75
        return self._spin, 0.75


def session_of(engine, x0):
    driver = HessianDriver(make_node(MC))
    return EngineSession(driver, engine, Configuration(), None, [6, 1, 1, 1, 1], x0)


X0 = np.full((5, 3), 0.1)


def test_without_the_guess_the_differences_depend_on_the_path():
    session = session_of(PathEngine(), X0)
    H, how = session.hessian()
    assert how == "finite differences over MDI"
    assert not np.allclose(H, K, atol=1e-4)
    assert session.fd_info["reference guess"] is False


def test_the_reference_guess_makes_them_exact():
    engine = GuessEngine()
    session = session_of(engine, X0)
    H, _ = session.hessian()
    assert np.allclose(H, K, atol=1e-8)
    assert session.fd_info["reference guess"] is True
    assert session.fd_info["tight SCF"] is True
    assert session.fd_info["problems"] == []
    # tight for the differences, then the engine's own SCF again
    assert engine.tight_calls == [True, False] and engine.tight is False
    # every one of the 30 displaced points started from the reference orbitals
    assert engine.restores == 30
    # and the engine is back at the reference
    assert np.allclose(engine.x, X0.reshape(-1) / BOHR)


def test_a_point_in_another_spin_state_refuses_the_hessian():
    engine = GuessEngine(spin=0.76, odd_point=(4, 1))
    session = session_of(engine, X0)
    with pytest.raises(HessianRefused, match="<S\\*\\*2> 0.9500"):
        session.hessian()
    assert len(session.fd_info["problems"]) == 1


def test_a_point_below_the_reference_refuses_the_hessian():
    engine = GuessEngine(odd_point=(7, -1))
    session = session_of(engine, X0)
    with pytest.raises(HessianRefused, match="below the reference"):
        session.hessian()


def test_check_fd_points():
    g0 = np.zeros(6)
    g0[2] = 0.1
    ok = {"j": 2, "sign": -1, "energy": -1.0 - 0.1 * 0.01, "spin": (0.76, 0.75)}
    assert check_fd_points(-1.0, g0, (0.76, 0.75), [ok], 0.01) == []
    low = dict(ok, energy=-1.0 - 0.1 * 0.01 - 2e-5)
    assert "below the reference" in check_fd_points(-1.0, g0, None, [low], 0.01)[0]
    spun = dict(ok, spin=(0.82, 0.75))
    assert "<S**2>" in check_fd_points(-1.0, g0, (0.76, 0.75), [spun], 0.01)[0]
    # closed shells: nothing to compare
    assert check_fd_points(-1.0, g0, (None, None), [spun], 0.01) == []


def test_check_hessian_with_gradients_from_the_reference():
    engine = GuessEngine()
    session = session_of(engine, X0)
    # the toy K puts ~6,500 cm-1 on hydrogen: lift the frequency limit
    result = session.check_hessian(K, np.random.default_rng(1), max_frequency=1e5)
    assert not result["refused"] and max(result["errors"]) < 1e-6
    assert result["reference guess"] is True and result["tight SCF"] is True
    assert engine.restores == 6  # 3 displacements, 2 gradients each
    wrong = session.check_hessian(1.2 * K, np.random.default_rng(1), max_frequency=1e5)
    assert wrong["refused"] and "disagrees with the gradients" in wrong["reason"]


def test_check_hessian_in_a_batch_is_the_same():
    def gradient(x):
        return K @ (np.asarray(x).reshape(-1) / BOHR)

    masses = np.full(5, 12.0)
    one = check_hessian(K, gradient, X0, masses, np.random.default_rng(3))
    batch = check_hessian(
        K,
        None,
        X0,
        masses,
        np.random.default_rng(3),
        batch=lambda xs: [gradient(x) for x in xs],
    )
    assert one["errors"] == batch["errors"]


def test_zero_modes_of_a_diatomic():
    """A harmonic diatomic along x: 5 zero modes (linear), one stretch."""
    k = 0.5  # hartree/bohr^2
    H = np.zeros((6, 6))
    H[0, 0] = H[3, 3] = k
    H[0, 3] = H[3, 0] = -k
    masses = [1.008, 1.008]
    xyz = [[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]]
    zero = zero_modes(H, masses, xyz)
    assert len(zero) == 5 and max(zero) < 1e-6
    nu = frequencies(H, masses)
    assert nu.max() == pytest.approx(np.sqrt(2 * k / 1.008) * 5140.4871, rel=1e-6)


class FreshEngine(GuessEngine):
    """A GuessEngine that is a context manager and dies once, on a chosen
    forces call, as an MDI engine whose process stops."""

    started = []
    die_on = None

    def __enter__(self):
        FreshEngine.started.append(self)
        return self

    def __exit__(self, *args):
        return False

    def forces(self, units="hartree/bohr"):
        if FreshEngine.die_on is not None and len(FreshEngine.started) == 1:
            if self.forces_calls + 1 == FreshEngine.die_on:
                self.forces_calls += 1
                raise RuntimeError("MDI Error: MDI_Recv failed")
        return super().forces(units)


def test_a_restarted_engine_gets_the_tight_scf_and_the_guess_again():
    FreshEngine.started = []
    FreshEngine.die_on = 3
    engine = _RestartingEngine(FreshEngine, restarts=1)
    engine.set_coordinates(X0, units="Å")
    assert engine.tight_scf(True)
    engine.forces()
    assert engine.save_guess()
    engine.set_coordinates(X0 + 0.01, units="Å")
    engine.restore_guess()
    engine.forces()  # second call: fine
    engine.set_coordinates(X0 - 0.01, units="Å")
    engine.forces()  # third: dies; a new engine is made and asked again
    new = FreshEngine.started[-1]
    assert len(FreshEngine.started) == 2
    assert new.tight is True
    assert new.saved is not None
    assert np.allclose(new.saved, X0.reshape(-1) / BOHR)  # the reference's SCF


def test_the_tasks_route_is_tight_and_checked():
    """Finite differences as tasks: tight, each point's <S**2> checked, and the
    check's gradients as tasks too."""
    from seamm_util import Q_

    class Evaluator:
        def __init__(self, odd=None):
            self.submitted = {}
            self.options = []
            self.odd = odd

        def submit(self, geometry, key, options=None):
            self.submitted[key] = geometry
            self.options.append(options)

        def results(self):
            submitted, self.submitted = self.submitted, {}
            for key, geometry in submitted.items():
                xyz = geometry.atoms.get_coordinates(as_array=True)
                x = np.asarray(xyz).reshape(-1) / BOHR
                s2 = 0.95 if key == self.odd else 0.76
                yield types.SimpleNamespace(
                    ok=True,
                    key=key,
                    energy=float(0.5 * x @ K @ x) * 2625.4996394799,
                    gradients=(K @ x).reshape(5, 3)
                    * Q_(1.0, "hartree/bohr").m_as("kJ/mol/Å"),
                    data={"spin": {"S**2": s2, "S(S+1)": 0.75}},
                )

    configuration = Configuration()
    configuration.spin_multiplicity = 2
    # heavy atoms, so the toy K has realistic frequencies (the check's 5000 cm-1)
    configuration.atoms.atomic_masses = [12.0] * 5
    driver = HessianDriver(make_node(MC))
    evaluator = Evaluator()
    E0, H, how = driver.fd_hessian_tasks(
        evaluator, configuration, X0, delta_bohr=0.01, check=True
    )
    assert how == "finite differences as tasks"
    assert np.allclose(H, K, atol=1e-6)
    assert all(o == {"precision": "tight"} for o in evaluator.options)
    assert driver.fd_info["tight SCF"] is True and driver.fd_info["problems"] == []
    assert not driver.last_check["refused"]
    # a point in another state
    evaluator = Evaluator(odd="c1-fd0004p")
    with pytest.raises(HessianRefused, match="<S\\*\\*2>"):
        driver.fd_hessian_tasks(evaluator, configuration, X0, delta_bohr=0.01)
