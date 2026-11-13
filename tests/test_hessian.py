# -*- coding: utf-8 -*-

"""Tests of seamm_exec.hessian: the shared Hessian driver (moved from
thermochemistry_step 2026-11-05, with its tests)."""

import contextlib
import json
import os
import types

import numpy as np
import pytest

import seamm_exec
import seamm_exec.hessian as H
from seamm_exec.hessian import EngineSession, HessianDriver


# --------------------------------------------------------------------------
# A fake step (node), configuration and engine
# --------------------------------------------------------------------------
class Configuration:
    """Methane-like, 5 atoms, neutral singlet."""

    def __init__(self):
        self.id = 1
        self.periodicity = 0
        self.charge = 0
        self.spin_multiplicity = 1
        self.n_atoms = 5
        xyz = np.array(
            [
                [0.0, 0.0, 0.0],
                [0.63, 0.63, 0.63],
                [-0.63, -0.63, 0.63],
                [-0.63, 0.63, -0.63],
                [0.63, -0.63, -0.63],
            ]
        )
        self.atoms = types.SimpleNamespace(
            atomic_numbers=[6, 1, 1, 1, 1],
            get_coordinates=lambda fractionals=False, as_array=True: xyz.copy(),
            set_coordinates=lambda x, fractionals=False: None,
        )


def make_node(mc=None, provider=None):
    variables = {} if mc is None else {"_model_chemistry": mc}
    return types.SimpleNamespace(
        indent="",
        title="Test",
        logger=None,
        global_options={"ncores": "available"},
        variable_exists=lambda name: name in variables,
        get_variable=lambda name: variables[name],
        flowchart=types.SimpleNamespace(
            plugin_manager=types.SimpleNamespace(get=lambda name: provider),
            executor=None,
        ),
    )


class Provider:
    def get_task(self):  # has a task route
        pass

    def analyze_task(self):
        pass

    def get_mdi_engine_command(self, *a, **k):
        return ["engine"]


class FakeEvaluator:
    def __init__(self, *a, path="batch", **k):
        self.provider = Provider()
        self.target = None
        self.path = path


MC = {
    "level": "ORCA:DFT@B2PLYP/cc-pVTZ",
    "step": "ORCA",
    "method": "B2PLYP",
    "options": {"mdi_capable": True, "analytic_hessian": False, "prefers_batch": True},
}

#: A harmonic potential E = 1/2 x.K.x (E_h, bohr) for the fake engine
K = np.diag(np.linspace(0.2, 1.6, 15))
K[0, 3] = K[3, 0] = 0.05
BOHR = 0.529177210903  # Å


class HarmonicEngine:
    """Forces and an optional analytic Hessian from E = 1/2 x.K.x."""

    def __init__(self, analytic=True):
        self._analytic = analytic
        self.x = np.zeros(15)
        self.n_set = 0

    def set_coordinates(self, x, units="Å"):
        x = np.asarray(x, dtype=float).reshape(-1)
        self.x = x / BOHR if units == "Å" else x
        self.n_set += 1

    def energy(self, units="kJ/mol"):
        e = 0.5 * self.x @ K @ self.x
        return e * 2625.4996394799 if units == "kJ/mol" else e

    def forces(self, units="hartree/bohr"):
        return -(K @ self.x)

    def supports(self, command):
        return self._analytic and command == "<HESSIAN"

    def hessian(self):
        return K.copy()

    def fingerprints(self):
        return {"energy": {"fingerprint": "sha256:x"}, "hessian": None}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def test_route_honours_the_choice(monkeypatch):
    """The engine when asked, even for a model that prefers batch and has no
    analytic Hessian; tasks when asked; automatic as before."""
    monkeypatch.setattr(seamm_exec, "Evaluator", FakeEvaluator)
    driver = HessianDriver(make_node(MC, Provider()))
    monkeypatch.setattr(driver, "code_is_here", lambda step: True)
    c = Configuration()
    driver.route = "automatic"
    assert driver.plan(c)[1] is None  # tasks: no analytic Hessian, batch path
    driver.route = "warm engine over MDI"
    assert driver.plan(c)[1] is not None  # the engine
    driver.route = "separate tasks"
    assert driver.plan(c)[1] is None


def test_messages_name_the_step():
    with pytest.raises(ValueError, match="Vibrational Analysis needs a Model Chem"):
        HessianDriver(make_node(), who="Vibrational Analysis").plan(Configuration())
    driver = HessianDriver(make_node(MC), who="Test", hint="or put it in a subflow")
    c = Configuration()
    c.periodicity = 3
    with pytest.raises(ValueError, match="periodic"):
        driver.plan(c)
    with pytest.raises(ValueError, match="before it to choose the method, or put"):
        HessianDriver(make_node(), hint="or put it in a subflow").plan(c)


def test_engine_route_keeps_fd_on_engine(monkeypatch):
    """With the warm-engine route the finite differences run over the engine
    even when the program prefers batch (tasks), and the fingerprints are
    captured; automatic goes to tasks."""
    import sys

    calls = {}

    class Eng(HarmonicEngine):
        def __init__(self, *a, **k):
            super().__init__(analytic=False)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # seamm_mdi is not a dependency of seamm_exec (it is imported only when an
    # engine starts), so stand in a module for it
    monkeypatch.setitem(sys.modules, "seamm_mdi", types.SimpleNamespace(MDIEngine=Eng))
    evaluator = FakeEvaluator()
    driver = HessianDriver(make_node(MC, Provider()))
    c = Configuration()
    monkeypatch.setattr(
        driver,
        "plan",
        lambda c: (evaluator, lambda h, p: ["x"], [6, 1, 1, 1, 1], np.zeros((5, 3))),
    )
    monkeypatch.setattr(driver, "engine_ncores", lambda: 1)
    monkeypatch.setattr(
        driver, "engine_environment", lambda n: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        driver,
        "fd_hessian_tasks",
        lambda *a, **k: calls.setdefault("tasks", (0, None, "tasks")),
    )
    driver.route = "warm engine over MDI"
    E0, Hess, how = driver.optimize_and_hessian(c, optimize=False)
    assert how == "finite differences over MDI" and "tasks" not in calls
    assert np.allclose(Hess, K, atol=1e-8)
    assert driver.fingerprints["energy"]["fingerprint"] == "sha256:x"
    driver.route = "automatic"
    E0, Hess, how = driver.optimize_and_hessian(c, optimize=False)
    assert how == "tasks" and "tasks" in calls


# --------------------------------------------------------------------------
# The engine session
# --------------------------------------------------------------------------
@pytest.mark.parametrize("analytic", [True, False])
def test_session_hessians(analytic):
    """Analytic or finite-difference Hessians agree with the potential, at the
    reference and at displaced geometries, and the engine is left at the
    reference."""
    driver = HessianDriver(make_node(MC))
    engine = HarmonicEngine(analytic=analytic)
    x0 = np.full((5, 3), 0.1)
    session = EngineSession(driver, engine, Configuration(), None, [6, 1, 1, 1, 1], x0)
    assert session.analytic is analytic
    H0, how = session.hessian()
    assert how == ("analytic" if analytic else "finite differences over MDI")
    assert np.allclose(H0, K, atol=1e-8)
    H1, _ = session.hessian(x0 + 0.05)
    assert np.allclose(H1, K, atol=1e-8)
    assert np.allclose(engine.x, x0.reshape(-1) / BOHR)
    # Gradient = K x
    g = session.gradient(x0 + 0.02)
    assert np.allclose(g, K @ ((x0 + 0.02).reshape(-1) / BOHR))
    if analytic:
        Hfd, how = session.hessian(finite_differences=True)
        assert how == "finite differences over MDI" and np.allclose(Hfd, K, atol=1e-8)


def test_evaluate_converts_units():
    """evaluate() returns kJ/mol and E_h/bohr gradients from an Evaluator."""
    from seamm_util import Q_

    class Evaluator:
        def __init__(self):
            self.submitted = {}

        def submit(self, geometry, key, options=None):
            self.submitted[key] = geometry
            self.options = options

        def results(self):
            for key, geometry in self.submitted.items():
                yield types.SimpleNamespace(
                    ok=True,
                    key=key,
                    energy=-10.0,
                    gradients=np.ones((5, 3))
                    * Q_(1.0, "hartree/bohr").m_as("kJ/mol/Å"),
                )

    driver = HessianDriver(make_node(MC))
    evaluator = Evaluator()
    out = driver.evaluate(evaluator, Configuration(), {"a": np.zeros((5, 3))})
    E, g = out["a"]
    assert E == -10.0 and np.allclose(g, 1.0) and g.shape == (15,)
    assert evaluator.options is None
    # The precision goes to the tasks as an option
    driver = HessianDriver(make_node(MC), precision="tight")
    evaluator = Evaluator()
    driver.evaluate(evaluator, Configuration(), {"a": np.zeros((5, 3))})
    assert evaluator.options == {"precision": "tight"}


# --------------------------------------------------------------------------
# Cores and the engine's environment
# --------------------------------------------------------------------------
def test_engine_ncores(monkeypatch):
    # No physical-core cap here: the test machine's topology is not the point
    monkeypatch.setattr(H, "physical_cores_allowed", lambda *a, **k: None)
    for variable in ("SLURM_CPUS_ON_NODE", "SLURM_NTASKS", "SEAMM_CE"):
        monkeypatch.delenv(variable, raising=False)
    n_here = (
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count()
    )
    node = make_node(MC)
    driver = HessianDriver(node)
    node.global_options = {"ncores": "available"}
    assert driver.engine_ncores() == max(1, n_here)
    node.global_options = {"ncores": "2"}
    assert driver.engine_ncores() == min(2, n_here)
    node.global_options = {"ncores": "nonsense"}
    assert driver.engine_ncores() == max(1, n_here)


def test_engine_ncores_uses_physical_cores(monkeypatch):
    """16 allowed hardware threads on 8 cores give 8 engine ranks."""
    monkeypatch.delenv("SEAMM_CE", raising=False)
    monkeypatch.setenv("SLURM_CPUS_ON_NODE", "16")
    monkeypatch.setattr(H, "physical_cores_allowed", lambda *a, **k: 8)
    assert HessianDriver(make_node(MC)).engine_ncores() == 8


def test_engine_ncores_in_a_parallel_iteration(monkeypatch):
    """An iteration of a parallel loop inherits the job's Slurm variables; its
    engine gets the iteration's share (SEAMM_CE), not the whole job (ChemAI
    5687: 6 iterations of 4 cores each ran 24 ranks in a 24-core job)."""
    monkeypatch.setattr(H, "physical_cores_allowed", lambda *a, **k: 24)
    monkeypatch.setenv("SLURM_CPUS_ON_NODE", "24")
    monkeypatch.setenv("SLURM_NTASKS", "24")
    monkeypatch.setenv(
        "SEAMM_CE",
        json.dumps(
            {"type": "slurm", "NTASKS": 4, "CPUS_PER_TASK": 1, "CPUS_ON_NODE": 24}
        ),
    )
    node = make_node(MC)
    node.global_options = {"ncores": "available"}
    assert HessianDriver(node).engine_ncores() == 4
    # A threaded share counts its threads
    monkeypatch.setenv("SEAMM_CE", json.dumps({"NTASKS": 1, "CPUS_PER_TASK": 8}))
    assert HessianDriver(node).engine_ncores() == 8
    # Unreadable: back to the job's allocation
    monkeypatch.setenv("SEAMM_CE", "not json")
    assert HessianDriver(node).engine_ncores() == 24
    # Outside a loop: the job's allocation, as before
    monkeypatch.delenv("SEAMM_CE")
    assert HessianDriver(node).engine_ncores() == 24


def test_engine_environment(tmp_path, monkeypatch):
    """The engine gets the program's environment (ORCA: its OpenMPI runtime
    with its mpirun first on the PATH, no core binding), restored after."""
    driver = HessianDriver(make_node(MC))

    # A step with its own hook
    class Hooked:
        __module__ = "other_step"

        @staticmethod
        def mdi_engine_environment(executor, options, ncores):
            return {"MY_VAR": str(ncores)}

    driver.step = Hooked()
    monkeypatch.delenv("OMPI_MCA_hwloc_base_binding_policy", raising=False)
    before = dict(os.environ)
    with driver.engine_environment(4) as env:
        assert os.environ["MY_VAR"] == "4"
        assert env["OMPI_MCA_hwloc_base_binding_policy"] == "none"
    assert dict(os.environ) == before

    pytest.importorskip("orca_step")  # the ORCA branch needs its mpi_env
    lib = tmp_path / "orca-mpi" / "lib"
    lib.mkdir(parents=True)
    (tmp_path / "orca-mpi" / "bin").mkdir()

    class FakeORCA:
        __module__ = "orca_step.fake"

        @staticmethod
        def get_executor_config(executor, options):
            return {"library-path": str(lib)}

    driver.step = FakeORCA()
    with driver.engine_environment(16) as env:
        assert os.environ["OMPI_MCA_hwloc_base_binding_policy"] == "none"
        assert os.environ["LD_LIBRARY_PATH"].startswith(str(lib))
        assert os.environ["PATH"].startswith(str(tmp_path / "orca-mpi" / "bin"))
        assert env["OMPI_MCA_pml"] == "ob1"
    assert dict(os.environ) == before


def _fake_module_shell(tmp_path, body='export SEAMM_TEST_MODULE="$2"'):
    """A 'shell' whose module function sets a variable, standing in for Lmod."""
    shell = tmp_path / f"fake_shell_{abs(hash(body))}"
    shell.write_text("#!/bin/bash\n" f"module() {{ {body}; }}\n" 'eval "$2"\n')
    shell.chmod(0o755)
    return str(shell)


def test_engine_environment_loads_modules(tmp_path, monkeypatch):
    """The engine of a module-installed program gets the module's environment
    (ORCA on TinkerCliffs could not load its libraries without it), restored
    afterwards."""
    shell = _fake_module_shell(tmp_path)

    class FakeModuleProgram:
        __module__ = "other_step"

        @staticmethod
        def get_executor_config(executor, options):
            return {"installation": "modules", "modules": "ORCA", "shell": shell}

    driver = HessianDriver(make_node(MC))
    driver.step = FakeModuleProgram()
    monkeypatch.delenv("SEAMM_TEST_MODULE", raising=False)
    with driver.engine_environment(4) as env:
        assert os.environ["SEAMM_TEST_MODULE"] == "ORCA"
        assert env["SEAMM_TEST_MODULE"] == "ORCA"
    assert "SEAMM_TEST_MODULE" not in os.environ


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def test_slurm_allocated_cpus():
    from seamm_exec.hessian import parse_cpu_ids, slurm_allocated_cpus

    assert parse_cpu_ids("CPU_IDs=16-31") == set(range(16, 32))
    assert parse_cpu_ids("0-3,8,10-11") == {0, 1, 2, 3, 8, 10, 11}
    text = (
        "JobId=13023 JobName=seamm\n"
        "     Nodes=ChemAI CPU_IDs=16-31 Mem=49152 GRES=\n"
    )
    assert slurm_allocated_cpus({"SLURM_JOB_ID": "13023"}, run=lambda j: text) == set(
        range(16, 32)
    )
    assert slurm_allocated_cpus({}, run=lambda j: text) is None
    assert slurm_allocated_cpus({"SLURM_JOB_ID": "1"}, run=lambda j: "no ids") is None


def test_physical_cores_allowed(tmp_path):
    from seamm_exec.hessian import physical_cores_allowed

    # 4 CPUs on 2 cores (hyperthread siblings 0/2 and 1/3)
    for cpu, core in ((0, 0), (1, 1), (2, 0), (3, 1)):
        d = tmp_path / f"cpu{cpu}" / "topology"
        d.mkdir(parents=True)
        (d / "core_id").write_text(f"{core}\n")
        (d / "physical_package_id").write_text("0\n")
    assert physical_cores_allowed({0, 1, 2, 3}, sysfs=str(tmp_path)) == 2
    assert physical_cores_allowed({0, 1}, sysfs=str(tmp_path)) == 2
    assert physical_cores_allowed({0, 2}, sysfs=str(tmp_path)) == 1
    assert physical_cores_allowed({7}, sysfs=str(tmp_path)) is None


def test_ncores_passed_only_when_accepted():
    from seamm_exec.hessian import _accepts_keyword

    def orca(executor, options, method, port, hostname, charge, multiplicity,
             n_atoms, ncores=1):  # fmt: skip
        pass

    def mopac(executor, options, method, port, hostname, charge, multiplicity,
              n_atoms, extra_args=None):  # fmt: skip
        pass

    def anything(executor, options, **kwargs):
        pass

    assert _accepts_keyword(orca, "ncores")
    assert not _accepts_keyword(mopac, "ncores")
    assert _accepts_keyword(anything, "ncores")


def test_module_environment(tmp_path):
    """A module installation's environment: the variables `module load`
    changes, by Local.exec's module rule; a clear error when it fails."""
    from seamm_exec.hessian import module_environment, modules_to_load

    assert module_environment({"installation": "local", "code": "orca"}) == {}
    assert module_environment({"installation": "modules", "modules": ""}) == {}
    config = {
        "installation": "modules",
        "modules": " ORCA ",
        "gpu_modules": "ORCA-gpu",
        "shell": _fake_module_shell(tmp_path),
    }
    assert modules_to_load(config) == "ORCA"
    assert modules_to_load(config, {"NGPUS": 1}) == "ORCA-gpu"
    assert modules_to_load({**config, "gpu_modules": ""}, {"NGPUS": 1}) == "ORCA"
    env = module_environment(config, environ={"PATH": "/usr/bin:/bin", "KEEP": "1"})
    assert env == {"SEAMM_TEST_MODULE": "ORCA"}
    env = module_environment(config, {"NGPUS": 2}, environ={"PATH": "/usr/bin:/bin"})
    assert env == {"SEAMM_TEST_MODULE": "ORCA-gpu"}
    # A failing load names the module
    config["shell"] = _fake_module_shell(
        tmp_path, body="echo 'Lmod has detected the following error' >&2; return 1"
    )
    with pytest.raises(RuntimeError, match=r"'module load ORCA' failed.*Lmod"):
        module_environment(config)


def test_precision_only_to_engines_that_declare_it(monkeypatch):
    """precision="tight" goes to an engine whose command names it, never
    through **kwargs; otherwise the user is told."""
    monkeypatch.setattr(
        seamm_exec, "Evaluator", lambda *a, **k: FakeEvaluator(path="mdi")
    )
    said = []

    class Tight(Provider):
        def get_mdi_engine_command(self, *a, precision=None, **k):
            return ["engine", str(precision)]

    class Loose(Provider):
        def get_mdi_engine_command(self, *a, **k):
            assert "precision" not in k
            return ["engine"]

    mc = {**MC, "options": {"mdi_capable": True}}
    for provider, expected in ((Tight(), ["engine", "tight"]), (Loose(), ["engine"])):
        driver = HessianDriver(make_node(mc, provider), precision="tight")
        monkeypatch.setattr(driver, "code_is_here", lambda step: True)
        monkeypatch.setattr(driver, "say", said.append)
        build_argv = driver.plan(Configuration())[1]
        assert build_argv("localhost", 1) == expected
    assert len(said) == 1 and "cannot be asked for tight convergence" in said[0]


# --------------------------------------------------------------------------
# An engine that dies is started again
# --------------------------------------------------------------------------
class DyingEngine:
    """An engine whose process 'dies' (raises like a broken MDI connection) on
    the calls listed in `deaths`, counted over all engines."""

    started = []
    calls = 0
    deaths = ()

    def __init__(self):
        self.x = None
        self.closed = False

    def __enter__(self):
        DyingEngine.started.append(self)
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def set_coordinates(self, xyz, units=None):
        self.x = (np.asarray(xyz).copy(), units)

    def energy(self, units=None):
        DyingEngine.calls += 1
        if DyingEngine.calls in DyingEngine.deaths:
            raise RuntimeError("MDI Error: MDI_Recv failed")
        assert self.x is not None, "a new engine must be given the coordinates"
        return float(np.sum(self.x[0]))

    def supports(self, command):
        return False


@pytest.fixture()
def dying():
    DyingEngine.started = []
    DyingEngine.calls = 0
    DyingEngine.deaths = ()
    return DyingEngine


def test_a_dead_engine_is_restarted(dying):
    from seamm_exec.hessian import _RestartingEngine

    said = []
    dying.deaths = (2,)
    engine = _RestartingEngine(dying, restarts=2, say=said.append)
    engine.set_coordinates([[1.0, 2.0, 3.0]], units="Å")
    assert engine.energy() == 6.0
    engine.set_coordinates([[1.0, 1.0, 1.0]], units="Å")
    # The engine dies; a new one gets the last coordinates and answers
    assert engine.energy(units="eV") == 3.0
    assert len(dying.started) == 2 and dying.started[0].closed
    assert dying.started[1].x[1] == "Å"
    assert engine.n_restarts == 1 and "started again" in said[0]
    # Other calls pass straight through
    assert engine.supports("<HESSIAN") is False
    engine.close()
    assert dying.started[1].closed


def test_restarts_are_limited(dying):
    from seamm_exec.hessian import _RestartingEngine

    dying.deaths = (1, 2, 3)
    engine = _RestartingEngine(dying, restarts=2)
    engine.set_coordinates([[0.0, 0.0, 1.0]])
    with pytest.raises(RuntimeError, match="MDI_Recv failed"):
        engine.energy()
    assert engine.n_restarts == 2 and len(dying.started) == 3


def test_the_session_restarts_its_engine(monkeypatch, dying):
    """An engine that dies in an optimization's session is replaced, and the
    session carries on."""
    import sys

    monkeypatch.setitem(
        sys.modules,
        "seamm_mdi",
        types.SimpleNamespace(MDIEngine=lambda *a, **k: dying()),
    )
    driver = HessianDriver(make_node(MC, Provider()))
    monkeypatch.setattr(
        driver,
        "plan",
        lambda c: (None, lambda h, p: ["x"], [1], np.zeros((1, 3))),
    )
    monkeypatch.setattr(driver, "engine_ncores", lambda: 1)
    monkeypatch.setattr(
        driver, "engine_environment", lambda n: contextlib.nullcontext()
    )
    dying.deaths = (1,)
    with driver.session(Configuration(), announce=False) as session:
        assert session.energy([[0.5, 0.5, 0.5]], units="hartree") == 1.5
    assert len(dying.started) == 2 and all(e.closed for e in dying.started)
