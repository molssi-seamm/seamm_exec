# -*- coding: utf-8 -*-

"""One iteration of a parallel Loop, run by an evaluator of its own (phase 6).

The parent's Loop sets an iteration up, then writes, in the iteration's
``_evaluator/`` directory, a snapshot of the job database holding the checkpoint
that resumes the flowchart into that iteration only. It runs the iteration as a
:class:`~seamm_exec.tasks.Task` of the program ``seamm``, whose resolver here
finds ``run_flowchart`` on the machine that runs it. The child keeps its step
directories in the parent's tree and its own files (database, output,
job-level files) in ``_evaluator/``.

When an iteration is done the parent merges what it did, in iteration order: the
database (:func:`molsystem.snapshot.merge`, in the parent's transaction), then,
after that transaction is committed, its job-level files and its citations
(:func:`plan_files`, :func:`merge_files`, :func:`merge_citations`).

See ``docs/developer_guide/campaigns/2026-10-02/NOTES_phase6.rst``.
"""

import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys

from .tasks import Resources, Task

logger = logging.getLogger("seamm-exec")

#: The program of an iteration's task, and its resolver's name
PROGRAM = "seamm"
#: Where an iteration's evaluator keeps its own files, in the iteration directory
EVALUATOR_DIRECTORY = "_evaluator"
#: The parent job's directory, relative to the child's, which puts an evaluator
#: into child mode
PARENT_JOB_ENVIRONMENT = "SEAMM_PARENT_JOB"
#: The child's share of the machine, as JSON (see computational_environment)
CE_ENVIRONMENT = "SEAMM_CE"
#: Write Structure's record of the files it appended to, in its step directory
APPENDED_RECORD = "appended_files.json"

# The evaluator's own files, never merged into the parent's job directory
OWN_FILES = {
    "seamm.db",
    "seamm.db-wal",
    "seamm.db-shm",
    "seamm.db-journal",
    "baseline.db",
    "job.out",
    "job_data.json",
    "checkpoint.json",
    "references.db",
    "flowchart.flow",
    "target.json",
    "final_structure.mmcif",
    "final_structure.cif",
    "stdout.txt",
    "stderr.txt",
}
OWN_DIRECTORIES = {"previous"}


# ----------------------------------------------------------------------------
# Running an iteration
# ----------------------------------------------------------------------------


def run_flowchart_path():
    """``run_flowchart`` of this Python, else the one on the PATH."""
    here = Path(sys.executable).parent / "run_flowchart"
    if here.exists():
        return str(here)
    found = shutil.which("run_flowchart")
    if found is None:
        raise RuntimeError(
            "Could not find run_flowchart to run an iteration of a parallel loop: "
            f"it is not beside {sys.executable} nor on the PATH."
        )
    return found


def resolve(config, cmd, env, ce, root):
    """The resolver of the program ``seamm``: an evaluator for one iteration.

    ``code`` is this machine's ``run_flowchart`` unless ``<root>/seamm.ini``
    names one. The child's share of the machine goes in its environment, so its
    own pool runs its codes within that share.
    """
    config = dict(config)
    if not (config.get("code") or "").strip():
        config = {"installation": "local", **config, "code": run_flowchart_path()}
    env = dict(env)
    share = {}
    for key, value in ce.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        share[key] = value
    env[CE_ENVIRONMENT] = json.dumps(share)
    return config, list(cmd), env


def _available(root):
    try:
        run_flowchart_path()
    except RuntimeError:
        return False
    return True


resolve.available = _available


def iteration_task(
    key,
    evaluator,
    job_directory,
    command_line=(),
    cores=1,
    memory=None,
    walltime=None,
    placement="inline",
    root_directory=None,
):
    """The task that runs one iteration in its ``_evaluator`` directory.

    Parameters
    ----------
    key : str
        The task's key, unique in the Loop's task set.
    evaluator : str or Path
        The iteration's ``_evaluator`` directory, holding its snapshot.
    job_directory : str or Path
        The parent evaluator's job directory, holding ``flowchart.flow`` (and
        ``target.json``).
    command_line : [str]
        The parent's command-line arguments (as in its checkpoint).
    cores : int
        Cores for the iteration.
    memory : int, optional
        Memory for the iteration, in bytes.
    walltime : float, optional
        Seconds.
    placement : str
        "inline": the iteration's codes run in its own share of the machine;
        "separate": they go to the job's target like any job's.
    root_directory : str or Path, optional
        Where the steps' directories are, by default ``job_directory``: the
        job's, also for a parallel loop nested in an iteration of another.
    """
    evaluator = Path(evaluator)
    job_directory = Path(job_directory)
    if root_directory is None:
        root_directory = job_directory
    parent = os.path.relpath(root_directory, evaluator)
    flowchart = os.path.relpath(job_directory / "flowchart.flow", evaluator)
    # Run by the shell, then formatted with the configuration: quote the
    # arguments and keep any braces in them
    args = [
        shlex.quote(str(a)).replace("{", "{{").replace("}", "}}")
        for a in (flowchart, *command_line)
    ]
    cmd = ["{code}", *args, ">", "stdout.txt", "2>", "stderr.txt"]
    env = {"SEAMM_RESUME": "1", PARENT_JOB_ENVIRONMENT: parent}
    if placement == "inline":
        # No target: the child's codes run in its own pool, within its share
        env["SEAMM_TARGET"] = ""
    else:
        target = job_directory / "target.json"
        if target.exists():
            shutil.copy2(target, evaluator / "target.json")
    cores = max(1, int(cores))
    mem_per_cpu = None if memory is None else max(1, int(memory) // cores)
    return Task(
        key=key,
        program=PROGRAM,
        cmd=cmd,
        env=env,
        directory=evaluator,
        in_situ=True,
        shell=True,
        keep=["."],
        resources=Resources(
            ntasks=cores, cpus_per_task=1, mem_per_cpu=mem_per_cpu, walltime=walltime
        ),
    )


def iteration_outcome(evaluator):
    """How an iteration ended, from its final checkpoint, or None if it did not.

    Returns
    -------
    dict or None
        ``{"done": True, "break": bool, "skip": bool}`` and ``run_id``.
    """
    import seamm

    path = Path(evaluator) / "seamm.db"
    if not path.exists():
        return None
    checkpoint = seamm.read_checkpoint(path)
    if checkpoint is None or checkpoint.get("state") != "finished":
        return None
    outcome = checkpoint.get("iteration")
    if outcome is None:
        return None
    return dict(
        outcome, run_id=checkpoint.get("run_id"), system_id=checkpoint.get("system_id")
    )


def encode_merge_state(state):
    """The merge's state between iterations (tuple keys), for the checkpoint."""
    return {
        "touched": [
            [list(key), value] for key, value in state.get("touched", {}).items()
        ]
    }


def decode_merge_state(data):
    """The inverse of :func:`encode_merge_state`."""
    if not data:
        return {}
    return {"touched": {tuple(key): value for key, value in data.get("touched", [])}}


# ----------------------------------------------------------------------------
# Merging an iteration
# ----------------------------------------------------------------------------


def merge_database(system_db, evaluator, state, iteration, later_wins=False):
    """Merge the iteration's database into the job's, in its open transaction.

    See :func:`molsystem.snapshot.merge`; the result also has ``exports``, the
    tables the iteration exported, ``{name: filename in the parent's job}``.
    """
    from molsystem.snapshot import merge

    evaluator = Path(evaluator)
    result = merge(
        system_db,
        evaluator / "seamm.db",
        evaluator / "baseline.db",
        state,
        iteration=iteration,
        later_wins=later_wins,
    )
    result["exports"] = {}
    if len(result.get("exported", [])) > 0:
        db = sqlite3.connect(f"file:{evaluator / 'seamm.db'}?mode=ro", uri=True)
        try:
            for name in result["exported"]:
                row = db.execute(
                    "SELECT metadata FROM _tables WHERE name = ?", (name,)
                ).fetchone()
                try:
                    filename = json.loads(row[0]).get("filename")
                except (TypeError, ValueError, AttributeError):
                    filename = None
                if filename:
                    result["exports"][name] = filename
        finally:
            db.close()
    return result


def _appended_starts(iteration_directory, evaluator, run_id):
    """Where the iteration started appending to each of its job-level files.

    From the records Write Structure keeps in its step directories: ``{path
    relative to the evaluator: offset}``.
    """
    evaluator = Path(evaluator).resolve()
    starts = {}
    for record in Path(iteration_directory).rglob(APPENDED_RECORD):
        try:
            saved = json.loads(record.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(saved, dict) or saved.get("run") != run_id:
            continue
        for name, size in saved.get("sizes", {}).items():
            path = Path(name).resolve()
            try:
                relative = path.relative_to(evaluator)
            except ValueError:
                continue
            offset = max(int(size), 0)
            key = str(relative)
            starts[key] = min(offset, starts.get(key, offset))
    return starts


def _job_files(evaluator):
    """The job-level files an iteration wrote, relative to its evaluator."""
    evaluator = Path(evaluator)
    files = []
    for path in sorted(evaluator.rglob("*")):
        relative = path.relative_to(evaluator)
        if relative.parts[0] in OWN_DIRECTORIES:
            continue
        if len(relative.parts) == 1 and relative.name in OWN_FILES:
            continue
        if path.is_dir() or path.name.endswith(".tmp"):
            continue
        files.append(relative)
    return files


def plan_files(evaluator, job_directory, run_id):
    """What merging the iteration's job-level files will do, before doing it.

    The plan is kept in the parent's checkpoint, committed before the files are
    touched, so a merge interrupted part way is redone exactly: each file the
    iteration appended to is first cut back to its size in the plan.

    Returns
    -------
    dict
        ``{"append": {name: [offset in the iteration's file, size of the job's
        file before]}, "copy": [name, ...]}``, names relative to the job.
    """
    evaluator = Path(evaluator)
    job_directory = Path(job_directory)
    starts = _appended_starts(evaluator.parent, evaluator, run_id)
    plan = {"append": {}, "copy": []}
    for relative in _job_files(evaluator):
        name = str(relative)
        if name in starts:
            target = job_directory / relative
            before = target.stat().st_size if target.exists() else -1
            plan["append"][name] = [starts[name], before]
        else:
            plan["copy"].append(name)
    return plan


def merge_files(evaluator, job_directory, plan):
    """Do a plan from :func:`plan_files`. Doing it again gives the same files."""
    evaluator = Path(evaluator)
    job_directory = Path(job_directory)
    for name, (offset, before) in plan.get("append", {}).items():
        source = evaluator / name
        target = job_directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if before < 0:
            target.unlink(missing_ok=True)
        elif target.exists() and target.stat().st_size > before:
            with open(target, "r+b") as fd:
                fd.truncate(before)
        with open(source, "rb") as fd:
            fd.seek(offset)
            data = fd.read()
        with open(target, "ab") as fd:
            fd.write(data)
    for name in plan.get("copy", []):
        target = job_directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(evaluator / name, tmp)
        os.replace(tmp, target)


def merge_citations(evaluator, references):
    """Cite in the job's references what the iteration cited.

    Parameters
    ----------
    evaluator : str or Path
        The iteration's ``_evaluator`` directory.
    references : reference_handler.Reference_Handler
        The job's.
    """
    path = Path(evaluator) / "references.db"
    if references is None or not path.exists():
        return 0
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT c.alias, c.raw, x.module, x.note, x.level, x.count "
            "FROM citation c JOIN context x ON x.reference_id = c.id "
            "ORDER BY x.id"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    n = 0
    for alias, raw, module, note, level, count in rows:
        if alias == "SEAMM" and module == "seamm":
            # Every evaluator cites SEAMM as it starts; the job has already
            continue
        for _ in range(max(1, int(count or 1))):
            try:
                references.cite(
                    raw=raw, alias=alias, module=module, level=level, note=note
                )
            except Exception as e:
                logger.warning(f"Could not cite '{alias}' from {path}: {e}")
                break
            n += 1
    return n


def export_path(filename, evaluator, job_directory):
    """Where a file the iteration wrote at ``filename`` belongs in the job."""
    path = Path(filename)
    try:
        relative = path.resolve().relative_to(Path(evaluator).resolve())
    except ValueError:
        return path
    return Path(job_directory) / relative
