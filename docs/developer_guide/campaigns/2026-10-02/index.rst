2026-10-02 -- Parallel execution: the task layer, schedulers and parallel loops
===============================================================================

Status: design complete and all six open questions settled with Paul on
2026-10-02; nothing implemented yet. Phase 1 (the task layer in ``seamm_exec``
with ``LocalPool``, the manifest, bundling, pruning and archiving, plus the
``orca_step`` and ``mopac_step`` conversions) is the first implementation
step. The canonical copy of this design is
``~/Work/SEAMM/Parallel_execution_design.rst`` at the workspace root; this is
the campaign copy, to be kept in sync while the design changes and to gain
``NOTES*`` files as the work proceeds. It continues the JobServer SLURM
campaign (``seamm_jobserver`` ``campaigns/2026-08-05``) and the multi-queue
routing campaign (``campaigns/2026-08-10``), and it answers the execution
question left open in ``MBE_correction_step_design.rst``.

Purpose
=======

SEAMM jobs run one step at a time, in one process, and every external code runs inline in that process.
Two common patterns need more than that:

1. **Many independent calculations inside one step.** The many-body correction (MBE) step needs about 2,800
   fragment calculations per periodic frame; the N-fragment counterpoise step, the Energy step over a
   trajectory, and the dimer-builder labelling all have the same shape.
2. **Loops whose iterations are independent.** A Loop over structures, table rows or parameters whose body
   may be one MOPAC step or a multi-step LAMMPS workflow, with results collected into tables or into the
   structure database.

This document designs one mechanism that serves both, works on a disconnected laptop, on a server plus
cluster pair (ChemAI + ARC), and on a cluster alone (ARC), and supports SLURM, PBS and other queueing
systems as well as none.

The design follows the model Paul used at Materials Design (MedeA): the **JobServer** manages jobs; a
**flowchart evaluator** does the lightweight work of interpreting the flowchart, preparing inputs and
analyzing outputs; and heavy codes run as **tasks** handed to whatever runs programs on the compute
resource, either a very simple **TaskServer** or the site's queueing system, which keeps the state.


Requirements
============

Deployment shapes
-----------------

All three must work with the same flowcharts and the same plug-in code:

=================== ====================================================================================
Shape               Where things run
=================== ====================================================================================
Laptop, offline     JobServer, evaluator and tasks all on the laptop. No queue, possibly no network.
Server + cluster    JobServer and evaluator on the server (ChemAI). Tasks on the server's own SLURM, or
                    on a remote cluster (ARC) over ssh. Datastore and job directories stay on the server.
Cluster alone       No persistent services allowed on the login nodes. The evaluator is a one-core queue
                    job; it submits tasks to the same queue from the compute node.
=================== ====================================================================================

Other requirements
------------------

- **Queueing systems:** SLURM and PBS from the start in the interface, SLURM first in code; LSF, Grid
  Engine and others later with no change to plug-ins. No queueing system must also work.
- **Restart:** never recompute a finished calculation. A frame of 2,800 fragments or a loop of 1,000
  iterations must resume after a crash, a walltime limit, or a JobServer upgrade.
- **Site limits:** respect per-user queued-job caps (TinkerCliffs: 1,000, each array element counting),
  bundle small tasks into shared allocations, and keep the file count down (the MBE prototype hit a
  10.5 M-inode quota overnight).
- **Live monitoring:** running a code in place so its output can be watched (MOPAC, LAMMPS trajectories)
  must remain possible; it is a per-task choice, not a global one.
- **Visibility:** the Dashboard should be able to show a job's tasks and their states.
- **Codes need not be installed where the evaluator runs.** The evaluator may be on ChemAI while ORCA is
  only on ARC.
- **Nesting:** a loop body may itself contain a step that fans out, or another parallel loop.


What exists today
=================

Facts established from the code on 2026-10-02:

- ``seamm_exec.Base.run(config, cmd, directory, input_data, files, env, return_files, shell, in_situ, ce)``
  already has the task shape: input files as a dict, a command template, return-file globs, a temporary
  working directory honouring ``$TMPDIR`` when not in situ, and copy-back of only the requested files. It
  is blocking, runs one command, and is given the whole allocation.
- ``computational_environment()`` reports the full SLURM allocation or the whole machine. Nothing shares an
  allocation among concurrent tasks. ORCA sets ``%pal`` to all of ``NTASKS``.
- Each code step reads its own ``<root>/<code>.ini`` (conda environment, modules, executable) in the
  evaluator process and resolves the command itself.
- ``seamm_slurm`` has ``SlurmBackend`` (submit, poll_many, cancel), ``LocalSlurm`` and ``SshSlurm``
  transports, ``status.classify()``, ``script.build_script()`` and ``RsyncStager``. It is already decoupled
  from the JobServer, which was intended to allow a per-step executor later.
- ``seamm_jobserver`` submits a whole flowchart as one sbatch when a queue section is of type ``slurm``, or
  spawns ``run_from_jobserver`` as a local subprocess. Queue sections live in ``<root>/<jobserver-name>.ini``
  and a job's ``parameters["queue"]`` selects one. Per-queue concurrency caps and resubmission exist.
- **Flowcharts do not resume.** The JobServer's resubmit-on-loss logic assumes a flowchart restarts from
  its first incomplete step. No such code exists in ``seamm``, ``exec_flowchart`` or ``loop_step``; a
  resubmitted job reruns from the top.
- ``loop_step`` runs iterations sequentially in one process. Iterations share the global variables dict,
  the in-memory pandas tables and their ``current index``, the SystemDB's current system pointer, and the
  references database. Nothing checks independence and finished iterations are not skipped.
- Tables exist only in memory, as ``{"type": "pandas", "table": DataFrame, ...}`` variables, until a Table
  step saves them. ``store_results`` writes cells with ``table.at[index, column]``.
- The structure database is SQLite in the job directory (``seamm.db``, WAL mode), owned by the one
  evaluator process.
- MDI engines are sequential by construction: one warm engine, one structure at a time. The ORCA engine
  runs a subprocess per evaluation in its own temporary directory, so for 27-second fragments MDI gains
  nothing over batch inputs.
- There is no parent/child job concept in the datastore, dashboard or JobServer.


Architecture
============

Roles
-----

.. code-block:: text

    Dashboard / Tk client / MCP
            |  submit job (flowchart, files, project, target)
            v
    JobServer  -- manages jobs; spawns one evaluator per job (subprocess, as today)
            |
            v
    Flowchart evaluator (run_from_jobserver) -- interprets the flowchart, runs lightweight steps,
            |     prepares inputs, analyzes outputs, writes the job database and checkpoint
            |  tasks (program, files, resources, return globs)
            v
    Task layer (seamm_exec) -- manifest, bundling, pruning, archiving, reattachment
            |
            +--> LocalPool      : in-process, partitions this machine or this allocation
            +--> TaskServer     : tiny service on a compute machine without a queue
            +--> Scheduler      : SLURM | PBS | ... via local commands or ssh
                     (staging: shared filesystem | rsync over ssh | TaskServer transfer)

The JobServer does not become a task broker. It keeps doing what it does: pick up submitted jobs, start an
evaluator, cap concurrency per target, reattach to or resubmit lost evaluators, and record state. The task
layer runs inside the evaluator. The queueing system or the TaskServer keeps the task state; the evaluator
keeps only the ids it needs to reattach.

How the three deployment shapes use this
----------------------------------------

- **Laptop:** the ``LocalPool`` back end. Tasks run concurrently, sized to the machine. A TaskServer on the
  laptop is optional and only buys independence from JobServer restarts.
- **Server + cluster:** the evaluator runs on the server under the JobServer. Its target section says
  ``scheduler = slurm, transport = local`` for the server's own queue, or ``transport = ssh, host = arc``
  for the cluster, with rsync staging of each task's directory. Only the heavy tasks leave the server.
- **Cluster alone:** the JobServer (on the server, or none) submits the evaluator itself as a one-core job,
  which is today's whole-flowchart path. The evaluator's own target section then says ``scheduler = slurm,
  transport = local`` and tasks are submitted from the compute node. The prototype's feeder job proved
  that sbatch works from TinkerCliffs compute nodes. Because the evaluator can outlive its walltime, this
  shape needs flowchart-level checkpointing (below).

The TaskServer
--------------

A deliberately small service for a machine without a queueing system (a workstation, a second Mac, a cloud
VM). Its protocol is the task API over HTTP, and nothing else:

- ``POST /tasks``: program name, files, command template, resources, return globs, in-situ flag. Returns an id.
- ``GET /tasks/{id}``: state (queued, running, finished, failed, cancelled), return code, listing.
- ``GET /tasks/{id}/files/{name}``: a returned file.
- ``DELETE /tasks/{id}``: cancel.
- ``GET /programs``: the programs it knows how to run, from its own ``<code>.ini`` files.

It owns the code configuration for its machine and a small pool for concurrency. It persists its queue
to a local SQLite file so it survives its own restart. Because it changes rarely, the JobServer and
plug-ins can be upgraded without losing running tasks.

**Transport and trust:** the TaskServer binds to loopback (``127.0.0.1``) only. A remote evaluator reaches
it through an ssh port-forward using the same passwordless ssh the scheduler transport already needs, so
trust is the user's ssh key and there are no new credentials, tokens or certificates to manage. The
``url`` of a ``tasks = taskserver`` target is therefore the local end of that tunnel, and the back end
opens the tunnel itself (``ssh -N -L``) when the target has a ``host``.

Resource vocabulary
-------------------

Tasks state resources in scheduler-neutral terms; each back end translates:

``ntasks``, ``cpus_per_task``, ``mem_per_cpu``, ``ngpus``, ``walltime``, ``partition``, ``account``,
``qos``, ``nodes``. A code step fills these from its own knowledge (ORCA: ``ntasks = 4``, ``mem_per_cpu =
2 GB``) or from the step's parameters, and may read site defaults from the target section. Within a task
the code sees its own allocation through the existing ``computational_environment()`` because the back end
runs it under the scheduler, or sets the equivalent variables for the pool.


The task API (``seamm_exec``)
=============================

Objects
-------

.. code-block:: python

    @dataclass
    class Task:
        key: str                    # unique within the step, stable across restarts (e.g. fragment key)
        program: str                # "orca", "mopac", "vasp", "run_flowchart", ...
        cmd: list[str]              # template; {code}, {NTASKS}, ... filled by the back end
        files: dict[str, str | bytes]
        return_files: list[str]     # globs; may include "@subdir+pattern" as today
        resources: Resources
        env: dict[str, str] = {}
        in_situ: bool | None = None # True = run and leave output in the task directory for watching
        shell: bool = False
        estimated_seconds: float | None = None   # plug-in's cost estimate; drives the inline rule
        target: str | None = None   # None = the job's target (reserved; no per-step override today)

    @dataclass
    class TaskResult:
        key: str
        state: str                  # finished | failed | cancelled | lost
        returncode: int | None
        stdout: str; stderr: str
        directory: Path             # <step dir>/tasks/<key>/
        files: dict[str, bytes | str]

    class TaskBackend(Protocol):
        def submit(self, tasks: list[Task]) -> list[str]: ...        # backend ids
        def status(self, ids: list[str]) -> dict[str, str]: ...
        def cancel(self, ids: list[str]) -> None: ...
        def fetch(self, task: Task, backend_id: str) -> TaskResult: ...

    class TaskSet:
        """What a step uses: submit many, wait, iterate results as they finish."""
        def __init__(self, node, target=None): ...
        def add(self, task: Task) -> None: ...
        def run(self) -> Iterator[TaskResult]: ...      # submits what is not done, polls, yields results
        def summary(self) -> dict: ...

``Base.run()`` stays for backward compatibility and becomes ``TaskSet`` with one task and ``LocalPool``
with one slot; existing steps keep working unchanged.

The manifest and restart
------------------------

Each step that uses tasks gets ``<step dir>/tasks/manifest.json`` recording, per key: the backend, its id,
the state, timestamps and the attempt count, plus ``<step dir>/tasks/<key>/DONE`` on completion. On
``TaskSet.run()``:

1. keys with ``DONE`` are yielded from their stored result and never resubmitted;
2. keys with a live backend id are reattached by polling, not resubmitted;
3. everything else is submitted, with a retry cap per task.

This is the same trust-the-record pattern the JobServer uses for jobs, and it gives fragment- and
iteration-level restart for free.

Target and the inline rule for tiny tasks
-----------------------------------------

Every task goes to the **job's target**, chosen at submission (``parameters["queue"]``). There is no
per-step target override; a flowchart that needs two targets is split into two jobs. ``Task.target`` is
reserved so an override can be added later without changing plug-ins.

The one exception is the **inline rule**, which catches the really bad cases such as submitting a 100 ms
MOPAC run to SLURM:

- the plug-in supplies a *cost estimate*, not a routing decision: ``Task.estimated_seconds`` computed from
  what the step knows (MOPAC: atom count and method; ORCA: atoms, basis and method class). A plug-in with
  no idea leaves it unset;
- each target has a threshold, ``inline_below`` (default 60 s); tasks under it run in the evaluator's own
  ``LocalPool`` instead of going to the queue, **provided the program is installed where the evaluator
  runs** (the pool checks for the program's ini section). Otherwise the task goes to the target as usual,
  where bundling still groups thousands of tiny tasks into one allocation.

Bundling
--------

Small tasks are grouped into bundles that share one allocation. A bundle is itself a scheduler job that
runs a generic worker script (shipped with ``seamm_exec``, pure Python, no SEAMM import) which executes the
bundle's tasks in order, writes each task's ``DONE``, and skips tasks already done. Bundling is in the task
layer, not implemented through native job arrays, because array support and limits differ between SLURM
and PBS and between sites. The target section sets bundle sizes (``bundle_tasks``, ``bundle_walltime``) or
the step computes them from an estimate per task, as the MBE prototype did (240 ORCA runs per 4-core job,
8 VASP fragments per 8-core job).

Pruning and archiving
---------------------

- ``return_files`` is the contract: nothing else comes back from a scratch run. In-situ tasks delete files
  not matched by ``return_files`` when they finish (``Base`` already does this).
- A step may declare ``archive = True`` on a ``TaskSet``; finished task directories are then packed into
  ``tasks/<bundle>.tar`` as their bundle completes, leaving the manifest, the archives and the step's own
  results. The MBE step requires this (about 14 files per frame instead of 10,000).
- Code steps should list the files worth keeping explicitly (for VASP: INCAR, KPOINTS, POSCAR, OUTCAR,
  OSZICAR, vasprun.xml).

Back ends
---------

``LocalPool``
    Runs tasks as subprocesses with a slot budget computed from the machine or the current allocation
    (cores and memory). It reads the local ``<code>.ini`` files exactly as the steps do today. Honour
    ``in_situ``. Set ``OMP_NUM_THREADS`` and the MPI binding policy per task as ``orca_step`` does now, so
    concurrent tasks don't pile onto the same cores.

``TaskServerClient``
    Speaks the TaskServer protocol. Staging is part of the protocol (files in the POST, files back by GET).

``SchedulerBackend``
    Parameterized by a scheduler module (below) and a transport (local commands or ssh), plus a stager
    (none on a shared filesystem, rsync over ssh otherwise). Writes one script per task or per bundle,
    submits it, polls many ids in one command, cancels, and fetches ``return_files`` after staging back.
    A target may set ``shared_filesystem = yes`` to skip staging when the evaluator and the target cluster
    see the same storage (TinkerCliffs, Falcon and Owl share ``/projects``).

    **Cross-cluster from a queued evaluator.** An evaluator that is itself a queue job may target another
    cluster over ssh. It is allowed, not precluded, but it is a last resort that the user sets up and
    tests: outbound ssh from compute nodes is site-dependent, and staging from a node whose allocation can
    end is fragile. The documentation says to test it from an interactive job first. The normal way to
    fan out across clusters is from the server shape, where the evaluator runs on ChemAI.

Code configuration moves to the back end
----------------------------------------

Steps currently resolve the executable, conda environment or modules themselves from ``<root>/<code>.ini``.
In the new model a step names the **program** and the back end resolves it on the machine where it runs:
the ``LocalPool`` from the local ini files, the TaskServer from its own, the scheduler back end from the
target section's ``setup`` text and the remote ini files. The command template keeps the existing
``{code}``/``{NTASKS}`` placeholders. This is the one refactor every code step shares, and it is done once
in ``seamm_exec``; a step's change is limited to replacing its ``executor.run(...)`` call with a ``Task``.


Scheduler abstraction
=====================

Generalize ``seamm_slurm`` into a new package, ``seamm_scheduler``, with one module per queueing system and
a shared interface. ``seamm_slurm`` remains as a thin compatibility shim re-exporting from
``seamm_scheduler`` so ``seamm_jobserver`` keeps working until it is updated to import the new package.

.. code-block:: python

    class Scheduler(Protocol):
        name: str                                   # "slurm", "pbs", ...
        def directives(self, resources: Resources, extra: dict) -> list[str]: ...  # "#SBATCH ..." lines
        def submit_cmd(self, script_path) -> list[str]: ...     # ["sbatch", "--parsable", ...]
        def parse_submit(self, stdout) -> str: ...              # job id
        def status_cmd(self, ids) -> list[str]: ...             # squeue/sacct or qstat
        def parse_status(self, stdout, ids) -> dict[str, str]: ...  # id -> queued|running|finished|failed|lost
        def cancel_cmd(self, ids) -> list[str]: ...
        env_names: dict[str, str]                   # {"ntasks": "SLURM_NTASKS", ...} for computational_environment

What is SLURM-specific in ``seamm_slurm`` today is exactly this set: the directive syntax, the submit and
status commands, the state vocabulary and the ``--json`` versus text parsing. The transports, the stager,
the script builder and the JobServer-facing backend are already generic and move up unchanged. The
JobServer's whole-flowchart submission and the task layer use the same scheduler modules, so a new
queueing system is one module plus tests.

``computational_environment()`` gains the same abstraction: it asks the scheduler module for the names of
its environment variables instead of hard-coding ``SLURM_*``.


Configuration
=============

The existing ``<root>/<jobserver-name>.ini`` sections become **targets** that describe both where a
flowchart evaluator may run and where its tasks run. New keys are additive; current files keep working.

.. code-block:: ini

    [DEFAULT]
    default = local

    [local]
    type = local                 ; evaluator runs as a local subprocess
    tasks = pool                 ; tasks: pool | taskserver | queue
    max_concurrent_jobs = 4

    [chemai]
    type = local                 ; evaluator on this machine
    tasks = queue                ; tasks go to this machine's SLURM
    scheduler = slurm
    transport = local
    partition = normal
    bundle_tasks = 50

    [arc]
    type = local                 ; evaluator still on ChemAI
    tasks = queue                ; tasks on ARC
    scheduler = slurm
    transport = ssh
    host = tinkercliffs
    remote_root = /projects/seamm/psaxe/tasks
    account = seamm
    partition = normal_q
    max_queued_tasks = 800
    setup = module load ORCA/6.1.1

    [arc-all]
    type = slurm                 ; evaluator itself is a one-core job on ARC (today's path)
    transport = ssh
    host = tinkercliffs
    tasks = queue                ; and it submits tasks locally from the compute node
    scheduler = slurm

    [workstation]
    type = local
    tasks = taskserver
    url = https://workstation.local:5500

A job's ``parameters["queue"]`` already selects a section; its meaning becomes "this job's target". The Tk
submit dialog's queue picker and the ``GET /api/queues`` route need no conceptual change. A step may
override the target for its own tasks through a parameter when that is genuinely needed (a cheap
preprocessing step staying local while the heavy step goes to the cluster), but the default is the job's
target.


Model Chemistry batch contract
==============================

MDI remains the right interface for a warm engine on one machine. Farming out needs the prepare/analyze
split. Add to the provider interface, next to ``get_model_chemistry_options()`` and
``get_mdi_engine_command()``:

.. code-block:: python

    def get_task(self, configuration, level, *, properties=("energy", "gradients"),
                 resources=None, options=None) -> Task: ...
    def analyze_task(self, result: TaskResult, level) -> dict: ...
        # {"energy": kJ/mol, "gradients": (n, 3) kJ/mol/Å, "stress": (3, 3) GPa, ... , "citations": [...]}

Consumers (Energy, MBE, N-fragment counterpoise, dimer builder) use a facade in ``seamm`` with the same
calls as ``seamm_mdi.MDIEngine`` but asynchronous: ``submit(configuration) -> key`` and ``results() ->
iterator``. **The facade, not the user or the step, chooses the path:** MDI when the job's target runs
tasks in the local pool and the provider has an MDI engine (so MLFF and MOPAC stay at milliseconds per
structure), the batch path otherwise (ORCA and VASP fan out). Energy, MBE and the counterpoise code
inherit the rule with no per-step logic, and the Energy step keeps its MDI path. Order of implementation: ORCA (its MDI engine already contains the analyze
half), MOPAC (tests the whole chain on a laptop in seconds), then the VASP registered-fragment mode the MBE
step needs. Options a consumer must be able to pass through: ghost atoms (counterpoise), point charges, an
initial guess file, and the registered-box parameters for VASP.


Tables in the job database
==========================

Tables move from in-memory pandas DataFrames to tables in the job's SQLite file (``seamm.db``), beside the
structures and properties. Reasons:

- the job's whole state is one file that is already on disk and already staged by rsync, so checkpointing
  needs no table serialization;
- merging parallel iterations is a SQL insert keyed by iteration index, through the same path as properties;
- the Dashboard, the web UI and the MCP server can read a job's tables directly, and "save or lose it" goes
  away.

Keep a DataFrame-shaped facade so existing code keeps working: a ``Table`` object backed by the SQL table,
with cell writes (``store_results``'s ``table.at[index, column]``) going straight through, a DataFrame view
materialized on demand for printing, plotting and ``$table`` expressions, and the ``current index`` and
``index column`` stored in a ``_tables`` registry table. Column types map to SQLite's dynamic typing;
``molsystem`` supplies the connection and the WAL settings. The Table step's Save/Save as keep exporting
CSV, Excel and JSON. Rule, unchanged: one writer per database file, which is why children never write
into the parent's file (see the Loop).

**Why not a multi-writer DBMS.** PostgreSQL would allow concurrent writers but is a server to install,
secure and reach from every compute node, and would mean porting ``molsystem`` off the sqlite3 module.
DuckDB is single-writer like SQLite. rqlite/dqlite funnel writes through one leader behind a daemon. The
deciding constraint is not the engine but the network: a task or iteration on an ARC compute node has no
route back to ChemAI and its allocation can end mid-write, so any shared-write design needs a network path
the cluster shapes do not reliably provide. Pull-merge needs none: the child writes its own file, the file
is staged back with the task, and the single writer merges it. A job also stays a self-contained
directory that the dashboard reads, rsync moves and Zenodo archives. No DBMS change is on the roadmap.


Checkpointing
=============

Task level (first)
------------------

The manifest and ``DONE`` markers above. Cheap, and it makes almost all of the compute safe to interrupt.
This alone meets the MBE requirement that no finished fragment is recomputed.

Flowchart level (second)
------------------------

Needed for the cluster-alone shape, where the evaluator outlives its walltime, and to make the JobServer's
resubmit-on-loss assumption true. Scheme:

- After each node completes, the evaluator writes ``checkpoint.json`` in the job directory: the id of the
  next node, the JSON-serializable variables, the current system and configuration ids, and the completed
  node ids. Tables and properties are already in the database. Non-serializable variables are listed by
  name and recreated by the node that made them, or the checkpoint refuses and says why.
- The Loop node records its iteration index and the per-iteration completion in the same file, so a loop
  resumes at the first unfinished iteration.
- On restart, nodes with a completion record are not run; their side effects are restored from the
  checkpoint. A node that was mid-flight re-enters ``run()`` and its ``TaskSet`` reattaches through the
  manifest.
- Nodes must therefore be idempotent on re-entry up to their tasks: they regenerate inputs
  deterministically (the task key makes this checkable) and must not append to tables before their tasks
  finish. A short audit of the code steps is part of this phase.
- The JobServer's "trust ``job_data.json``, else resubmit up to ``max_resubmits``" path becomes correct
  without change.


The parallel Loop
=================

A ``parallel`` option on the Loop step (default off) runs each iteration as a task whose program is the
flowchart evaluator:

- **Child flowchart:** the loop body as a flowchart, with a start node that loads a snapshot: the
  variables at loop entry plus the loop variable and ``_loop_index``, and a child ``seamm.db`` holding
  **only the iteration's selected configuration(s)** and copies of the tables the body reads (the
  default). A Loop option, *give iterations the whole database*, copies the parent's ``seamm.db`` for
  bodies that read other structures, such as pairing the current structure against others.
- **Contract (documented, user-declared, not verified):** the body communicates back only through table
  rows, properties on its configuration(s), and files in its iteration directory. Variables set inside an
  iteration are not visible after the loop. Table rows written by iterations are appended in iteration
  order when the parent merges. This is the same contract a job array imposes.
- **Merge:** the parent collects each child's ``seamm.db`` and inserts its new table rows and properties
  into its own database, keyed by iteration index; files stay in ``iter_N/``.
- **Placement:** one choice per target, from the Loop's parameters: run the body's codes **inline** in the
  iteration's allocation (default; right for a uniform body such as one MOPAC or one small ORCA step, with
  per-iteration resources set in the Loop) or **as separate tasks** submitted by the child (right for a
  body mixing a long LAMMPS run with short analyses). Iteration evaluators are small, so the task layer
  bundles several per allocation on a cluster.
- **Shapes:** on the laptop iteration evaluators run concurrently in the pool; on ChemAI + ARC they run on
  ChemAI and only their heavy tasks cross to ARC; on ARC alone they are one-core queue jobs.
- **Nesting** works because an iteration containing another parallel loop is a task spawning tasks.
- **Restart:** each child has its own manifest and checkpoint; the parent's manifest tracks iterations.
- **Errors:** the existing ``errors`` parameter (continue / exit / raise) applies per iteration; failed
  iterations are reported in the loop's results and never merged.


How the MBE step maps onto this
===============================

- Enumerate fragments; each fragment is a ``Task`` from the provider's ``get_task()``, keyed by its canonical
  fragment key, with ``resources`` from the level (VASP 8 ranks, 2 GB per rank; ORCA 4 ranks) and
  ``return_files`` restricted to the parse set.
- Two ``TaskSet``\ s per frame (periodic and molecular levels), ``archive = True``, bundle sizes from the
  per-task estimate. The cell calculation is a single task.
- The step waits on both sets, then runs the increment algebra from ``seamm_mbe``; a frame with a failed
  fragment is reported and never written as a label.
- Restart comes from the manifest; a resubmitted job finishes the frame.
- Execution model (b) of the MBE document ("fan-out") is realized without new JobServer capability.


Dashboard visibility
====================

Add a ``tasks`` summary to the job's ``job_data.json`` (counts by state per step) that the evaluator updates
as its ``TaskSet``\ s progress, and a ``GET /api/jobs/{id}/tasks`` route in ``seamm_webui`` that reads the
manifests. No datastore migration is needed. A later option is for the JobServer to register child
iteration jobs as real datastore jobs with a ``parent_id`` in the ``parameters`` JSON, which would show
them in the job list; not needed for the first release.


Phases
======

1. **Task layer in ``seamm_exec``** with ``LocalPool``, the manifest, bundling, pruning and archiving, and
   ``Base.run()`` reimplemented on it. Convert ``orca_step`` and ``mopac_step`` to ``Task``. Test on the
   laptop with concurrent MOPAC and ORCA runs; verify restart by killing mid-run.
2. **Scheduler abstraction and the SLURM back end.** Refactor ``seamm_slurm`` into scheduler modules used
   by both the JobServer and the task layer; local and ssh transports with rsync staging; bundled worker
   script. Validate ORCA fragments from this Mac to TinkerCliffs and from ChemAI to its own SLURM.
   Start the PBS module as a compile-time proof of the interface, tested against a mocked ``qstat``.
3. **Model Chemistry batch contract** for ORCA and MOPAC, the asynchronous facade, and the Energy step
   using it. Then the MBE step (its Phase 3) on this.
4. **Tables in the database** behind the DataFrame facade; Table step and ``store_results`` converted.
5. **Flowchart-level checkpointing** in ``exec_flowchart``, ``Node.run()`` and the Loop; node idempotence
   audit of the code steps; JobServer resubmission validated for real on ARC-alone.
6. **Parallel Loop** with the snapshot, merge and placement options; validate on the laptop (pool), on
   ChemAI + ARC, and on ARC alone.
7. **TaskServer** and its client; **PBS** validated on a real PBS site when one is available; Dashboard
   task view.

Phases 1 and 2 unblock the MBE step; 4 and 5 are prerequisites of 6.


Decisions (2026-10-02)
======================

Settled with Paul, in the order the open questions were discussed:

- The JobServer stays a job manager; the task layer lives in the evaluator (MedeA model).
- The queueing system or the TaskServer keeps task state; the evaluator keeps ids in a manifest.
- Steps request a program by name plus scheduler-neutral resources; the back end owns code configuration.
- Bundling is generic (worker script), not native job arrays.
- Tables move into the job database behind a DataFrame facade; SQLite stays, one writer per file,
  results from children return by staging and merge. No multi-writer DBMS.
- Batch contract on the Model Chemistry providers; MDI kept for local warm engines, chosen by the facade.
- Task-level restart first, flowchart-level checkpointing second.
- Parallel Loop iterations are evaluator tasks with a declared independence contract.
- **Q1 package:** new ``seamm_scheduler``; ``seamm_slurm`` becomes a compatibility shim.
- **Q2 cross-cluster from a queued evaluator:** allowed as a last resort, the user's responsibility to set
  up and test; ``shared_filesystem`` flag skips staging on sites like ARC.
- **Q3 per-step target:** no override; the job's target for everything, except the plug-in cost-estimate
  inline rule for tiny tasks. ``Task.target`` reserved for later.
- **Q4 TaskServer:** loopback only, reached through an ssh port-forward.
- **Q5 iteration snapshot:** only the selected configurations by default, with a whole-database option.
- **Q6 Energy step:** keeps MDI; the facade picks MDI versus batch.


Remaining questions
===================

None blocking. Items to settle during implementation:

1. The exact ``estimated_seconds`` heuristics per code, and whether the threshold should also consider
   the queue's current wait.
2. The merge policy when two iterations write the same table index (error, or last wins with a warning).
3. Whether the Dashboard shows child iterations as rows (``parent_id`` in ``parameters``) in the first
   release or only the per-step task counts.
