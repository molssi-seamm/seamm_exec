Getting Started
===============

``seamm_exec`` runs flowcharts (``run_flowchart``, ``run_from_jobserver``) and runs
the external codes that steps need. A step gets the executor from its flowchart and
asks it to run a program; the executor handles the program's configuration from
``<root>/<program>.ini`` (a conda environment, environment modules, a Docker image,
or a plain executable), where the program runs, and which files come back.

Running a code: ``executor.run()``
----------------------------------

The original interface, used by most steps:

.. code-block:: python

    result = self.flowchart.executor.run(
        config,                      # the program's section of <root>/<program>.ini
        cmd=["{code}", "input.dat", ">", "output.txt"],
        directory=self.directory,
        files={"input.dat": text},   # written before the run
        return_files=["output.txt", "*.log"],
        shell=True,
    )
    output = result["output.txt"]["data"]

``in_situ`` chooses where the code runs: ``None`` (the default) runs in a scratch
directory under a scheduler such as SLURM (``$TMPDIR``, so node-local storage) and
in place otherwise; ``True`` always runs in place, so the output can be watched as
it is written; ``False`` always uses scratch. Only the ``return_files`` come back to
the step directory.

Running codes as tasks
----------------------

Since 2026.10.2 a step can describe each calculation as a :class:`~seamm_exec.Task`
and run any number of them through a :class:`~seamm_exec.TaskSet`:

.. code-block:: python

    from seamm_exec import Resources, Task, TaskSet

    tasks = TaskSet(self, archive=True)
    for key, text in inputs.items():
        tasks.add(
            Task(
                key=key,                       # stable across reruns, e.g. a fragment key
                program="orca",
                cmd=["{code}", "orca.inp", ">", "orca.out"],
                files={"orca.inp": text},
                return_files=["orca.out", "orca.engrad"],
                resources=Resources(ntasks=4),   # mem_per_cpu in bytes if needed
                success_text={"orca.out": "ORCA TERMINATED NORMALLY"},
                estimated_seconds=30,
                shell=True,
            )
        )
    for result in tasks.run():
        if result.ok:
            parse(result.files["orca.out"])
        else:
            print(f"{result.key} failed: {result.reason}")

What this gives a step:

* **Concurrency.** The tasks run through a pool sized to the machine or to the
  SLURM allocation, each in its own process group; a task's ``Resources`` say how
  many cores and how much memory it needs, and ``ntasks=None`` means all of it.
  ``tasks.capacity()`` reports the pool's size, for a step that sizes its input
  (ORCA's ``%pal``) before writing it.
* **Restart.** The set keeps ``tasks/manifest.json`` in the step directory and a
  ``DONE`` marker in each task's directory, ``tasks/<key>/``. Rerunning the job in
  the same directory restores finished tasks from their markers and never
  recomputes them; a task whose inputs changed is recomputed. A task that failed is
  not retried within a run, but is tried again on a rerun, up to three attempts in
  all (the count resets when the inputs change). A process that a crashed run left
  behind is killed on the rerun, after checking it is the same process.
* **Success beyond the exit code.** ``success_text`` names text that must appear in
  a result file; ORCA exits 0 on an error termination, for example.
* **Archiving.** With ``archive=True`` finished task directories are packed into one
  tar per bundle, so a step with thousands of tasks leaves a handful of files.

A step with a single calculation uses the same machinery through
:func:`~seamm_exec.run_task`, which also keeps the output in the step directory, as
``executor.run()`` did:

.. code-block:: python

    result = run_task(task, self, directory=self.directory)

A step that handles ``<program>.ini`` itself passes the program's ``config``
(and ``env``) on the task, and the pool uses them as they are. A task without
``config`` is configured where it runs, from that machine's
``<root>/<program>.ini`` and the program's resolver (an entry point in
``org.molssi.seamm.exec.resolvers``; ORCA's adds its full path and the OpenMPI
paths). In the command, ``{code}`` is the program and ``{code_dir}`` its
directory, when it has one.

Many structures, one model chemistry
------------------------------------

A step that needs the energy (and gradients, and stress) of many structures
with the flowchart's model chemistry uses an :class:`~seamm_exec.Evaluator`
rather than tasks or MDI directly:

.. code-block:: python

    from seamm_exec import Evaluator

    with Evaluator(self, properties=("energy", "gradients")) as evaluator:
        for configuration in configurations:
            evaluator.submit(configuration, key=f"c{configuration.id}")
        for result in evaluator.results():
            if result.ok:
                store(result.key, result.energy, result.gradients)  # kJ/mol, kJ/mol/Å
            else:
                print(f"{result.key} failed: {result.reason}")

The evaluator chooses the path, not the step or the user. On a queue target a
program that can make tasks runs as tasks there; otherwise a warm MDI engine
evaluates the structures one after another, unless the program prefers tasks
(ORCA, whose engine starts ORCA for each structure anyway). Both paths give the
same numbers. A structure the program cannot run as a task (a periodic system
for ORCA or MOPAC) goes to its MDI engine; if there is none here it fails alone,
with the reason. The batch path keeps the task layer's restart, so a rerun
reuses finished structures. ``options`` on ``submit`` carries what a fragment
needs: ``atom_indices``, ``ghost_atoms``, ``charge`` and ``multiplicity``.

``resources`` on the Evaluator (a :class:`~seamm_exec.Resources`: ranks, memory
per rank) sets the size of each calculation on the batch path; the provider's
``get_task`` receives it. Without it the provider chooses.

A program offers the batch path through three classmethods beside
``get_model_chemistry_options``: ``get_task``, ``analyze_task`` and, optionally,
``can_run_task``; see :mod:`seamm_exec.evaluator`.

**The sign of the stress.** The stress comes back as the program gives it, in
GPa, and programs differ in its sign. A provider that returns a stress must
therefore declare its convention in its ``get_model_chemistry_options`` entry:
``options["stress_convention"]`` is ``"pressure"`` (positive when the system
pushes outward: VASP's ``in kB`` line, MDI's ``<STRESS``) or ``"stress"``
(sigma = -P, as ASE and xnn use). The Evaluator passes the stress through
unchanged; a consumer converts it with that declaration and refuses a level
that lacks it. Pinning one convention for every provider later would be a
documented change of this contract.

Where things run
----------------

Tasks go to the job's *target*, a section of the JobServer's
``<root>/<jobserver-name>.ini`` (see ``seamm_scheduler.config``). The JobServer
writes the job's section into the job directory as ``target.json``; for a run by
hand, ``SEAMM_TARGET=<section>`` (with ``SEAMM_TARGETS=<ini file>`` if it is not
``<root>/<hostname>.ini``) does the same. Without a target, or with
``tasks = pool``, tasks run on this machine or inside the current allocation, as
before.

With ``tasks = queue`` the ``TaskSet`` submits them, in bundles, as batch jobs:

.. code-block:: ini

    [arc]
    type = local
    tasks = queue
    scheduler = slurm
    transport = ssh
    host = tinkercliffs
    remote_root = /projects/seamm/psaxe/tasks       ; task directories, staged
    remote_python = /projects/seamm/SEAMM/venv/bin/python
    account = seamm
    partition = normal_q
    qos = tc_normal_short
    export = NONE
    bundle_tasks = 8                 ; or bundle_walltime = 04:00:00
    max_queued_tasks = 800

- Each bundle is one job that runs ``python -m seamm_exec.task_worker
  bundle.json`` on the cluster, which runs the bundle's tasks through a pool
  sized to the allocation. The program is configured *there*, from that
  machine's ``<root>/<program>.ini`` and the program's resolver (an entry point
  in ``org.molssi.seamm.exec.resolvers``), so the code need not be installed
  where the evaluator runs.
- Without a shared filesystem the task directories are copied to
  ``remote_root`` with ``rsync`` before the job and back after it; with
  ``shared_filesystem = yes`` (or the local transport) nothing is copied.
- A rerun of the step polls bundles still in the queue rather than submitting
  them again, and a bundle that ran out of time leaves its finished tasks done.
- Tasks estimated to take less than ``inline_below`` seconds (default 60) run on
  the evaluator's machine when their program is installed there.
- A task that carries its own ``config`` was configured for the evaluator's
  machine, so on an ssh target it runs there instead, with a warning. Steps that
  still configure their program themselves do this; ORCA (from orca_step
  2026.10.3.1) and the Evaluator's tasks name only their program.

Bundle files are in ``<step>/tasks/_bundles/<bundle>.<n>/``: ``bundle.json``,
``run.sh`` and the scheduler's log.
