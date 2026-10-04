Phase 5 notes
=============

2026-10-04 -- design note (draft for Paul and the design session)
-----------------------------------------------------------------

Phase 5 makes a flowchart resumable: after a crash, a walltime kill, a lost node or
a JobServer resubmit, a rerun in the job's own directory continues at the first
unfinished step (or Loop iteration) instead of starting again from the top. Nothing
is coded yet. This note records the survey, the proposed design and the decisions
Paul needs to make, marked **D1** to **D7**.

Inputs carried in from earlier phases: the ``seamm.step_completed(node)`` hook
(``seamm/checkpoint.py``, called by ``exec_flowchart`` *and* inside ``Loop.run``);
the "no checkpoint" branch already released in seamm_exec 2026.10.4 (seamm_exec#41:
archive ``seamm.db*``/``references.db`` to ``previous/<UTC>/``, keep the step
directories so task manifests reuse finished calculations); tables in ``seamm.db``
with the current row in the registry; the ``_table_changes`` journal; the
NOTES_phase4 review list (append_row/set_cell-on-NULL not idempotent, the Loop's
in-memory row snapshot, per-operation molsystem commits).

Survey (2026-10-04)
-------------------

What a resume has to deal with, found by reading the code and an audit of the
plug-ins:

1. **molsystem commits after nearly every operation.** 42 ``commit()`` calls
   (19 in ``configuration.py``; the ``System.configuration`` setter commits), so a
   node killed half way leaves its earlier writes in the database. Every molsystem
   object reaches SQLite through the one connection made in ``SystemDB.filename``
   (``system_db.py:358``); ``seamm.Table`` and ``user_tables`` use it too. No code
   opens a second connection to the job's ``seamm.db``. The temporary in-memory
   databases (conformer_search, packmol, ligpargen) are separate connections.
   No ``with db:``, ``BEGIN`` or ``SAVEPOINT`` anywhere. Two places bypass or fight
   a long transaction:

   - ``_Table.__delitem__`` (drop a column) uses ``executescript``, which commits
     whatever is pending before it runs.
   - The ``with table:`` / ``with configuration:`` backup-and-restore blocks
     (``_Table.__enter__``/``__exit__``) restore after an exception with
     ``PRAGMA foreign_keys = OFF``, which SQLite silently ignores inside a
     transaction (verified on 3.12). Used by molsystem's own tests; no plug-in uses
     it, but it must still be correct.

   ``ATTACH`` inside a transaction works (verified). ``DETACH`` does not once the
  attached database has been read ("database ... is locked"; found by the code
  review), so a deferring ``SystemDB`` leaves it attached (molsystem, review fixes).

2. **The current system lives only in memory** (``SystemDB._current_system_id``,
   defaulting to the last system). The current configuration of each system and
   each table's current row are in the database.

3. **Variables are not all JSON.** JSON-safe: numbers, strings, lists, dicts,
   ``_loop_indices`` (a tuple), ``_row``, ``_model_chemistry``. Not JSON-safe but
   encodable: numpy scalars and arrays (``store_results`` "variable" puts these in
   for every non-scalar result), ``Path``\ s (Control Parameters "file"), NaN
   (NULL float cells). Objects: ``printer``, ``_system_db`` (the evaluator's own),
   ``seamm.Table`` handles (a name plus the SystemDB), ``_forcefield`` (a
   ``seamm_ff_util.Forcefield``, rebuildable from its file), and whatever a Custom
   step's ``exec`` leaves behind (modules, functions, anything). ``eval`` in
   ``Variables.value`` inserts ``__builtins__``.

4. **Who runs other nodes inside ``run()``.** The Loop runs its whole body inside
   one ``run()`` call. So do the code steps with sub-flowcharts (ORCA, MOPAC,
   Psi4, LAMMPS, Gaussian, VASP, xTB, FHI-aims, DFTB+, …) and the hosts Energy
   Scan, Reaction Path, Conformer Search, Thermomechanical, Diffusivity, Thermal
   Conductivity, Training, the ASE/geomeTRIC optimizers (which re-run their
   sub-flowchart for every geometry) and the generic Sub-flowchart step. Each
   hand-rolls its loop over sub-steps; there is no shared helper to hook. LAMMPS,
   Psi4 and MOPAC build one input from *all* their sub-steps and run once, so
   resuming "at sub-step 3" is not meaningful for them in general. Split and Join
   do nothing at run time; there are no if/while steps.

5. **Non-repeatable side effects** (would duplicate or corrupt if a node re-runs
   after doing part of its work):

   - database writes: new systems/configurations (the standard structure handling,
     ~40 plug-ins; NMS and Dimer Builder's ``create_combined_system``), table
     appends (Table "Append a row", Properties, Geometry Analysis, ``set_cell``
     with no current row);
   - files appended *outside* the step directory: Write Structure "append" to a
     job-root ``/name`` file (sdf/extxyz/cif), Gaussian's basis-set append;
   - names made unique by checking the disk: ``Loop.safe_filename`` picks
     ``name_2`` when ``name`` exists, so a resumed iteration would get a new
     directory;
   - ``references.db`` is deleted at job start, and ``job.out``,
     ``iteration.out`` and ``step.out`` are truncated when (re)opened;
   - harmless: timing CSVs in ``~/.seamm.d/timing``, appended ``stderr.out``,
     the Table step's "save every N" counter and Golden Compare's pass counts
     (node state lost on resume).

   No node writes the datastore or the Dashboard; only the evaluator does, at the
   start and the end.

6. **Warm engines** (MDI for Energy, NMS, Dimer Builder, LAMMPS QM) live for one
   ``run()`` call. A re-entered node starts a new engine; only the time is lost.
   The batch path already resumes through ``tasks/manifest.json`` + ``DONE``.

7. **The JobServer's resubmit-on-loss never fires today.** The evaluator writes
   ``job_data.json`` with ``state: started`` when it begins (and the JobServer
   writes it at submit). ``_read_job_data_state`` returns that, any non-``None``
   state counts as the job's conclusion, so a walltime-killed SLURM evaluator is
   finalized with status ``started`` and dropped. A local job whose process died
   is finalized the same way. The design doc's "the JobServer becomes correct
   without change" is wrong: it needs a small change (below).

8. **The Loop already freezes its items.** Every loop type computes its list
   once, when ``run()`` is entered (``table_rows``/``table_indices`` for a table,
   ``select_configurations`` for the database, the values otherwise), never per
   iteration. Freezing the list in the checkpoint therefore reproduces today's
   semantics exactly, including a body that appends rows to the table being
   looped over (the new rows are not visited).

9. **SQLite with a long open write transaction** (verified, 3.12, WAL): a second
   ``mode=ro`` connection reads the last committed state; uncommitted pages spill
   into the ``-wal`` file (20 MB written → 19 MB of WAL before the commit);
   ``PRAGMA wal_checkpoint`` from a read-only connection fails ("disk I/O
   error"). Nothing in the evaluator, the JobServer, the web UI or the Dashboard
   runs ``wal_checkpoint``.

The design
----------

The node is the transaction
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Instead of auditing and fixing every plug-in's database writes for re-entry, make
them atomic per node:

- The job's ``SystemDB`` connects with ``sqlite3.connect(..., factory=JobConnection)``,
  a ``sqlite3.Connection`` subclass whose ``commit()`` is a no-op while a node is
  running. molsystem's 42 ``commit()`` calls are untouched.
- ``seamm.step_completed(node)`` does the one real commit, and in the same
  transaction writes the checkpoint row (below). The database and the checkpoint
  can never disagree: there is no window in which the database is a node ahead.
- A process killed mid-node loses exactly that node's database writes, by design:
  SQLite discards the uncommitted transaction. Its files (step directory, task
  manifests, ``DONE`` markers) survive, so the re-entered node regenerates its
  inputs and its TaskSet reuses every finished calculation.
- Prototyped on the SEAMM_DEV Python (3.12, WAL): writes and ``CREATE TABLE`` after
  the deferral are gone after ``os._exit``, present after the commit.

Consequences, stated so they are not surprises:

- **Readers see the last finished node.** In WAL mode the web UI, the Dashboard and
  the MCP server reading a running job's ``seamm.db`` see the state as of the last
  ``step_completed``. A node running a TaskSet for hours shows nothing new until it
  finishes. Loop bodies commit per body node, so loops stay visible iteration by
  iteration (the phase 4 behaviour). HISTORY says so, and the web UI's job page
  labels a running job's tables and structures "as of the last finished step".
- **The WAL grows during a long node.** SQLite cannot checkpoint the WAL past an
  open transaction, so a node writing thousands of configurations (Dimer
  Builder, NMS, Extract Clusters) grows ``seamm.db-wal`` to about the size of its
  writes until ``step_completed``; it is folded back after the commit. Harmless
  on disk. Nothing may run ``PRAGMA wal_checkpoint`` mid-node.
- **One writer per file** (already the rule) is now enforced by SQLite's lock: a
  second connection writing the job's ``seamm.db`` mid-node would wait 10 s and
  fail. None exists today; the temporary in-memory databases are separate files.
- **molsystem changes:** ``__delitem__`` without ``executescript`` (statements one
  by one, ``PRAGMA defer_foreign_keys``), and the ``with`` blocks use
  ``SAVEPOINT``/``ROLLBACK TO``/``RELEASE`` when the connection is deferring (the
  PRAGMA-based restore is wrong inside a transaction). ``JobConnection`` lives in
  molsystem; ``SystemDB(filename, deferred_commit=True)`` selects it; the default is
  unchanged, so every other user of molsystem behaves as today.
- **D1 -- a node that raises.** Uncaught (the job fails): roll back that node's
  writes, so the database matches the checkpoint and a rerun re-enters the node
  cleanly. Today the partial writes are committed; after this the failed node's
  files and ``final_structure.*`` (written from the same connection before the
  rollback) remain for debugging, its rows and structures do not; so do any
  other files the failing node wrote, such as a Write Structure file (see the
  append fix below). Caught by a Loop
  with ``errors = continue``: **commit** the partial writes as today and record the
  iteration as failed, so a loop's surviving behaviour does not change.
  *Recommended as stated; the alternative is to roll back in both cases.*

Granularity: nodes and Loop iterations, not sub-steps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A checkpoint unit is a node of the flowchart or of a Loop body, recursively
(nested loops). The sub-steps of a code step (ORCA's Energy/Optimization/…) and the
bodies of the other hosts in survey item 4 are *not* units: such a node is one
transaction and is re-entered as a whole.

- Why: 45 plug-ins each run their own sub-step loop, several build one input from
  all their sub-steps, and the ASE/geomeTRIC optimizers call their sub-flowchart
  from inside an optimizer's callback. A per-sub-step unit would need resume
  support in every one of them, and committing per sub-step without it would
  break the node-is-a-transaction rule.
- What makes it cheap anyway: converted codes (ORCA, MOPAC, Energy, BSSE, Dimer
  Builder, NMS, MBE) reuse every finished calculation through their manifests, so
  re-entering a 5-sub-step ORCA node redoes only the analysis.
- What it costs: unconverted codes (Gaussian, Psi4, LAMMPS, VASP, …) recompute
  the whole node on re-entry. That is correct, just slow; each code gets the task
  layer on its own merits (the phase 1 rule), not as part of this phase.
- The generic Sub-flowchart step could become a unit like the Loop later; not in
  this phase.

What the checkpoint holds
~~~~~~~~~~~~~~~~~~~~~~~~~

One JSON document, stored in a one-row ``_checkpoint`` table in ``seamm.db``
(authoritative, written in the node's transaction) and mirrored to
``<job>/checkpoint.json`` after the commit, for people only. A crash between the
two leaves the mirror one node behind (or absent after the first node), so no
decision is ever taken from the mirror: the start-up branch choice opens the
existing ``seamm.db`` read-only and reads ``_checkpoint`` (see *Resuming*):

.. code-block:: text

   {
     "format": 1,
     "state": "running",                      // running | finished | error
     "flowchart_digest_strict": "…",
     "command_line": ["…"],
     "versions": {"seamm": "…", "loop_step": "…"},
     "written": "2026-10-04T12:34:56Z",
     "position": [                            // a stack, outermost first
       {"node": "3", "loop": {"type": "For rows in table", "iteration": 17,
                               "length": 40, "items": [11, 12, …],
                               "directory": "iter_0017", "done": false}},
       {"node": "3.iter_0017.2", "next": "3.iter_0017.3"}
     ],
     "system_id": 1,
     "variables": {"…": "…"},
     "unrestorable": {"_forcefield": {"type": "Forcefield", "set_by": "2"}},
     "failed_iterations": {"3": [5, 9]}
   }

- **Position.** The innermost frame names the next node to run; the Loop frames
  above it carry each loop's iteration, its *frozen item list* (the row ids for a
  loop over a table, the configuration ids for a loop over the database, the
  values otherwise, computed once at loop entry and never recomputed, because the
  body may have changed the table or the database since) and its directory name
  (so ``safe_filename`` is never consulted on resume). ``done: true`` with no
  inner frame means the iteration finished and the loop continues at the next.
- **Variables** go through a small tagged JSON codec in ``seamm``: numpy arrays
  and scalars, ``Path``, tuples, sets, NaN/±inf, ``datetime``, and ``seamm.Table``
  handles (by name). Skipped: only the evaluator's own ``printer`` and
  ``_system_db`` (it remakes them) and ``__builtins__``. Anything else, including
  the modules and functions a Custom step's ``exec`` leaves behind, is recorded
  in ``unrestorable`` with its type and the id of the node that set it, so a
  later step that uses it gets the clear error below, not a ``NameError``.
- **D2 -- unrestorable variables.** Proposed: do not refuse the checkpoint. A node
  may implement ``checkpoint_variable(name, value) -> json`` and
  ``restore_variable(name, data) -> value`` for the objects it makes (Forcefield
  does: it saves the file name and rebuilds); ``Variables`` records which node set
  each variable so the right node is asked. A variable nobody can restore is
  replaced by a marker that raises a clear error if a later step reads it ("the
  variable '_x' (a Foo set by step 4) could not be restored when this job resumed;
  rerun it without --resume"). A job whose later steps never touch
  it resumes normally. *The design doc's alternative was to refuse the checkpoint
  up front; that would disable resume for every flowchart with a Custom step.*
- **Current system** id, restored before the first node runs.
- **What is not in it:** tables, structures and properties (already in the
  database), files (already on disk), task state (manifests).

Writing it
~~~~~~~~~~

``step_completed(node)`` (both callers) serializes the variables, builds the
position from the evaluator's and the Loops' frames, writes the row, commits,
then writes the mirror (``checkpoint.json.tmp`` + rename). The Loop also calls a
``seamm.iteration_started``/``iteration_finished`` pair so the frame is correct at
iteration boundaries. At the end of the run the state becomes ``finished`` or
``error``.

No checkpoint is written (and no resume is possible) for ``--read-only``,
``:memory:`` and a ``--database`` outside the job directory, the same cases in
which the 2026.10.4 archive rule leaves the database alone.

Cost: one JSON dump of the variables per node. The phase 4 soak measured 1.15 s per
loop iteration with a commit per node; the gate is that ``timing_noindex.flow`` and
``soak_loop_store.flow`` stay within 5 %. A flowchart that keeps a large array in a
variable pays for it at every node, so the soak includes one carrying a ~1 MB
numpy variable through a loop; if its cost shows, cache the encoded value and
re-encode only changed variables.

Resuming
~~~~~~~~

Resume is **explicit** (D4, revised after the design review): a plain rerun in
place never silently skips steps. The digest and the command line do not cover
the *input files* a flowchart reads (Read Structure's file, Control Parameters'
files, a forcefield file), so an automatic resume after the user replaced
``structure.xyz`` would skip Read Structure and carry on with the old structure.

At start, ``ExecFlowchart.run`` opens an existing ``<job>/seamm.db`` read-only and
reads its ``_checkpoint`` row (falling back to ``checkpoint.json`` only when the
table is absent, i.e. a database from before phase 5), then:

1. **Resume requested** (``--resume`` on the command line, or ``SEAMM_RESUME=1``
   in the environment) **and** a checkpoint in state ``running`` or ``error``
   with the same strict flowchart digest and command line: resume.
2. **Resume requested but not possible** (no checkpoint, a ``finished`` one, or a
   different flowchart or command line): archive and run from the top, saying
   why in ``job.out``.
3. **No resume requested:** archive the previous database (and
   ``checkpoint.json``) to ``previous/<UTC>/`` and run from the top, as 2026.10.4
   does. If the checkpoint was resumable, ``job.out`` says so first: "this
   directory had a resumable checkpoint (step 3, iteration 17 of 40); it was
   moved to previous/<UTC>/; rerun with --resume to continue instead". Nothing is
   lost: moving ``previous/<UTC>/seamm.db`` back and rerunning with ``--resume``
   resumes.

The JobServer's resubmit sets ``SEAMM_RESUME=1``. It is an environment variable
rather than a flag so that a remote venv still at 2026.10.4 ignores it and starts
from the top instead of failing on an unknown option. Because queue sections use
``export=NONE`` (SLURM) and PBS starts from a clean login, the JobServer writes
``export SEAMM_RESUME=1`` *into the batch script* on a resubmit, not into its own
environment.

A change of package versions since the checkpoint is printed as a warning, not
refused; rolling a venv back and resuming is a legitimate thing to do (tested).

This changes the 2026.10.4 rule in one respect, noted in seamm_exec's HISTORY:
the archive decision reads the database's ``_checkpoint`` row, not the existence
of ``checkpoint.json`` (which 2026.10.4 treated as "keep the database").

Resume itself:

- keep ``references.db`` (do not delete it at start); the re-entered node may
  count a citation twice, which is harmless;
- append to ``job.out`` under a banner ("Resumed at step 3, iteration 17 of 40, on
  <date>; previous attempt ended <state>"), and to the resumed iteration's
  ``iteration.out``;
- ``job_data.json`` keeps its ``start time`` and gains ``"resumed": [<times>]``;
- restore the variables, the current system, then walk the position: nodes before
  the outermost frame are skipped (their ``describe()`` output is still printed, as
  now), the Loop named by a frame restores its frozen state and re-runs its
  iteration set-up for that iteration (variables, current row, current
  configuration, directory: all idempotent once the names come from the
  checkpoint) and then starts its body at the frame's ``next`` node instead of
  the first;
- a node that was mid-flight is simply run again; its writes were rolled back and
  its tasks reattach or reuse their results.

Remaining non-database re-entry fixes (the audit)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With the database atomic per node, what is left is small:

- **Write Structure append** to a job-root file: record the file's size in the
  step directory before appending, truncate back to it on re-entry
  (read_structure_step). Same for Gaussian's basis-set append if it can append to
  a shared file (to check).
- **Loop directory names**: from the checkpoint (above).
- **references.db / job.out / iteration.out**: above.
- Accepted as harmless: timing CSVs, ``stderr.out``, the Table step's save counter,
  Golden Compare's counts.

The JobServer
~~~~~~~~~~~~~

- **Queue jobs (SLURM/PBS):** treat ``submitted``/``started``/``running`` in
  ``job_data.json`` as "no conclusion", so a job the scheduler reports ended
  without a terminal state goes down the existing resubmit path (``max_resubmits``,
  same directory) with ``SEAMM_RESUME=1`` in the batch script.
- **Staging order for ssh targets.** After a walltime kill the *remote* directory
  is the newer copy. The resubmit path already stages out (remote → local) before
  it stages in (local → remote), both ``rsync -a`` without ``--delete``, so after
  the stage-out the local copy has the same sizes and times and the stage-in
  overwrites nothing. One wrinkle: a file the job *deleted* remotely but that
  still exists locally (a pruned task directory, a skipped iteration's
  directory) is pushed back. The manifests tolerate that (fingerprints and
  ``DONE`` decide, not a directory's presence); the live test checks it.
- **Bounded damage before phase 5 is installed everywhere.** With the JobServer
  change, a lost job whose *remote* venv predates phase 5 ignores
  ``SEAMM_RESUME`` and reruns from the top, up to ``max_resubmits`` times
  (3 on ChemAI), each a full walltime. Recommendation: release the JobServer
  change last, and set ``max_resubmits = 1`` on any queue whose remote venv is
  older until it is upgraded.
- **D3 -- local jobs.** A local evaluator that died (machine reboot, OOM kill)
  with ``state: started`` is today finalized as ``started`` (a bug either way).
  Proposed: finalize it as ``error`` by default, and add an opt-in section key
  ``resubmit_lost = yes`` that resubmits it like a queue job, so a laptop that
  rebooted resumes its jobs when the JobServer comes back. *Default unchanged
  apart from the status fix, per the rollout rule.*
- Two subtleties to keep: the resubmit counter must not reset on resume (it does
  not today), and a walltime kill must not count as a ``failed`` task in the
  evaluator's manifests (a killed bundle job is "lost", which is retried).

Phase 6 compatibility
~~~~~~~~~~~~~~~~~~~~~

A parallel-Loop iteration is a child evaluator with its own ``seamm.db``; it uses
the same ``_checkpoint`` row and ``checkpoint.json``, unchanged. The parent's Loop
frame then holds per-iteration states (pending/running/done/failed) instead of a
single ``iteration``; the frame format above already has the frozen item list
and ``failed_iterations``, so phase 6 adds a field, not a format.

Decisions for Paul
------------------

- **D1** A node that raises and is not caught: roll back its writes (recommended)
  or commit them as today. Caught by a Loop with ``errors = continue``: commit as
  today (recommended).
- **D2** Unrestorable variables: per-node restore hooks + a marker that fails only
  if read (recommended), or refuse the checkpoint.
- **D3** Local jobs that die: ``error`` by default + opt-in ``resubmit_lost``
  (recommended), or resubmit by default.
- **D4** Resume is explicit: ``--resume`` for people, ``SEAMM_RESUME=1`` written
  into the batch script by the JobServer's resubmit; a plain rerun in place
  archives as today and says a resumable checkpoint was set aside (recommended,
  the design session's proposal). Alternative: automatic resume with
  ``--from-the-top`` to override, which then must *refuse* when any file named by
  a file-type parameter of a completed node is newer than the checkpoint, and
  print a loud banner. Paul's ``rm -fr tmp/*`` test idiom is unaffected either
  way.
- **D5** Granularity: nodes and Loop iterations only; sub-steps of code steps and
  other hosts are re-entered whole (recommended).
- **D6** Deferred commit is on for every job database the evaluator owns (the
  file in the job directory), with no switch; rollback is the venv rollback as in
  phase 4 (recommended). It changes when a running job's writes become visible
  (end of node, not mid-node).
- **D7** Validation targets: SEAMM_DEV soak (below) and the real-resubmit test on
  TinkerCliffs through the SEAMM_DEV JobServer, including the staging order;
  ARC-alone as a re-``sbatch`` of the same script in the same directory with
  ``--resume``. ChemAI not involved.

Packages, release order, tests
------------------------------

Release order (shared libraries first, pinned): molsystem (``JobConnection``,
``deferred_commit``, savepoint ``with`` blocks, no ``executescript``) → seamm
(checkpoint write/read, variable codec, ``Variables`` origin tracking, node
restore hooks, ``--resume``) → seamm_exec (the three branches, resume walk,
``references.db``/``job.out``/``job_data.json``) → loop_step (frames, frozen items,
names) → read_structure_step (append offset), forcefield_step (``_forcefield``
restore) → seamm_jobserver (non-terminal states, ``resubmit_lost``).

Tests:

- unit: molsystem deferred commit + rollback on ``os._exit`` in a subprocess,
  savepoint ``with`` blocks with foreign keys on; seamm codec round trips; seamm_exec
  branch choice and the resume walk with fake nodes; loop_step resume mid-body,
  at an iteration boundary, nested, every loop type, with a body that appends rows
  to the table being looped over; jobserver non-terminal state → resubmit.
- **kill-anywhere soak** in ``~/SEAMM_DEV`` (extends ``Testing/phase4/soak``): a
  harness that runs a flowchart uninterrupted once, then N times with
  ``kill -9`` after a random delay, rerunning in place until it finishes, and
  diffs every results file, table export and the ``seamm.db`` contents (systems,
  configurations, properties, tables) against the uninterrupted run (each rerun
  with ``--resume``; one more trial reruns *without* it and checks the archive
  message and a clean from-the-top result). Flowcharts:
  soak_kill, soak_nested, soak_loop_store, soak_props_geom, an ORCA one (task
  reuse), one each using Forcefield + LAMMPS and a Custom step (D2), and one
  carrying a ~1 MB numpy variable (encode cost). Plus: resume after a venv
  rollback (the versions warning), and reading ``_checkpoint`` from a second
  read-only connection while a node's transaction is open.
- A/B corpus run (phase 4 harness) for the deferred-commit change alone: results
  must be identical with no kill at all.
- live: a SEAMM_DEV flowchart on ``tinkercliffs_debug`` with a walltime shorter than
  the job, so SLURM kills it, the JobServer resubmits and it resumes to the same
  results; and the same on TinkerCliffs alone by re-``sbatch``.

Review (design session, 2026-10-04)
-----------------------------------

The design session reviewed the draft. Every point is folded in above:

1. *Must:* branch selection reads the database's ``_checkpoint`` row, never the
   mirror (a crash between commit and mirror would otherwise archive a resumable
   database); noted as a change to the 2026.10.4 rule.
2. *Must:* the stale-input trap of automatic resume; resume made explicit
   (``--resume`` / ``SEAMM_RESUME=1``), D4 revised.
3. Frozen item lists reproduce today's semantics: verified, survey item 8.
4. The WAL during a long node and readers during an open transaction: verified,
   survey item 9 and the consequences list.
5. A Custom step's modules and functions go to ``unrestorable``, not skipped.
6. Staging order for ssh resubmits stated; the deleted-file wrinkle found while
   checking it.
7. Bounded damage while remote venvs predate phase 5: release the JobServer last,
   ``max_resubmits = 1`` meanwhile.

Nits adopted: venv-rollback resume test, a ~1 MB numpy variable in the soak,
Write Structure files of a failed node listed with D1.

Design-session recommendations on the decisions: D1, D2, D3, D5, D6, D7 as
recommended; D4 explicit (now the recommendation above).

Decisions (Paul, 2026-10-04)
----------------------------

D1 to D7 agreed as recommended: roll back an uncaught failing node, commit when a
Loop continues (D1); per-node restore hooks and a marker that fails only if read
(D2); dead local jobs ``error`` plus opt-in ``resubmit_lost`` (D3); explicit
resume, ``--resume`` / ``SEAMM_RESUME=1`` (D4); nodes and Loop iterations only
(D5); no switch, venv rollback (D6); validation in SEAMM_DEV, TinkerCliffs through
the SEAMM_DEV JobServer, and TinkerCliffs alone (D7).

Implementation (2026-10-04)
---------------------------

Local commits on ``dev`` (not pushed), in release order:

- **molsystem** e15be3d, ad24ed7: ``JobConnection`` (a ``sqlite3.Connection``
  subclass whose ``commit()`` does nothing while ``deferring``; ``commit_now``),
  ``SystemDB(deferred_commit=True)``, ``commit_transaction()``,
  ``rollback_transaction()``; ``with`` blocks use savepoints when deferring;
  column deletion by ``ALTER TABLE DROP COLUMN``; a Configuration's ``with``
  makes its cell row first. While deferring a transaction is always open (see
  *Findings*). Default behaviour unchanged.
- **seamm** 32ab81e, fbf4528: ``seamm/checkpoint.py`` -- ``Checkpointer`` (frames,
  ``start``, ``loop_resume``, ``enter_iteration`` which writes,
  ``iteration_failed``, ``leave_loop``, ``step_completed``, ``finish``), the
  variable codec, ``Unrestorable``, ``read_checkpoint``, ``resumable``,
  ``find_node``; ``Variables`` records the uuid of the step that set each
  variable; ``Node.checkpoint_variable``/``restore_variable``.
- **seamm_exec** 90d09b5: ``plan_start`` (the branch choice, reading the
  ``_checkpoint`` row read-only), ``--resume``/``SEAMM_RESUME``, the deferred job
  database, the checkpointer around the run loop, rollback on error after the
  final structure is written, ``job.out`` appended and ``job_data.json``
  ``resumed``/``first start time`` on resume; a relative ``--database`` is
  relative to the job directory.
- **loop_step** 70e4e80: state saved at each iteration's start; an ``advance``
  flag so a resumed iteration is set up again rather than advanced; body started
  at the unfinished step; ``iteration_failed`` for continue/exit.
- **read_structure_step** 87ea2c9: Write Structure records the size of each file
  it appends to (``appended_files.json`` in its directory) and cuts the file back
  when it runs there again.
- **forcefield_step** 6ea4c26: ``_forcefield`` saved as file + forcefield name.
- **seamm_jobserver** ca83a11: only a terminal ``job_data.json`` state concludes a
  job; resubmitted batch scripts export ``SEAMM_RESUME=1``; dead local jobs are
  ``error``, or resubmitted with ``--resubmit-lost``.

Tests: molsystem 340 (11 new), seamm 295 (33 new), seamm_exec 127 (new
``test_resume.py``), loop_step 22 (crash-and-resume against an uninterrupted
run: For, Foreach, float, table whose body appends rows, systems with named
directories, nested at 8 positions, twice, errors continue/exit; database and
directories identical), read_structure_step 3 new, forcefield_step 2 new,
seamm_jobserver 102 (10 new). Test venvs ``~/SEAMM_DEV/venvs/phase5-A``
(released) and ``phase5-B`` (A + the seven checkouts, editable); the live
SEAMM_DEV venv is untouched.

Findings
~~~~~~~~

- **DDL commits itself.** The sqlite3 module opens a transaction implicitly only
  before INSERT/UPDATE/DELETE/REPLACE. A step whose first write after a commit was
  ``CREATE TABLE`` (a Geometry Analysis table) committed it at once, and a kill left
  an empty, unregistered user table that shifted later tables' SQL names. Found by
  the kill soak (``soak_props_geom``, 1 trial in 6); fixed by keeping a
  transaction open while deferring (molsystem ad24ed7, with a test that fails
  without it).
- **SMILES → 3D with RDKit was not resumable.** RDKit's embedding (molsystem's
  default flavor) carries its random state from call to call, so the same SMILES
  gave a different conformer depending on what the process had built before:
  reproducible from a fresh start, but a resumed run (and phase 6's parallel
  iterations) built different, equally valid structures after the resume point
  (MOPAC energies differing by ~1 kcal/mol). OpenBabel's builder is not affected
  (checked; an earlier draft of this note said it was). **Paul, 2026-10-04: fix
  it** -- molsystem 9c9a31f gives the embedding a fixed seed, so a SMILES always
  gives the same structure. This changes the conformers SMILES-built flowcharts
  produce compared with earlier releases (HISTORY must say so). Follow-up:
  structure_step embeds stereoisomers the same way (``rdDistGeom.EmbedMolecule``
  without a seed). The soak flowcharts read pre-built SDF files either way.
- **Read Structure and a MOL block without ``$$$$``.** An ``.sdf`` holding a single
  MOL block without the terminator counts as 0 records, and the default indices
  ``1:end`` then fail with "If stop < start, the step must be negative: 1".
  Pre-existing; to file.
- (Corrected later: forcefield_step's suite passes, 231 tests, when run from its
  own directory, as CI does. The 223 errors came from running it elsewhere, where
  the fixtures' relative data paths do not resolve and seamm_ff_util falls into a
  branch that calls a ``Forcefield._create_file`` it does not have -- a small
  latent seamm_ff_util bug, not a phase 5 matter.)
- **The strict digest blocks resuming after any update.**
  ``Flowchart.digest(strict=True)`` hashes each step's version, so a resume after a
  venv update or rollback (even a dev commit, in the soak) was refused as "the
  flowchart has changed". And ``Flowchart.digest`` follows the steps with
  ``next()``, which stops at the first Loop: changes inside or after a loop do not
  change it. The checkpoint now matches on its own ``flowchart_fingerprint``
  (every node's parameters and every edge, no versions; seamm 7960d35), and a
  resume notes the packages whose versions changed (seamm eabf0bb, seamm_exec
  c2ec57c). ``Flowchart.digest`` itself is unchanged (it is recorded in
  ``job_data.json``); its stopping at Loops is a pre-existing bug to file.
- **Evaluator kills used up task attempts.** Three kills during the same ORCA step
  exhausted its ``max_attempts`` (3): the resumed step refused to run, the Loop
  (continue on errors) carried on, and the job finished with the row missing. A
  local task stopped because the evaluator stopped is now recorded as not counted
  (seamm_exec cb4b724); a task a queue lost still counts. How often a job comes
  back is the JobServer's ``max_resubmits``.
- **Node uuids are not stable across runs.** Reading a flowchart gives its steps
  new uuids, so a variable's origin saved as a uuid found no step on resume and the
  Forcefield's ``_forcefield`` came back unrestorable (clear error, as designed,
  but wrong). Origins are saved as the steps' ids from the flowchart's numbering
  (seamm bf4dbd4). The seamm unit test restored in the same process and missed
  it; it now rebuilds the flowchart first.
- The harness itself: a trial that had finished was rerun once more, which (as
  a rerun without a resumable checkpoint) started from the top and, for loops
  naming directories after rows, made ``mol0001_2`` directories -- correct
  behaviour, pre-existing, not a phase 5 issue. Fixed in the harness.
- **A large variable made quick loops slow.** The A/B run of ``soak_custom`` (a
  1 MB numpy array in a variable, 200 iterations of two quick steps) took 72.9 s
  against 16.7 s released: every checkpoint encoded the array as a JSON list and
  wrote it into ``seamm.db`` and ``checkpoint.json``. Variables now live one per
  row in ``_checkpoint_variables``, rewritten only when they change (a numpy
  array is fingerprinted by a hash of its bytes, so an unchanged one is not even
  encoded), arrays are stored as base64 bytes (bit-exact), and ``checkpoint.json``
  only summarizes values over 2000 characters (seamm 866cc6e). Now 10.3 s against
  16.2 s released: deferring molsystem's per-operation commits to one per step
  makes such loops faster than before.

Soak and A/B results (2026-10-04)
---------------------------------

Kill-anywhere soak (``Testing/phase5/kill_harness.py``: ``kill -9`` on the
evaluator's process group at random times, up to three times per trial, rerun with
``--resume``; every table of ``seamm.db`` but the checkpoint, the result files and
the directory tree compared with an uninterrupted run), after the fixes above:

=============================== ======== =====================================================
Flowchart                       Trials   Result
=============================== ======== =====================================================
soak_kill (MOPAC, table)        8        identical
soak_nested (nested loops)      8        identical
soak_loop_store (table rows)    8        7 identical; 1 refused to resume (strict digest, fixed)
soak_props_geom (properties,    8        identical (after the DDL fix)
geometry analysis tables)
soak_custom (Custom step, 1 MB  6 + 4    identical
numpy variable)
soak_ff_lammps (Forcefield      6        identical (after the origins fix)
object, LAMMPS)
soak_orca (ORCA opt + energy)   6        all rows; one energy 1e-9 Eh apart (ORCA restarts
                                         from the killed run's .gbw); ORCA scratch files of
                                         killed runs left behind (cosmetic)
phase 4 soak_kill (from SMILES) 6        identical (after the RDKit seed)
=============================== ======== =====================================================

A/B without kills (``Testing/phase5/ab.py``, phase5-A released vs phase5-B, 17
flowcharts): identical apart from the new ``checkpoint.json`` and
``_checkpoint_variables``, except the flowcharts that build structures from
SMILES, which differ by design since the seed (other conformers; same rows, same
exit codes). Timing: no measurable cost -- ``p5_loop_store`` three repeats 41.3/35.9,
35.6/35.8, 35.8/35.6 s (A/B), ``p5_orca`` 96.6/98.1 s, ``p5_ff_lammps`` 26.7/25.0 s,
``p5_custom`` 16.2/10.3 s; run-to-run noise is about 10 %.

**Correction (2026-10-04, from the live TinkerCliffs test).** The fingerprint as
first written (seamm 7960d35) hashed the steps' *uuids*, which are new each time a
flowchart is read: every real resume, in a new process, was refused ("the flowchart
has changed") and ran from the top. Those runs still finished with identical
results, because the steps' finished calculations were reused from the task
manifests, so the kill soaks run after 7960d35 (soak_ff_lammps and soak_orca
reruns, soak_custom, the SMILES soak) passed without resuming, and the table above
overstates them. The unit tests resumed with the same flowchart objects. Fixed in
seamm df0afbe (the fingerprint uses the steps' numbered ids); the seamm_exec and
loop_step resume tests now build the flowchart again before resuming (they fail
on the old fingerprint: 18 and 5 tests), and the kill harness now fails a trial
whose run.log shows a refused resume. The whole soak is being rerun.

Live TinkerCliffs, first attempt (with the uuid fingerprint): SEAMM_DEV job 4010 on
``tinkercliffs_phase5`` (3-minute walltime) was stopped by SLURM three times
(TIMEOUT), left ``state: started``, and was resubmitted by the JobServer each time
with ``SEAMM_RESUME=1`` in the script (``resubmit_count`` 3), finishing on the
fourth (SLURM 7851390/7851412/7851420/7851489). The JobServer half is therefore
validated; the evaluator half refused to resume (above). TinkerCliffs alone
(re-``sbatch`` with ``--resume``) likewise: 3 TIMEOUTs, then COMPLETED; results
identical to an uninterrupted reference run (SLURM 7851391, 10:26).

Live TinkerCliffs, second attempt (seamm df0afbe): TinkerCliffs alone resumed at
iterations 43 and 87 (two TIMEOUTs, then COMPLETED) and is identical to the
reference. SEAMM_DEV job 4011 resumed at iterations 37 and 75 (``resubmit_count``
2); its third attempt finished the flowchart and wrote ``finished`` just as the
walltime hit (SLURM recorded TIMEOUT), and the JobServer rightly trusted
``job_data.json``. Its staged-back ``seamm.db``, however, read as 74 of 120 rows:

- **A stale SQLite log after a second stage-out.** ``RsyncStager`` copies with
  ``rsync -a``, which never deletes. The stage-out before the second resubmit
  brought ``seamm.db-wal`` back; the last attempt then folded its log into
  ``seamm.db`` and removed it on exit; the final stage-out copied the new
  ``seamm.db`` but left the old ``-wal`` beside it, and SQLite replayed it over
  the newer database. Pre-existing (a Dashboard pulling a running job's files and
  then the final stage-out does the same), but resubmits make it routine.
  seamm_scheduler f557c92: a second rsync pass in ``stage_in`` and ``stage_out``
  with ``--delete`` restricted to ``*-wal``/``*-shm``/``*-journal``; a test runs
  rsync for real through a fake ssh. With the stale files removed by hand, job
  4011 is identical to the reference. **seamm_scheduler joins the phase 5 release
  set** (before seamm_jobserver).

Live TinkerCliffs, third attempt (with the staging fix, seamm_scheduler f557c92 in
SEAMM_DEV): job 4012 was stopped by the walltime three times and resumed at
iterations 40, 77 and 116 (``resubmit_count`` 3); its staged-back ``seamm.db`` is
complete with no stale log beside it, and the job is identical to the reference
apart from a ``mopac.end`` left by a killed MOPAC run. **The live validation (D7)
is complete:** JobServer → TinkerCliffs resubmit-and-resume (jobs 4011, 4012) and
TinkerCliffs alone with ``--resume`` both reproduce an uninterrupted run exactly.

**Kill soak, final (2026-10-04, after seamm df0afbe, with the harness failing any
trial whose rerun refused to resume):** every trial resumed and matched the
uninterrupted run -- soak_kill 6/6, soak_nested 6/6, soak_loop_store 6/6,
soak_props_geom 6/6, soak_custom 6/6, soak_ff_lammps 6/6, soak_orca 6/6, SMILES
soak_kill 6/6. Differences ignored as not data: the row order of
``_checkpoint_variables`` (rewritten on a resume's first checkpoint); ORCA scratch
files and ``mopac.end`` left by killed runs; ORCA timings; and in one ORCA trial the
molecule's name in ``Thermochemistry.txt`` ("ethane" vs "Formula: C2H6"), which
comes from a PubChem lookup over the network that failed in that attempt.

Added during phase 5 (Paul OK, 2026-10-04): **a timed-out task gets more time when
retried** (reported by mbe from C.3b). A bundle whose tasks give no walltime asks
for twice their estimates plus ten minutes; when the queue stopped it for its time
limit, the task was resubmitted with the same time and could never finish (r2SCAN
ghost tasks with SlowConv). seamm_scheduler 77b31fa: ``JobStatus.timed_out`` (SLURM
TIMEOUT; PBS exit status -29 or a "walltime ... exceeded" comment). seamm_exec
52d2f40: the timeout is recorded in the task's history (``timed_out``), and each
retry after one asks for ``2**timeouts`` times the estimated time, within
``bundle_walltime``, both within a run and across runs; a walltime the task gives is
used as given; the prepared bundle is keyed on the scale. Test: 1200, 2400, 4800 s
for a task that times out twice.


Release preparation (2026-10-04, not pushed or tagged)
------------------------------------------------------

HISTORY entries, pins and documentation are prepared on ``dev`` for: molsystem
2026.10.4; seamm 2026.10.4 (``molsystem>=2026.10.4``); seamm_exec 2026.10.4.1
(``molsystem``/``seamm``/``seamm-scheduler>=2026.10.4``; a "Resuming a job" section
and the task retry rules in ``getting_started.rst``); loop_step 2026.10.4
(``seamm>=2026.10.4``); read_structure_step 2026.10.4.1; forcefield_step 2026.10.4;
seamm_scheduler 2026.10.4; seamm_jobserver 2026.10.4 (``seamm_scheduler>=2026.10.4``;
the user guide's resubmit section rewritten). read_structure_step and
forcefield_step drop ``devtools/conda-envs/test_env.yaml`` (uv CI). Versions are
dated today; bump them if the merge is on a later day. Release order (corrected by
the review: seamm_exec pins seamm-scheduler): molsystem, seamm, seamm_scheduler,
seamm_exec, loop_step, read_structure_step and forcefield_step (both pin seamm),
seamm_jobserver last; until a cluster's SEAMM has seamm_exec 2026.10.4.1 a resubmit
there reruns from the top, so set ``max_resubmits = 1`` on such queues until it is
upgraded.

Cleanup after the release (each needs Paul's OK)
------------------------------------------------

- ``~/SEAMM_DEV``: its venv is ``venvs/phase5-B`` (editable checkouts). After the
  release, update it to the released packages (or switch back to ``phase4-B`` and
  ``update --latest``), restart its services, and prune ``venvs/phase5-A`` and
  ``venvs/phase5-B``.
- ``~/SEAMM_DEV/PaulVT.local.ini``: remove the ``[tinkercliffs_phase5]`` section
  (3-minute walltime, for the live test); the file before it is
  ``PaulVT.local.ini.bak-2026-10-04-phase5``. Restart the JobServer after.
- TinkerCliffs: ``/projects/seamm/psaxe/phase5`` (private venv, source copies,
  ``remote_jobs/``, ``ref/``, ``alone/``, ``alone2/``, smoke test); 1.8 GB.
- SEAMM_DEV test jobs 4010-4012 (project ``test``) can stay or be deleted.
- ``Testing/phase5/runs`` and ``ab_runs`` (local, 306 MB and 114 MB) once the
  results above are no longer needed.


Code review (design session, 2026-10-04)
----------------------------------------

A review subagent read all eight packages at the release-preparation heads and
probed the risky paths. Fixed:

1. *Must:* ``Checkpointer.start()`` keeps only the outermost frame, so a resume that
   failed inside a Loop before it re-entered its iteration (an unrestorable
   variable in the loop's parameters, say) wrote that bare position with state
   ``error``, and the next resume restarted the loop at iteration 1 over the
   committed iterations: duplicate rows and ``_2`` directories. Now a failed resume
   that wrote nothing rewrites the checkpoint it resumed from, marked ``error``.
   Test: crash mid-loop, a resume that fails in ``Loop._restore_state``, then a
   good resume -- identical to an uninterrupted run (fails without the fix).
2. A variable that encodes but that JSON cannot hold (numpy ``datetime64``: its
   ``.item()`` is a ``date``) raised in ``step_completed`` and failed the job; it is
   now recorded as unrestorable. Test.
3. ``ExecFlowchart.run()`` making its own plan did so before ``set_ids()``, and the
   fingerprint of a flowchart without ids never matches; it sets them first. Test
   with the ids reset.
4. molsystem: ``detach`` while deferring leaves the database attached (above);
   dropping a column while deferring drops the indices on it first and needs SQLite
   3.35 (clear error otherwise). Tests.
5. Write Structure's ``appended_files.json`` was not tied to a run: a rerun from the
   top in place could cut a job-root file back to the previous run's sizes. The
   record now carries the checkpoint's ``run_id`` (new for a run from the top,
   kept through resumes; seamm ``Checkpointer.run_id``) and other runs' records,
   or a run without a checkpoint, are ignored. read_structure_step now pins
   ``seamm>=2026.10.4``. Tests.

Nits taken: the checkpoint's ``position`` is deep-copied; loop_step writes an
iteration's checkpoint before making its directory (a kill in between left a
directory the checkpoint did not know, so ``name_2`` after the resume), keeping a
resumed iteration's ``iteration.out``; loop_step's HISTORY says a failed iteration
under "exit the loop" also keeps its writes; the stager's side-file pass protects
directories (``--filter=P */``; rsync's ``--delete`` also removed empty directories
found only on one side) -- verified live to TinkerCliffs (GNU rsync 3.2.7) from the
Mac (openrsync) in both directions, and MolSSI10 has GNU rsync 3.2.3; the RDKit
seed changes no test that compares coordinates (forcefield_step_experimental's 25
failures and 5 errors are the same with the released packages; xnn embeds with RDKit
itself).

Known, not changed (documented here):

- A queue TIMEOUT seen only when a restarted evaluator reattaches is recorded as
  lost without ``timed_out``, so the time doubling does not apply to that retry.
- The doubled time is capped by ``bundle_walltime`` only, not by the queue's own
  maximum unless ``bundle_walltime`` is set to it.
- A job that finished but whose final ``job_data.json`` could not be staged back is
  resubmitted with ``SEAMM_RESUME=1``; the evaluator finds the checkpoint
  ``finished``, refuses to resume and runs from the top, up to ``max_resubmits``.
- gaussian_step's basis-set append (``energy.py``) is not made safe to repeat.
- The structured-dtype numpy arrays come back as raw bytes (``|V12``) and a
  ``str`` Enum as its repr; neither occurs in SEAMM's variables today.
- molsystem's ``with configuration:`` now creates the configuration's cell row (for
  every configuration used in such a block).

Released (2026-10-04)
---------------------

All eight on PyPI, in order: molsystem 2026.10.4 (PR #122), seamm 2026.10.4 (#223),
seamm_scheduler 2026.10.4 (#3), seamm_exec 2026.10.4.1 (#43), loop_step 2026.10.4
(#39), read_structure_step 2026.10.4.1 (#83), forcefield_step 2026.10.4 (#53),
seamm_jobserver 2026.10.4 (#26); every checkout synced with ``make update``.

On the way: two publish runs failed only on the macOS / Python 3.11 runner and
passed on a re-run of that job (molsystem: PubChem tests over the network;
seamm_exec: ``test_tasks_run_concurrently``, four 1-second tasks took 3.1 s against
a 2.5 s limit -- worth loosening). seamm_exec's PR CI first failed because PyPI's
*simple* index still listed seamm-scheduler only to 2026.10.3 minutes after the
release (the release's own JSON page was already there): wait for the simple index
before the next PR. loop_step's PR CI failed because its new resume tests import
seamm_exec, which it did not declare; it is now a ``test`` extra, and each later
package was first tested in a clean PyPI-only venv.

Not rolled out to any installation; that waits for Paul's ask, and so does the
cleanup above.

Cleanup done (Paul OK, 2026-10-04): SEAMM_DEV moved to the released packages in a
new versioned venv (``venvs/2026-10-04T15-17-18``, built from ``phase4-B`` with
``update --latest`` of the eight; ``phase5-A``/``phase5-B`` deleted, the older
versions kept); ``[tinkercliffs_phase5]`` removed from ``PaulVT.local.ini`` (back to
``local``, ``molssi10``, ``molssi10-tasks``) and the services restarted;
TinkerCliffs ``/projects/seamm/psaxe/phase5`` deleted (1.8 GB); local
``Testing/phase5/runs`` and ``ab_runs`` deleted (the harness, specs and SDF data
kept for phase 6). SEAMM_DEV jobs 4010-4012 are kept as the live-validation record.
