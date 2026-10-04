Phase 6 notes
=============

2026-10-04 -- design note (draft for Paul and the design session)
-----------------------------------------------------------------

Phase 6 adds a ``parallel`` option to the Loop step: its iterations run at the same
time, each in its own evaluator with its own job database, and the parent merges
what they did back into the job. Nothing is coded yet. This note records the survey,
the proposed design and the decisions Paul needs to make, marked **D1** to **D8**.

Inputs: the design doc's "The parallel Loop" section (decided 2026-10-02: iterations
are evaluator tasks with a declared independence contract; the snapshot holds the
selected configurations by default, the whole database as an option, Q5); phase 4's
change journal "for the phase 6 merge"; phase 5's checkpoint (frames, frozen items,
``run_id``, restore hooks); and the design session's ten points and five follow-ups
(2026-10-04), each answered below.

The idea: an iteration is a resume
----------------------------------

A child evaluator runs **the parent's own flowchart file**, resumed from a checkpoint
the parent synthesizes: the Loop's frame holds the frozen items and iteration *k*,
the next node is the body's first, and the frame is marked "this iteration only". The
child is started like any resume (``SEAMM_RESUME=1``). When iteration *k* ends, the
Loop sees the mark, leaves without advancing, and the evaluator stops there instead
of running the rest of the flowchart.

Everything phase 5 built then applies unchanged: the variables at loop entry (the
codec, the restore hooks keyed by step id, ``Unrestorable`` markers), the current
system, the Loop's frozen item list and directory name, nested loops, and the
fingerprint, which matches by construction because it is the same flowchart. Step
numbering is the parent's (``3.iter_0017.2``), so step directories and headers are
what a serial loop gives.

*The alternative* is to cut the loop body out as a standalone flowchart
(``format3.tree`` → the Loop's item → ``_steps_data`` → ``from_data``) and start it
with injected state. The survey found it workable but worse: there is no way today
to inject variables or the current system at start (a new mechanism), the child
numbers its steps from 1 (directories and headers shift), nested loops and restore
hooks would need the same treatment again, and it is a second code path for running
a body. Resume reuses tested machinery; this note goes with it.

Survey (2026-10-04)
-------------------

- **No cross-database copy or merge exists in molsystem.** Only same-database
  ``copy_configuration``/``create_combined_system``; ``attach`` and ``_Table.copy``
  (whole SQL tables) are the tools. Primary keys are plain ``INTEGER PRIMARY KEY``,
  so every child allocates the same new ids: the merge must remap every new object
  and its foreign keys (system → configuration → atomset/bondset/cell/symmetry →
  atoms, bonds, coordinates, velocities, gradients; subsets, templates). Properties
  are rows keyed by a per-database property id: map by name.
- **The table journal has no values** (op, table, row, column only), does not record
  ``current_row``/metadata/index changes, and appended rowids collide across
  children (``append_rows`` starts at the copy's own maximum).
- **Job-level paths follow the root.** ``Node.directory`` and ``Node.job_path`` are
  both ``flowchart.root_directory``; ``/name`` and ``job:NAME`` paths (Write Structure,
  Read Structure, LAMMPS, MBE's labels file "so a loop gathers every configuration
  into one file", ORCA checkpoints, Properties' ``<table>.csv``) resolve against it.
- **No "run_flowchart" program for the task layer**: a program needs a ``config``,
  an ini, or a resolver (only orca, mopac, vasp register one).
- ``safe_filename`` de-duplicates by looking at the disk, so directory names are fixed
  by the parent before dispatch (the frozen frame already holds them).
  ``BreakLoop``/``ContinueLoop``/``SkipIteration`` are exceptions inside a process.
- Each child writes its own ``references.db`` and ``final_structure.*``.

The design
----------

Where a child runs (design follow-up 2)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The flowchart gets a ``job_directory`` beside ``root_directory`` (defaulting to it,
so nothing changes for a normal job): ``Node.directory`` stays under
``root_directory``, ``Node.job_path`` (``/name``, ``job:``) and the evaluator's own
files move to ``job_directory``. A child runs with

- ``root_directory`` = the parent's job directory, so its step directories are the
  serial loop's ``<loop>/<iter dir>/<step>`` -- distinct per iteration, so children
  never share one;
- ``job_directory`` = ``<loop>/<iter dir>/_evaluator/``: its ``job.out``,
  ``job_data.json``, ``seamm.db`` (the snapshot it writes), ``baseline.db`` (the
  untouched snapshot, read-only), ``checkpoint.json``, ``references.db``, and any
  job-level file the body writes (``/name``).

So no two evaluators ever write one database (the one-writer rule), and nothing the
child writes collides with the parent or a sibling. On a target without a shared
filesystem the iteration's directory is staged out and back as a unit.

The snapshot (design 1, follow-up 4)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Built by the parent, committed state only, into ``_evaluator/seamm.db``:

- **default** (Q5): the iteration's selected configurations -- the current
  configuration (a loop over systems), plus any configurations the frozen frame names
  -- each with its system, atoms, bonds, cell, symmetry, coordinates, velocities,
  gradients and properties, copied with **their parent ids**; all user tables with
  their rows, registry and rowids (copying only "the tables the body reads" is not
  knowable in general; tables are small next to structures); the property
  definitions; an empty journal. A new molsystem ``SystemDB.snapshot(path,
  configurations=..., whole=False)`` does it with ``ATTACH`` and ``INSERT … SELECT``,
  following the foreign keys. Keeping parent ids makes "unchanged" mean "same id,
  same rows", and only new objects need remapping on merge.
- **whole database** (Loop option): ``sqlite3`` backup API, a consistent copy of the
  committed parent.

Then ``baseline.db`` = a copy of the snapshot, kept read-only: the merge diffs against
it. The checkpoint row is written into the snapshot last (``run_id`` new for the child
-- its ``appended_files`` records are its own).

What comes back: the merge (design 2, 3)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The parent merges iterations **in iteration order**, each in one transaction together
with its checkpoint (the merged prefix in the Loop frame), so a kill mid-merge leaves
either nothing or the whole iteration merged. A finished iteration waits for the ones
before it; merging is cheap next to running.

- **Tables** (authority: the child's journal). Replay in journal order: ``create`` and
  ``add_column`` if missing; ``append`` → a new parent row (renumbered: never promise
  rowid stability; the user-visible key is the index column or position); ``set`` on a
  baseline row → the same parent row; ``set`` on an appended row → its renumbered row.
  Values are read from the child's database by (table, row, column) at merge time: the
  final value is the one that matters, and the journal stays small (decision: no value
  column). Two iterations setting the same cell of a baseline row: **D3**.
- **Properties**: values the child added or changed on configurations it had from the
  parent go onto the same parent configurations (property ids mapped by name).
- **Structures**: **D1** -- what to bring back.
- **Files**: the step directories are already in place. Job-level files in
  ``_evaluator/``: a file Write Structure *appended* (its ``appended_files.json``
  record says from which size) has the appended bytes appended to the parent's file,
  in iteration order; any other job-level file is copied to the parent's job
  directory, later iterations replacing earlier ones (what a serial loop leaves);
  tables a child exported (``Save as``) are re-exported from the merged table at the
  end of the loop. The evaluator's own files stay in ``_evaluator/``.
- **Citations**: the child's ``references.db`` entries are cited in the parent's.
- **Variables**: not merged. A variable set in the body is not visible after the loop
  (the contract); the loop variables are removed as now.
- A body that *reads* a table or structure another iteration writes sees it as it was
  at loop entry (snapshot semantics), unlike a serial loop.

The contract (design 3)
~~~~~~~~~~~~~~~~~~~~~~~

Documented in the Loop's help and user guide, declared by choosing ``parallel``, not
verified: iterations are independent; each sees the job as at loop entry; what comes
back is what D1 decides plus table rows, properties and files; variables set in the
body are not visible after the loop; ``break`` / ``continue`` / ``skip`` work per
iteration (a ``break`` in iteration *k*: iterations after *k* are cancelled or not
merged, so the result is the serial one).

Running children: placement and resources (design 5, 7; follow-up 5)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each iteration is a ``Task`` in the Loop step's ``TaskSet`` (manifest under the loop's
``tasks/``), program ``seamm`` -- a new resolver in seamm_exec gives the evaluator
command for the target: the parent's own interpreter locally, the target's
``remote_python`` (``-m seamm_exec.exec_flowchart``) on a cluster -- with
``SEAMM_RESUME=1`` in the task's environment. Resources per iteration come from the
Loop (cores, memory, walltime estimate).

- **Placement** (design doc): *inline* (default) -- the body's codes run in the
  iteration's own allocation through the child's ``LocalPool``; *separate tasks* --
  the child submits its codes to the target like any job.
- **Shapes**: on the laptop the parent's ``LocalPool`` runs ``cores / cores per
  iteration`` children at once; with a queue target the iteration evaluators are
  bundled several per allocation (the task layer's bundling); on ARC alone the same
  from a queued parent.
- **No deadlock** (the rule): a child's own codes and a nested parallel loop inside it
  use the *child's* pool and the *target*, never the parent's pool; the parent's pool
  slots hold only iteration evaluators, and the parent waits on nothing else. A nested
  parallel loop is a child spawning children against the target.
- A child's lost or timed-out allocation is retried by the task layer's rules
  (``max_attempts``; the ``2**n`` time for timeouts); the retried child resumes from its
  own checkpoint.

Errors (design 6)
~~~~~~~~~~~~~~~~~

The ``errors`` parameter applies per iteration: *continue* -- the failed iteration is
reported (in ``job.out`` and the Loop frame's ``failed`` list) and **never merged**,
unlike a serial loop, which keeps a failed iteration's partial writes (phase 5 D1);
*exit* -- no new iterations start, running ones are cancelled, and the iterations
before the failed one are merged; *raise* -- as *exit*, then the job fails.

Checkpoint and restart (design 4)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The parent's Loop frame keeps the frozen items, ``parallel: true``, the merged prefix
and the failed list; the per-iteration states (pending, running, done, failed) are the
``TaskSet`` manifest's, as for any task. A resumed parent re-plans from them: done
and unmerged → merged; running → reattached (queue) or rerun (local, the child
resumes itself); pending → submitted. A child uses the same ``_checkpoint`` row and
``checkpoint.json`` as any job.

Dashboard (design 8)
~~~~~~~~~~~~~~~~~~~~

First release: the Loop's ``job.out`` reports iterations started, finished, failed and
merged; the task counts are in the manifest. Child iterations as rows of their own
(``parent_id``) later.

Decisions for Paul
------------------

- **D1 -- structures.** Recommended: bring back everything an iteration did to
  structures -- new systems and configurations are added to the parent (ids
  remapped), and configurations from the parent that the iteration changed (e.g.
  optimized with "overwrite the current configuration") replace the parent's. This
  keeps the common serial loops ("create a structure from SMILES, optimize it, store
  results") giving the same database. Two iterations changing the same configuration
  (possible only with the whole-database snapshot) is an error. *The design doc's
  stricter contract -- table rows, properties and files only -- is the alternative;
  it is less work but silently drops the structures such loops make.*
- **D2 -- snapshot content by default**: the selected configurations and all user
  tables (recommended), not only "the tables the body reads", which cannot be known.
- **D3 -- the same cell written by two iterations**: an error by default, a Loop option
  to let the later iteration win with a warning (design session's recommendation).
- **D4 -- failed iterations** under *continue* are not merged (design doc), unlike a
  serial loop, which keeps their partial writes.
- **D5 -- job-level files**: appended files gathered in iteration order, other files
  last-iteration-wins, exported tables re-exported after the merge (recommended).
- **D6 -- the Loop's new parameters**: ``parallel`` (no / yes), ``iterations at once``
  (default: as many as the cores allow), ``cores per iteration`` (default 1),
  ``memory per iteration``, ``snapshot`` (selected structures / whole database),
  ``placement`` (inline / separate tasks), ``same-cell writes`` (error / later wins).
  Grayed or hidden when ``parallel`` is no (the GUI rule).
- **D7 -- scope of the first release**: all four loop types; ``break`` handled as
  above; Dashboard child rows later.
- **D8 -- validation**: an A/B harness comparing serial and parallel runs of the same
  flowcharts (tables, properties, structures, files; identical, row order aside where
  the table has an index); the kill soak extended to kill a child, the parent, and
  both; a live run on TinkerCliffs with iterations bundled as queue jobs. Local runs
  stay small (MOPAC, a few iterations at once; the 2-4 GB rule).

Packages and order
------------------

molsystem (``snapshot``, structure import with id remapping, table journal replay
helpers) → seamm (``job_directory``, the "iteration only" frame, the snapshot
checkpoint) → seamm_exec (the ``seamm`` resolver, child mode in ``ExecFlowchart``,
merge driver) → loop_step (the ``parallel`` option, dispatch, merge in order, GUI)
and the six steps that build job-level paths from ``root_directory`` (read_structure,
table, properties, lammps, orca, geometry_analysis; review point 6).
seamm_scheduler only if bundling evaluators needs it. Opt-in, default off: no soak
switch; pins to the phase 5 versions.

Review (design session, 2026-10-04)
-----------------------------------

The design session reviewed the draft and approved the design (child as a resume,
``job_directory`` beside ``root_directory``). Folded in:

1. *Must:* **no snapshot or merge through the parent's deferred connection.** An
   ``ATTACH`` + ``INSERT … SELECT`` on the parent's connection would be part of its
   open transaction (not visible to the child until the parent commits), and with
   ``detach`` a no-op while deferring, one attachment per iteration would hit
   SQLite's limit of 10. So: the Loop writes the iteration's frame and commits
   (checkpoint) **before** dispatch; ``SystemDB.snapshot()`` reads committed state
   through its **own short-lived connection** (or the backup API for the whole
   database). The merge runs in the parent's per-iteration transaction but reads the
   child's database through a **separate read-only connection**, never an ``ATTACH``
   on the parent's.
2. *Must:* **what a child needs on a target without a shared filesystem.** Stage in:
   the flowchart, ``<loop>/<iter dir>/`` (with ``_evaluator/``), and the job
   directory's top-level *files* (inputs read by ``/name`` or ``job:``: structure,
   forcefield, control-parameter files) -- not other iterations' or steps'
   directories; a Loop option adds paths. Stage out: ``<loop>/<iter dir>/`` only. So a
   500-iteration loop does not copy the job 500 times.
3. **Current row after the loop**: as a serial loop leaves it -- for each table an
   iteration moved, the current row of the last merged iteration (mapped to the
   parent's row); otherwise unchanged.
4. **New property definitions** a child created (``store_results``) are created in the
   parent before values are mapped by name, as a child's new tables are; a child's
   ``add_column`` whose type or default differs from the parent's column is an error.
5. **Iterations at once**, memory-aware: ``min(cores // cores per iteration,
   memory // memory per iteration)``, memory per iteration defaulting to 2 GB -- on a
   16 GB laptop 4-8 MOPAC children but 1-2 ORCA (the machine-wide 2-4 GB rule).
6. **Everything job-level keys off ``job_directory``** in a child: ``plan_start``,
   ``archive_previous_database``, ``checkpoint.json``, ``job_data.json``, ``job:`` and
   ``/name``. Direct uses of ``flowchart.root_directory`` for job-level paths, which
   must move to ``Node.job_path`` (else a child writes into the parent's job
   directory): read_structure_step ``write_structure.py:180`` and
   ``read_structure.py:204,209,213`` (``213`` derives other jobs' directories for
   ``job://<n>`` and must use the *parent* job's directory); table_step
   ``table.py:305`` (``Save as /name``); properties_step ``properties.py:286``;
   lammps_step ``nve.py:485``; orca_step ``energy.py:655`` (checkpoints);
   geometry_analysis_step ``geometry_analysis.py:230``. The code steps' uses that pass
   the root to their subflowcharts stay (step directories), and gaussian_step's are
   its own install root. So phase 6 also touches these six step packages.
7. **Errors**, completed: under *exit*/*raise*, iterations after the failed one that
   had already finished are not merged (as for ``break``) and their directories are
   kept and reported; under *continue*, a failed iteration's step directories and
   ``_evaluator/`` stay for debugging.

Nits taken: D3's same-cell rule covers the index column; citations merge by key
(no duplicates); each child's iteration gets the usual banner in the parent's
``iteration.out``; the parent's ``job_data.json`` records iterations run, failed and
merged.

The design session's recommendations on D1-D8 match the ones above, with D6's
memory-aware default (5).

Decisions (Paul, 2026-10-04)
----------------------------

D1-D8 approved as recommended (with the review's memory-aware D6): structures come
back with ids remapped; the default snapshot is the selected structures and all
tables; the same cell from two iterations is an error unless the Loop allows the
later to win; failed iterations are never merged; job-level files as in D5; the new
Loop parameters; all four loop types with inline and separate placement; validation
by serial-vs-parallel comparison, kill soak on parent and children, and a live
TinkerCliffs run.

Implementation (2026-10-04)
---------------------------

Local commits on ``dev``, not pushed; all ten packages installed editable in
``~/SEAMM_DEV/venvs/phase6-B``.

- **molsystem** ``molsystem/snapshot.py``: ``snapshot`` (own connection; selected
  configurations with their ids, or the whole database by the backup API),
  ``baseline`` (read-only copy) and ``merge`` (property definitions by name, id
  tables remapped, row tables, property data, the table journal replayed with
  values read from the child, ``MergeConflict``). Found in testing: an iteration
  adds *columns* -- per-atom attributes of the structure tables (MOPAC's charges)
  and table columns after rows it appended (MOPAC results) -- so the merge adds
  new columns first and reads the baseline with NULL for them.
  ``System.copy_configuration`` fixed on the way (it copied from the new
  configuration, not the previous one).
- **seamm**: ``Flowchart.job_directory`` (defaults to ``root_directory``);
  ``Node.job_path`` follows it, ``Node.job_file(name)`` reads the iteration's own
  file else the job's (inputs read by ``/name`` or ``job:NAME``); citations go to
  ``job_directory``; ``IterationDone(broke, skipped)``; ``Checkpointer.write_child``
  (the child's checkpoint, written into its snapshot), ``parallel_loop`` (the
  parallel frame, committed), and ``iteration`` kept in the final checkpoint.
- **seamm_exec** ``seamm_exec/iteration.py``: the ``seamm`` resolver (entry point;
  ``run_flowchart`` beside the interpreter; the child's share of the machine as
  ``SEAMM_CE``, which ``computational_environment`` honours), ``iteration_task``
  (shell command with relative flowchart and parent paths, ``SEAMM_RESUME=1``,
  ``SEAMM_PARENT_JOB``, ``SEAMM_TARGET=""`` for inline placement, ``target.json``
  copied for separate), ``iteration_outcome``, ``merge_database``,
  ``plan_files``/``merge_files`` (appends from Write Structure's records, by run id;
  other files copied; redone exactly from the plan), ``merge_citations`` (not the
  SEAMM citation every evaluator adds). Child mode in ``exec_flowchart``: forced
  standalone (no datastore job of its own); resumes only its parent's checkpoint,
  never from the top; a rerun after the iteration finished reports it from the
  checkpoint. ``Task.keep`` (``"."`` for an iteration) so an in-situ task's
  cleanup leaves the evaluator's files; ``TaskSet`` looks for ``target.json`` in
  ``job_directory``.
- **loop_step**: the D6 parameters, shown only when parallel is yes. ``run()``
  refactored around ``_prepare`` / ``_setup_iteration(k)`` / ``_end_loop`` shared
  by the serial loop, the child (``only``) and the parent. The parent:
  ``parallel_loop`` frame (frozen items, directories given out, ``next`` to merge,
  failed, merge state, exports, pending file merge); ``loop_entry.db`` (the
  committed database at loop entry, rollback journal) from which every snapshot
  is taken; per iteration its set-up, directory name committed before the
  directory exists, snapshot + ``write_child`` + baseline, then a task; the
  ``TaskSet`` with a ``LocalPool`` of ``iterations at once``; merge in order (one
  transaction + checkpoint each, then files and citations from a committed plan);
  for a loop over rows the iteration's row is made current before the merged
  current rows are applied (the merge cannot see a move back to the entry's
  current row). An evaluator that stopped before its iteration ended (its
  checkpoint still 'running': killed, out of memory) is run again, resuming, up
  to twice; one that ended in error is a failed iteration. Errors: *continue*
  records and goes on; *exit* records and stops; *stop the job* raises without
  recording, so a resume runs the iteration again. ``break`` stops dispatching
  and merging after the iteration; skipped iterations are merged and their
  directory removed, as serially.
- **The six steps** (read_structure, table, properties, lammps, orca,
  geometry_analysis): job-level writes to ``job_path``, reads through
  ``job_file``; ``job://<n>`` keeps ``root_directory`` (the parent job in a child);
  ORCA's read-only checkpoint lookup falls back to the job's checkpoints.

Validation so far (``Testing/phase6``: ``ab_parallel.py`` builds each spec serial
and parallel with ``seamm-flowchart`` and compares databases and files;
``kill_parallel.py`` kills the parent, a child, or both, resumes, and compares
with the serial run):

============================  ==============================================
case                          serial vs parallel
============================  ==============================================
p1 rows + MOPAC + files       identical
p2 systems, optimize in place identical (D1)
p3 parallel outer, serial in  identical
p4 serial outer, parallel in  identical
p5 break, skip, failure       identical database; the dispatched iteration
                              after the break kept, not merged (by design)
============================  ==============================================

Differences that are the contract, not bugs: variables set in the body (and the
row variables of a rows loop) are not visible after a parallel loop; the order of
the table journal (merge bookkeeping). A loop whose iterations are *not*
independent (Read Structure overwriting the current system that iteration 1
created) differs, as documented in the Loop's user guide.

Timing: each iteration pays ~5 s of evaluator start-up, so the tiny MOPAC test
loops run slower in parallel (p1: 11 s serial, 21 s parallel with 2 at once).

Kill soak (``kill_parallel.py``, local, 2026-10-04): every trial resumed and matched
the serial run -- p1 rows: parent 3/3, child 4/4, both 6/6 (rerun after the fix
below); p2 systems: parent 3/3, both 3/3; p3 parallel outer: parent 3/3, both 3/3;
p4 parallel inner: parent 3/3, both 3/3. Killed iterations were run again and
resumed from their own checkpoints; orphaned evaluators of a killed parent were
killed by the resumed parent's ``TaskSet`` before it reran them. Found: an evaluator
killed *as it exited*, after its iteration had finished, was counted a failed
iteration (return code -9) and dropped under *continue* -- now the iteration's own
final checkpoint decides, whatever the return code (loop_step 92d9ac8).

Live TinkerCliffs (2026-10-04), flowchart ``p8_tc`` (a parallel Foreach over SMILES:
From SMILES as a new system, a row, MOPAC PM7 optimization into the row, Write
Structure appending ``/all.sdf``), private venv ``/projects/seamm/psaxe/phase6``:

============================================  =========  ======================
run                                            elapsed    vs serial reference
============================================  =========  ======================
ref: serial, 1 core (SLURM 7855609)            1:48       --
inline: 4 at once in a 4-core job (7855610)    1:50       identical
queue: 1-core parent, iterations bundled 2     2:27       identical
per SLURM job (7855611 + bundles
7855614-17)
SEAMM_DEV job 4013 via the JobServer,          0:58       identical
4-core, 1-minute walltime (finished as the
walltime hit; JobServer trusted job_data)
SEAMM_DEV job 4014, 24 molecules, 1-minute     3 attempts identical to ref24
walltime: TIMEOUT, resubmit, resume at 1/24;   (SLURM     (7855648, 1:59) but a
TIMEOUT, resume at 19/24; COMPLETED            7855649,   ``mopac.end`` of a
(``resubmit_count`` 2)                         -56, -58)  killed MOPAC run
============================================  =========  ======================

The iterations are short (about 10 s of MOPAC each), so evaluator start-up hides
any speed-up; the runs validate correctness, placement and the queue bundling,
not performance. **The live validation (D8) is complete** for inline placement,
iterations bundled as queue jobs from a parent on a compute node, and the
JobServer's resubmit-and-resume of a parallel loop. Separate placement (the
children's codes as tasks on the target) is not yet tested live.

The two remaining A/B cases (2026-10-04): **p6, whole-database snapshot** -- a loop
over systems with each iteration given the whole database (each baseline holds all
six systems): identical to serial. **p7, two iterations setting the same cell** of a
row that existed before the loop: by default the parallel job stops with
``MergeConflict`` at iteration 2 (iteration 1 merged and committed, iteration 2's
merge rolled back; the message now names the Loop setting, loop_step b7423f1);
with "the later iteration wins" it is identical to serial, with a warning per
overwritten cell (in the log, not ``job.out`` -- a review point).

Left to clean up after the review: ``[tinkercliffs_phase6]`` in
``~/SEAMM_DEV/PaulVT.local.ini`` (backup ``PaulVT.local.ini.bak-2026-10-04-phase6``;
restart the services after); TinkerCliffs ``/projects/seamm/psaxe/phase6``;
SEAMM_DEV jobs 4013 and 4014 (project ``test``); local ``Testing/phase6/runs`` and
``~/SEAMM_DEV/venvs/phase6-B``.
