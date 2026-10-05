# -*- coding: utf-8 -*-

"""Helpers for testing a code step's run path end to end.

A test builds a small flowchart from a spec, as ``seamm-flowchart build`` does, and
runs it with ``run_flowchart`` in a subprocess, the way a job runs. The code is
either a *fake* -- a script that writes a real run's recorded output -- so that the
whole path runs in CI where the code is not installed, or the *real* program, found
as SEAMM finds it, with the test skipped where it is not installed::

    from seamm_exec.testing import fake_program, find_program, run_spec, table_value

    SPEC = '''
    title: MOPAC energy
    steps:
    - Water: {}
    - MOPAC:
        steps:
        - Energy
    '''

    def test_with_a_fake(tmp_path):
        code = fake_program(tmp_path / "bin" / "mopac", files=DATA.glob("mopac.*"))
        job = run_spec(tmp_path, SPEC, inis={"mopac": {"code": code}})
        assert table_value(job / "job.out", "Enthalpy of Formation") == ...

Each run has its own ``HOME`` and ``SEAMM_ROOT`` under the test's directory, so the
user's ``~/.seamm.d`` (timing files, ``seammrc``) and ``<root>/<code>.ini`` are
never read or written, and the step's ``<code>.ini`` is the one the test writes.

``Water`` is a tiny step provided by :func:`run_spec` (on ``PYTHONPATH``) that
makes a water molecule at a fixed geometry, so that a code step's test needs no
structure step.

None of this imports pytest, so the module is importable anywhere; failures raise
``AssertionError`` with the end of the run's output.
"""

import configparser
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

__all__ = [
    "WATER_STEP",
    "fake_program",
    "find_program",
    "install_water_step",
    "run_spec",
    "table_value",
]

BUILD = "import sys; from seamm.flowchart_cli import main; sys.exit(main())"
RUN = "import sys; from seamm_exec import run; sys.argv[0] = 'run_flowchart'; run()"

#: Source of the ``Water`` step: water at a fixed geometry (O at the origin).
WATER_STEP = textwrap.dedent('''
    """A step for testing: water at a fixed geometry."""

    import seamm


    class Water(seamm.Node):
        def __init__(self, flowchart=None, extension=None):
            super().__init__(flowchart=flowchart, title="Water", extension=extension)

        @property
        def version(self):
            return "0.1"

        def description_text(self, P=None):
            return self.header + "\\n    Water at a fixed geometry."

        def run(self):
            next_node = super().run(None)
            db = self.get_variable("_system_db")
            system = db.create_system(name="water")
            configuration = system.create_configuration(name="water")
            configuration.atoms.append(
                x=[0.0, 0.757, -0.757],
                y=[0.0, 0.586, 0.586],
                z=[0.0, 0.0, 0.0],
                symbol=["O", "H", "H"],
            )
            db.system = system
            return next_node


    class WaterStep:
        my_description = {
            "description": "Water for tests",
            "group": "Building",
            "name": "Water",
        }

        def __init__(self, flowchart=None, gui=None):
            pass

        def description(self):
            return WaterStep.my_description

        def create_node(self, flowchart=None, **kwargs):
            return Water(flowchart=flowchart, **kwargs)

        def create_tk_node(self, canvas=None, **kwargs):
            raise NotImplementedError("no GUI for the test step")
''')

# A fake program: checks the input exists and is not empty, copies the recorded
# files beside it and prints the recorded standard output, as the program would.
FAKE_PROGRAM = textwrap.dedent("""\
    #!{python}
    import shutil
    import sys
    from pathlib import Path

    FILES = {files!r}
    STDOUT = {stdout!r}
    inputs = [a for a in sys.argv[1:] if Path(a).is_file()]
    if {need_input!r} and (not inputs or Path(inputs[0]).read_text().strip() == ""):
        sys.exit("fake program: no input")
    where = Path(inputs[0]).parent if inputs else Path.cwd()
    for source in FILES:
        shutil.copy(source, where / Path(source).name)
    if STDOUT is not None:
        sys.stdout.write(Path(STDOUT).read_text())
    """)


def install_water_step(site):
    """Write the ``Water`` step and its entry points into the directory ``site``,
    to be put on ``PYTHONPATH``. Returns ``site``."""
    site = Path(site)
    info = site / "seamm_test_water-0.1.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (site / "seamm_test_water.py").write_text(WATER_STEP)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: seamm-test-water\nVersion: 0.1\n"
    )
    (info / "entry_points.txt").write_text(
        "[org.molssi.seamm]\nWater = seamm_test_water:WaterStep\n\n"
        "[org.molssi.seamm.tk]\nWater = seamm_test_water:WaterStep\n"
    )
    return site


def fake_program(path, files=(), stdout=None, need_input=True):
    """Write an executable script at ``path`` that stands in for a program.

    Parameters
    ----------
    path : str or Path
        Where to write it (its directory is created).
    files : iterable of paths
        Recorded output files, copied beside the input (the first argument that is
        an existing file), or into the working directory without one.
    stdout : path, optional
        A recorded file to print as the program's standard output, for codes whose
        output the command redirects (``{code} orca.inp > orca.out``).
    need_input : bool
        Fail, as the program would, if the input file is missing or empty.

    Returns
    -------
    str
        The script's path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        FAKE_PROGRAM.format(
            python=sys.executable,
            files=[str(Path(f).resolve()) for f in files],
            stdout=None if stdout is None else str(Path(stdout).resolve()),
            need_input=need_input,
        )
    )
    path.chmod(0o755)
    return str(path)


def find_program(program, ini=None, section="local", root=None):
    """The installed executable for ``program``, or None, as SEAMM finds it.

    In order: ``$<PROGRAM>_EXE``; ``program`` on the PATH; the ``code`` named in
    the installation's ``<ini>.ini`` ``[section]`` (an absolute path, or the
    executable in its ``conda-environment``). The installation's root is ``root``,
    else ``$SEAMM_ROOT``, else ``~/SEAMM``.
    """
    path = os.environ.get(f"{program.upper()}_EXE") or shutil.which(program)
    if path:
        return path
    if root is None:
        root = os.environ.get("SEAMM_ROOT", "").strip() or "~/SEAMM"
    config = configparser.ConfigParser()
    try:
        config.read(Path(root).expanduser() / f"{ini or program}.ini")
        local = config[section]
    except (configparser.Error, KeyError):
        return None
    code = local.get("code", "").strip()
    code = code.split()[0] if code else ""
    candidates = [Path(code)] if code else []
    if local.get("conda") and local.get("conda-environment"):
        prefix = Path(local["conda"]).parents[1] / "envs" / local["conda-environment"]
        candidates.append(prefix / "bin" / (Path(code).name if code else program))
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)
    return None


def run_spec(
    tmp_path, spec, inis=None, args=(), source=None, extra_path=(), timeout=600
):
    """Build the flowchart ``spec`` and run it; return the job's directory.

    Parameters
    ----------
    tmp_path : str or Path
        A fresh directory for the run: ``job/`` (the job), ``root/`` (its
        ``SEAMM_ROOT``, holding the ini files), ``home/`` (its ``HOME``) and
        ``site/`` (the ``Water`` step).
    spec : str
        The flowchart spec, YAML as ``seamm-flowchart build`` takes it.
    inis : {str: {str: str}}, optional
        ``<code>.ini`` files to write in the root, each as the values of its
        ``[local]`` section, e.g. ``{"orca": {"code": "/path/to/orca"}}``
        (``installation = local`` is added unless given).
    args : [str]
        More arguments for ``run_flowchart``, after the flowchart, e.g.
        ``["--ncores", "1"]``.
    source : str or Path, optional
        A package's source directory to put first on ``PYTHONPATH``, so that the
        code under test is the one run.
    extra_path : [str or Path]
        More directories for ``PYTHONPATH``.
    timeout : float
        Seconds allowed for each of the build and the run.
    """
    tmp_path = Path(tmp_path)
    job, root, home = tmp_path / "job", tmp_path / "root", tmp_path / "home"
    for directory in (job, root, home):
        directory.mkdir(parents=True)
    site = install_water_step(tmp_path / "site")
    for name, values in (inis or {}).items():
        values = {"installation": "local", **{k: str(v) for k, v in values.items()}}
        text = "[local]\n" + "".join(f"{k} = {v}\n" for k, v in values.items())
        (root / f"{name}.ini").write_text(text)
    (job / "spec.yaml").write_text(textwrap.dedent(spec))

    env = {k: v for k, v in os.environ.items() if not k.startswith("SEAMM_")}
    env.pop("OMP_NUM_THREADS", None)
    env["HOME"] = str(home)
    env["SEAMM_ROOT"] = str(root)
    paths = [*([source] if source else []), site, *extra_path]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(p) for p in paths] + list(filter(None, [os.environ.get("PYTHONPATH")]))
    )
    for command in (
        ["-c", BUILD, "build", "spec.yaml", "-o", "test.flow"],
        ["-c", RUN, "test.flow", *args],
    ):
        result = subprocess.run(
            [sys.executable, *command],
            cwd=job,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            tail = ""
            if (job / "job.out").exists():
                tail = (job / "job.out").read_text()[-3000:]
            raise AssertionError(
                f"{' '.join(command[2:])} failed ({result.returncode}):\n"
                + result.stdout[-3000:]
                + result.stderr[-3000:]
                + tail
            )
    return job


def table_value(text_or_path, label):
    """The number in the last row of a results table whose label is ``label``.

    Steps print their results as tables, ``| label | value | units |`` (with
    ``|`` or the box-drawing ``│``). ``text_or_path`` is the text or a file
    holding it, such as a job's ``job.out``.
    """
    text = text_or_path
    if isinstance(text_or_path, Path) or (
        isinstance(text_or_path, str) and "\n" not in text_or_path
    ):
        text = Path(text_or_path).read_text()
    value = None
    for line in text.splitlines():
        cells = [c.strip() for c in line.replace("│", "|").split("|")]
        cells = [c for c in cells if c != ""]
        if len(cells) >= 2 and cells[0] == label:
            value = float(cells[1])
    if value is None:
        raise AssertionError(f"No '{label}' row in the results:\n{text[-3000:]}")
    return value
