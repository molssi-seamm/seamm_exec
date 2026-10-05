Phase 7 notes
=============

2026-10-05 -- design note (draft for Paul and the design session)

Phase 7 of the parallel-execution campaign: the **TaskServer** (the machine-wide queue
for a computer without a queueing system), the **Dashboard task view**, the triage of
the items deferred in phases 1-6, what the PBS test site needs, and the rollout of
phases 6 and 7 together. PBS itself was done in phase 4/7 (2026-10-03, see the
campaign index). Nothing here is coded yet.

Scope
-----

From the campaign plan ("The TaskServer"; decision Q4: loopback only, reached through
an ssh port-forward) and the design session's list (2026-10-05):

1. A long-lived broker that owns a machine's cores **and memory** and takes the tasks of
   *every* evaluator on that machine, so two jobs on one Mac no longer oversubscribe it
   (the problem ``OMPI_MCA_hwloc_base_binding_policy=none`` only papered over), with
   memory a first-class resource (the 16 GB laptop incidents); reachable from a remote
   evaluator; surviving its own and the evaluators' restarts; and its relation to the
   JobServer's local queue.
2. A read-only Dashboard (web UI) task view from the files: per-step task counts and
   states, a parallel loop's iterations as rows, the "as of the last finished step"
   wording for a running job.
3. Triage of the deferred items.
4. PBS: what MolSSI10 needs to stay a useful test site.
5. A rollout plan for phases 6 and 7 together.

Survey (2026-10-05)
-------------------

- **The task layer already has the slot**: a target with ``tasks = taskserver`` reaches
  ``TaskSet.backend``, which raises "not yet" (``tasks.py``). Any ``TaskBackend``
  (``submit``/``status``/``cancel``/``fetch``, optional ``wait``/``adopt``/``reattach``/
  ``capacity``) plugs in there.
- **A queueing system is one module** in seamm_scheduler (``slurm.py``, ``pbs.py``, about
  500 lines each): directives, the submit/poll/cancel/find commands and their parsing,
  the environment a job sees. ``QueueBackend`` pairs it with a transport (local or ssh)
  and a stager (rsync, or none on a shared filesystem). The task layer's
  ``SchedulerBackend`` (bundles, the worker, adoption from manifest records, retries
  with ``2**n`` time) and the JobServer (``type = queue``) both drive any scheduler
  module through it, and both were validated over ssh against SLURM (TinkerCliffs) and
  OpenPBS (MolSSI10), including laptop sleep and lost connections.
- **The JobServer has no private state**: it reads and writes ``jobs`` in the
  datastore's ``seamm.db`` and rebuilds its in-memory view on restart from the pids and
  queue ids stored there; no HTTP or other IPC. Services are created by seamm-manager
  from a fixed list (``known_services`` in ``services.py``); launchd and systemd units
  are generic.
- **The web UI** (FastAPI + React) finds a job's directory from the datastore row,
  lists its files with an unbounded ``rglob`` (large for a parallel loop with many
  iterations), never reads ``job_data.json`` or any manifest, has no tabs on the job
  page and no auto-refresh (only a Refresh button, which first syncs a remote job's
  files back). Backend tests use ``TestClient`` with fake job directories; the frontend
  has no tests (CI builds and lints it).

The TaskServer
--------------

Two designs. **(A)** is the one decided in the campaign plan; **(B)** is proposed here
because the survey shows how much of it already exists. Paul chooses (D1).

**(A) As decided: an HTTP service.** ``POST /tasks``, ``GET /tasks/{id}``,
``GET /tasks/{id}/files/{name}``, ``DELETE /tasks/{id}``, ``GET /programs``; files in
the request, results fetched by GET; a pool and an SQLite queue inside the service;
bound to ``127.0.0.1``; a remote evaluator reaches it through ``ssh -N -L`` opened by a
new ``TaskServerClient`` back end. New: the service, the protocol, its client, file
transfer in the protocol, the tunnel's life cycle (a laptop that sleeps or changes
network drops it), adoption for the new back end.

**(B) Proposed: the TaskServer as a queueing system.** A small queue with a command
line in the spirit of ``sbatch``/``squeue``/``scancel``, and a ``seamm`` scheduler
module in seamm_scheduler that speaks it:

.. code-block:: text

    seamm-taskserver submit [--cores N --memory BYTES --time SECONDS --name NAME
                             --chdir DIR --output FILE] SCRIPT      -> prints the job id
    seamm-taskserver status [ID ...]          (JSON: state, exit code, reason, times)
    seamm-taskserver cancel ID ...
    seamm-taskserver find NAME                (ids of jobs with that name)
    seamm-taskserver queue                    (what is queued and running, for people)
    seamm-taskserver config                   (the machine's capacity)

Everything that drives SLURM and PBS then drives it unchanged, locally or over ssh:
bundling, the task worker, rsync staging, ``adopt`` after an evaluator restart, the
retry rules, the queue-full and transient-failure handling, and the JobServer
(``type = queue``, ``scheduler = seamm``) for whole jobs. Trust is the user's ssh key, as
Q4 intended, and stronger: nothing listens on any port, not even loopback, and there is
no tunnel to keep alive across sleep and network changes. The client side is one
scheduler module (~300 lines, simpler than PBS since we define the output format), plus
an entry in ``computational_environment()`` so a job sees its allocation.

The queue itself (both shapes need one):

- **State** in one SQLite file per machine, ``<root>/taskserver/queue.db`` (WAL; one
  short transaction per change), with each job's script, request, state, pid, process
  start time, exit code and times. A versioned schema, so an upgrade of the package never
  loses or misreads queued and running jobs.
- **Resources**: cores and memory, both first class. The machine's capacity comes from
  ``<root>/taskserver.ini`` (``cores``, ``memory``), defaulting to the physical cores and
  *half* the RAM (the machine-wide memory rule; the desktop applications and the
  evaluators need the rest). A job without a memory request is charged a default per
  core (2 GB). A request larger than the capacity is refused at submission, never run
  "alone".
- **Scheduling**: first fit in submission order (a small job may start beside a large one
  waiting for room), with the oldest waiting job reserving room after 30 minutes so a
  large job cannot starve.
- **Running a job**: in its own session, as ``seamm-taskserver run-job ID``, a small
  runner that sets up the environment (``SEAMM_TASKSERVER_JOB_ID``, ``..._NTASKS``,
  ``..._MEM``: ``computational_environment()`` reads them as it reads SLURM's), runs the
  script with the time limit (SIGTERM, then SIGKILL), records the exit code, and runs a
  scheduling pass so the next queued job starts.
- **No daemon** (D2). Every ``submit`` and ``status`` call also runs a scheduling pass,
  and so does every runner when it ends; a ``status`` pass also checks that each running
  job's runner is alive (pid and start time) and marks a vanished one ``lost`` (after a
  reboot, say). There is therefore nothing to keep running and no service to manage: the
  queue works whether or not anything is watching it, an upgrade or restart of anything
  never touches running jobs, and a laptop that sleeps simply resumes. *(Alternative: a
  ``taskserver`` service created by seamm-manager, KeepAlive, polling every second,
  which can enforce limits and start jobs without waiting for a call; the survey lists
  the six places in seamm-manager that would learn its name.)*
- **Where it lives**: in seamm_scheduler (console script ``seamm-taskserver``), which is
  already in every SEAMM venv, locally and on a remote workstation; no new package. It
  depends on nothing but the standard library and psutil, and runs scripts, not SEAMM
  code: it changes rarely.

**Targets** (evaluator side, in ``<host>.ini`` / ``target.json``)::

    [workstation]                 ; a second machine without a queue
    type = local
    tasks = queue
    scheduler = seamm
    transport = ssh
    host = workstation.local
    remote_root = /home/paul/seamm_tasks
    remote_python = /home/paul/SEAMM/venv/bin/python

    [local]                       ; this Mac: every job's tasks share its queue
    type = queue                  ; the JobServer submits the evaluators to it too
    scheduler = seamm
    transport = local
    tasks = queue

On the Mac, the JobServer's ``[local]`` section can stay ``type = local`` (evaluators as
subprocesses, tasks in each evaluator's own pool -- today's behaviour) or become
``type = queue, scheduler = seamm``, so the evaluators and all their tasks share one
accounting of the machine (D3). The JobServer then *is* a TaskServer client, through
the same scheduler module it already uses for SLURM and PBS; no new code in it.

**Adoption and restarts**: the evaluator's manifest holds the queue's job ids, so
``SchedulerBackend.adopt`` works as for SLURM. The queue survives its own "restart"
trivially (there is no process); runners are independent of evaluators.

**Validation** (D4): unit tests of the queue (scheduling, limits, lost runners, a
concurrent-submit soak against SQLite locking); the scheduler module against recorded
output, as for PBS; on the Mac, two jobs of concurrent MOPAC tasks through the JobServer
with ``[local] type = queue`` showing the core and memory limits held; one task run over
ssh to a second machine (paul.local, with Paul's OK) with staging; a kill soak of
evaluators and runners. All small (MOPAC, a few tasks at once).

Dashboard task view
-------------------

Read-only, from files, no new database (D5):

- **Backend**: ``GET /api/jobs/{id}/tasks`` walks the job directory for
  ``tasks/manifest.json`` (skipping ``_bundles``, bounded depth) and returns, per step
  directory, the counts by state and per task its key, state, attempts, back end and id,
  bundle and the reason of a failure; and for each parallel loop (a step directory whose
  iterations have ``_evaluator/``), one row per iteration: its directory, its state (from
  ``_evaluator/job_data.json``, read with the JobServer's header tolerance, or the
  checkpoint), whether it has been merged or failed (from the parent's checkpoint frame:
  ``next``, ``failed``, ``directories``), with links to its ``job.out`` and
  ``iteration.out``. Path safety through the existing ``_resolve_job_file``. A cheaper
  file list for big jobs (a depth or a subtree parameter on ``/files``) comes with it.
- **Frontend**: a Files | Tasks switch on the job page; the Tasks panel a table grouped
  by step, iterations as rows (phase 6's deferred "child rows" -- inside the job, not as
  datastore rows); refreshed every 15 s while the job is running. For a remote job the
  panel says "as of the last sync" and the existing throttled sync runs first; the
  database-derived panes say "as of the last finished step" (a running job's database
  shows only finished steps).
- **Tests**: backend tests with fake job directories (manifests, iterations, a parallel
  checkpoint); the frontend is checked live in a browser with a real parallel-loop job
  (Playwright, as the web UI's earlier features were).

Deferred items
--------------

=====================================  =====  =============================================
item                                   what   why / how
=====================================  =====  =============================================
Dimer builder wall-walk per-point      defer  Dimer-builder work, not task-layer; with its
TaskSets on a queue                           next campaign.
MOPAC ``success.dat`` skip-rerun vs    do     A stale ``success.dat`` must not skip a task
the task fingerprint                          whose inputs changed: the fingerprint
                                              decides, ``success.dat`` only for legacy
                                              directories. Small, mopac_step.
Remote staging cleanup (stage-back     do     Pruned directories come back on the next
of pruned directories)                        stage-out; prune on the remote too. The
                                              TaskServer over ssh uses the same staging.
``conda run`` PATH fragility           do     Launch the env's own interpreter
(mopac_step#161)                              (``<env>/bin/python``) instead of
                                              ``conda run ... python``; check ORCA's MDI
                                              launch the same way (consider each code).
Warm evaluator per bundle (5 s         defer  Phase 8, if real loops need it: measure the
start-up per iteration)                       start-up on ChemAI/TC first.
TIMEOUT seen on reattach not doubled;  do     Small task-layer fixes: a timeout found when
``2**n`` capped only by                       adopting counts like one seen live; cap the
``bundle_walltime``                           doubling at the queue's maximum walltime too.
Flaky                                  do     Make it measure overlap of the tasks'
``test_tasks_run_concurrently``               running intervals, not total elapsed < 2.5 s.
Timing-file helper (vasp-step#18)      do     A locked, slim-row append helper in
                                              seamm_exec (``fcntl`` lock, rotation by size);
                                              vasp-step adopts it in its own release.
seamm_manager#26 (migrate starts       do     Before the rollout: these change shared
stopped services), #27 (recreate              environments, #28/#29 already damaged one.
--latest), #28 (installer applies its
yml to a hand-built env), #29 (update
runs refused packages' installers)
Release vs docs ``gh-pages`` push      do     In devops: one concurrency group for every
collision (properties_step, twice)            workflow that pushes ``gh-pages``, so the docs
                                              push of CI-on-main and of Release queue
                                              instead of failing (and skipping PyPI).
=====================================  =====  =============================================

PBS test site (MolSSI10)
------------------------

- ``job_history_enable`` on with ``job_history_duration`` of at least 7 days, so
  ``qstat -x`` still reports a job that finished while the laptop was asleep for a long
  weekend (the JobServer and ``SchedulerBackend`` both rely on it to find terminal
  states).
- Remove the orphan test jobs and their remote directories left by the 2026-10-03
  validation (list them first; MolSSI10 is test-only, so with a one-line OK).
- ``max_resubmits``: PBS loses jobs the same ways SLURM does (node failure, ``qdel`` by
  an admin, walltime); the same guidance holds -- 3 for production sections, more for a
  deliberately short-walltime test section -- and the JobServer's user guide says so for
  both schedulers.

Rollout of phases 6 and 7
-------------------------

Paul decides when, timed with the EC pilot (D6). Phase 6 is opt-in and phase 7's queue
is opt-in, so the rollout only needs the packages, not a configuration change:

- Packages: molsystem, seamm, seamm-exec, loop-step, read-structure-step, table-step,
  properties-step, lammps-step, orca-step, geometry-analysis-step (phase 6, 2026.10.5);
  seamm-scheduler, seamm-exec, seamm-webui, seamm-manager and mopac-step at their phase 7
  versions.
- TinkerCliffs (``/projects/seamm/SEAMM``): ``update`` of exactly that list with
  ``--dry-run`` first, never ``--all`` (the xnn/lammps environment caveat); no services.
- ChemAI: hands-off except on Paul's explicit ask, for this list only; restart its
  JobServer and web UI after, between jobs.
- Memory rule: nothing on the laptop beyond the small validation runs above.

Decisions for Paul
------------------

- **D1 -- the TaskServer's shape.** Recommended: **(B)**, the queue with a command line
  and a ``seamm`` scheduler module -- the same trust as Q4 (ssh only) without a listener
  or a tunnel, and nearly all of the client already built and tested. (A) is the plan as
  written.
- **D2 -- daemon or none.** Recommended: no daemon (passes on every call and when a job
  ends); the alternative is a seamm-manager service.
- **D3 -- the Mac's own jobs.** Recommended: offer ``[local] type = queue, scheduler =
  seamm`` as an option, switched on for SEAMM_DEV first; the default stays as today.
- **D4 -- validation** as above, including one ssh run to paul.local.
- **D5 -- the task view** as above, read-only from files, iterations as rows within the
  job.
- **D6 -- the deferred items and the rollout** as triaged above; the rollout when Paul
  says.

Packages and order
------------------

seamm_scheduler (the queue, ``seamm-taskserver``, the ``seamm`` scheduler module) →
seamm_exec (``computational_environment``, the retry fixes, the timing helper, the flaky
test) → seamm_webui (task view) → mopac_step (``success.dat``, #161) → seamm_manager
(#26-#29) → devops (the ``gh-pages`` concurrency group). The PBS site items are
configuration on MolSSI10, not releases.

Review (design session, 2026-10-05)
-----------------------------------

The design session recommends (B). Folded in:

1. **No deadlock with D3.** With the Mac's evaluators in the same queue as their tasks,
   N one-core evaluators on N cores would hold every core while their tasks wait. Rule:
   a job submitted by the JobServer as an evaluator is charged **no cores** and a small
   memory (1 GB) -- evaluators are light and mostly waiting; only tasks are charged their
   cores. Tested with capacity 2 and three jobs each with a 2-core task.
2. **Atomic claim without a daemon.** Passes run from concurrent callers (several
   evaluators' status polls, runners ending), so a pass claims a queued job with one
   ``UPDATE ... SET state = 'starting' WHERE id = ? AND state = 'queued'`` in a short
   ``BEGIN IMMEDIATE`` transaction and starts it only if one row changed; the soak runs
   concurrent passes as well as concurrent submissions.
3. **Time limits across sleep.** A job's time is measured with ``time.monotonic()``,
   which stops while the machine sleeps on both macOS and Linux (Linux's
   ``CLOCK_MONOTONIC`` excludes suspend; ``CLOCK_BOOTTIME`` is the one that does not), so
   a laptop closed overnight does not time out its jobs on waking. A sleeping runner is
   never "lost": only a vanished pid, or a pid whose start time differs, is.
4. **Memory enforced, not just accounted.** The runner watches its job's process tree
   (the job's session, so detached MPI ranks too) and stops the job -- SIGTERM, then
   SIGKILL -- when it uses more than 125 % of its request for 30 s, recording ``memory``
   as the reason. On macOS RSS misses compressed and swapped pages (the
   ``mac-memory-guard-rss-blind`` lesson), so the measure is the process footprint where
   psutil offers it (``memory_full_info().uss``), and a second guard watches the
   machine: below 10 % available memory the newest job is stopped. seamm-manager writes
   the capacity into ``<root>/taskserver.ini`` on install, explicitly (physical cores,
   half the RAM), rather than leaving a silent default; the 2-4 GB rule for the Claude
   sessions' own local work is separate and unchanged.

Nits taken: over ssh the queue is invoked as ``<remote_python> -m
seamm_scheduler.taskserver ...``, never a command on the PATH (the ``conda run`` lesson);
a versioned ``queue.db`` schema with a migration test; ``seamm-taskserver queue`` shows
memory as well as cores; the task view refreshes only while the job is running, and the
``/files`` depth parameter defaults to 2; seamm-mbe and mbe-step join the ChemAI rollout
list only if Paul wants them there; the PBS orphan-job removal waits for Paul's one-line
OK.

Decisions (Paul, 2026-10-05)
----------------------------

D1 (B): the TaskServer is a queue with a command line and a ``seamm`` scheduler module.
D2: no daemon, with the atomic claim and sleep-safe timing of the review. D3: the Mac's
own jobs through the queue as an option, switched on in SEAMM_DEV first; evaluators
charged no cores. D4-D6 as proposed, except that the ssh validation uses the Mac mini at
work (``macmini`` in ``~/.ssh/config``: 8 cores, 16 GB, its own ``~/SEAMM``) rather than
paul.local. The rollout of phases 6 and 7 waits for Paul's word, timed with the EC pilot.

Implementation (2026-10-05)
---------------------------

Local commits on ``dev``, not pushed; test venvs ``~/SEAMM_DEV/venvs/phase7-B``
(released packages + editable checkouts) and ``phase7-webui``.

- **seamm_scheduler**: ``taskserver.py`` -- the queue (``queue.db``, schema 2 with a
  migration), scheduling passes with the atomic claim, first fit with the 30-minute
  reservation, the runner (``time.monotonic`` limits, memory by the session's unique
  set size with a 30 s grace, the machine-wide floor), the command line, and, found in
  testing, two rules: evaluators charged no cores *and* never counted against tasks
  (three 1-GB evaluators deadlocked a 2-GB queue through memory, not cores), and a
  job whose runner vanished is stopped before it is marked lost (its script runs in a
  session of its own and a rerun would have run beside it). ``seamm.py`` -- the
  ``seamm`` scheduler (``#SEAMM`` directives, ``python -m seamm_scheduler.taskserver``
  with the section's ``remote_python``/``remote_seamm_root``). ``max_walltime`` in
  target sections. Staging: SQLite side files and ``loop_entry.db`` are mirrored by
  listing them, not with rsync filters -- openrsync (macOS) protected the directories'
  contents under ``P */``, so the phase 5 fix never removed a *nested* stale log
  (found in the web UI: job 4014 kept a stale ``loop_entry.db``).
- **seamm_exec**: ``computational_environment`` reads a TaskServer job; the retry
  doubling capped by ``max_walltime``; a reattach-seen timeout is already doubled
  (regression test; a job the queue has forgotten cannot be known to have timed out);
  remote copies of a bundle removed once staged back; ``seamm_exec.timing``;
  ``test_tasks_run_concurrently`` by overlap.
- **mopac_step**: a stale ``success.dat`` no longer skips changed input; the MDI engine
  by the environment's Python path (#161, not reproducible with conda 24.11.3 here).
- **seamm_webui**: ``/api/jobs/{id}/tasks`` and the Files | Tasks switch; ``/files``
  takes an optional ``depth`` (no default, so the file browser keeps its full tree).
  Its ``test_sync`` tests expect seamm_scheduler's one-call stage-out and fail against
  2026.10.4: the web UI must pin the new seamm_scheduler.

Validation:

- Unit tests: seamm_scheduler 212 (TaskServer 16, staging 17), seamm_exec 152, the web
  UI 56, mopac_step 43. The web UI's Tasks tab checked in headless Chromium on job 4014
  (24 iterations, 25 steps with tasks, jump to an iteration's ``job.out``).
- **Two jobs sharing the Mac** (SEAMM_DEV ``[local] type = queue, scheduler = seamm``,
  capacity 4 cores / 4 GB): jobs 4015 and 4016, each a parallel loop of 8 MOPAC
  iterations at 1 core / 1 GB: 16 iteration tasks and 2 evaluators; at most 4 tasks
  (4 cores, 4 GB) ever ran at once across both jobs, both evaluators alongside; both
  finished, identical to each other (and within 0.07 kcal/mol of the TinkerCliffs
  serial reference, a Linux MOPAC).
- **Over ssh to macmini** (no shared filesystem): four MOPAC tasks in two bundles queued
  and run by macmini's TaskServer (private venv ``~/phase7_test``), staged both ways,
  results back, nothing left under ``remote_root``.
- **Kill soak** on SEAMM_DEV: the evaluator of job 4018 killed mid-loop -- its queue job
  failed (-9), the JobServer resubmitted it (``resubmit_count`` 1), the new evaluator
  adopted the iteration tasks, identical results; the runner of an iteration task of
  job 4019 killed -- the task lost and its job stopped, the iteration rerun and resumed,
  identical results.

The rest (2026-10-05):

- **seamm_manager** (#26-#29 and the TaskServer's capacity): a service recreated by an
  environment change is no longer started when it was stopped (``create_service(...,
  start=...)``), and updates restart the JobServer and web UI only if they ran
  (``restart_if_running``); ``environment recreate --latest``; an installer leaves
  alone a conda environment named in its .ini that SEAMM did not make (no
  ``seamm-*.sha256`` record and not SEAMM's name -- note: the TinkerCliffs
  ``seamm-lammps-xnndev`` got such records in the 2026-10-04 incident; remove them by
  hand before relying on this); ``update`` runs no installer for a refused package;
  ``install`` writes ``<root>/taskserver.ini`` if missing.
- **devops** (local branch ``release-docs-nonblocking``, not pushed): the Release
  workflow's docs deploy is ``continue-on-error``. The collision was with the
  package's own Docs workflow (``on: push``, its ``buildDocs.sh`` pushing
  ``gh-pages``) for the merge to main and ``make update``'s push to dev; a concurrency
  group would not do, since GitHub cancels all but one *pending* job in a group.
- **seamm_jobserver**: user guide -- ``max_resubmits`` for SLURM, PBS and the
  TaskServer, and PBS job history.
- **seamm_scheduler** docs: a TaskServer page.
- **PBS (MolSSI10)**: ``job_history_enable = True``, ``job_history_duration =
  168:00:00`` already; no jobs queued or running; the 22 leftover staged directories
  (``~/seamm_dev_remote_jobs``, 38 MB, 2026-08-09 to 2026-10-03) removed with Paul's OK
  (2026-10-05). The devops change is devops#3, merged 2026-10-05.

State left for the soak: SEAMM_DEV runs from ``venvs/phase7-B`` with ``[local] type =
queue, scheduler = seamm`` (backup ``PaulVT.local.ini.bak-2026-10-05-phase7``) and
``~/SEAMM_DEV/taskserver.ini`` (4 cores, 4 GB); macmini has ``~/phase7_test`` (a
private venv and a queue); ``~/SEAMM_DEV/venvs/phase7-webui``. SEAMM_DEV jobs
4015-4019 are the validation record.

Answers from the design session (2026-10-05, to the three questions with the code):

1. *The evaluator rule* (tasks never count evaluators) is right; its failure mode is
   recorded here: N evaluators can use more than their 1 GB charge each (one holding
   a large job database or a parallel loop's snapshots), and the machine-wide floor
   then stops the *newest* job, which may be a task rather than the evaluator that
   caused it. The stopped job's reason says why ("the machine was low on memory ...
   the newest job was stopped"). The real protection is the JobServer's
   ``max_concurrent_jobs``, which caps the evaluators running at once.
2. ``/files`` without a default depth is fine (the browser needs the full tree); the
   walk for ``/tasks`` stays bounded, and ``depth`` is documented for API users (the
   route's docstring, in the API's OpenAPI page).
3. No TaskServer service: the runners check the floor while anything runs, and when
   nothing runs there is nothing to protect; memory eaten by *other* processes while
   TaskServer jobs run is covered by the same check.

Code review (design session, 2026-10-05) and fixes
--------------------------------------------------

The review found the queue's core sound (atomic claim, migration, lost detection,
cancel, staging) and three blocking defects, all fixed (seamm_scheduler 95d86ee):

1. **The 30-minute reservation could deadlock the queue**: the oldest queued job could
   be an evaluator, whose ``break`` then held up every task -- including those of the
   running evaluators, which therefore never finished. Now only a task reserves, and
   only against later tasks; a task larger than a lowered capacity fails instead of
   reserving for ever. Tested (four evaluators fill a 2 GB queue, a fifth waits an
   hour, the running ones' task still starts).
2. **A slow runner could be marked lost while starting its script** (check's SELECT,
   then finish): now one guarded ``UPDATE ... AND pid IS NULL``.
3. **A job inherited the environment of whoever made the scheduling pass** (another
   job's ``SEAMM_CE``, threads, ids): a job's script now starts from a minimal
   environment, as ``export=NONE``; the local transport drops ``SEAMM_TASKSERVER_`` and
   ``SEAMM_CE``. Tested.

Also: the machine-wide floor waits its grace and stops one job per low-memory episode
(state in ``meta``; read-only while memory is fine); a job's processes include
descendants with sessions of their own (a pool's codes) for stopping and for memory;
the runner always records its job's end (try/finally); over ssh the queue needs
``remote_python``; the evaluator charge is at most a quarter of a small queue's
memory. The timing helper's rotation race (a writer waiting on a set-aside file
rotating the new one onto the same name) is fixed with an inode check and unique
names (seamm_exec d779a10). mopac_step: ``reuse_previous_run`` with a test, and
``_conda_python`` takes a path or asks conda (8a4d8d8) -- checked with a real MOPAC
flowchart after lint caught a ``success`` variable left behind, which no unit test
exercises. Web UI (0291f63): nested parallel loops find their frame in the
iteration's own checkpoint; ``/files?depth`` prunes its walk and rejects ``depth <
1``.

Kept, with reasons: the queue's root stays machine-wide (the default installation's
root unless ``remote_seamm_root``), so that installations on one machine share its
capacity rather than each using all of it; documented on the TaskServer page.
Pins at release preparation: seamm_exec and the web UI on the new seamm_scheduler.
