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
