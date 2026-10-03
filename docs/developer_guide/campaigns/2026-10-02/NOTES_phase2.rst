Phase 2 notes
=============

2026-10-02 -- the scheduler abstraction and the SLURM back end for tasks
------------------------------------------------------------------------

Done locally, on ``main`` of a new local repository, ``seamm_scheduler`` (no
GitHub repository yet), and on ``dev`` in ``seamm_slurm``, ``seamm_exec`` and
``seamm_jobserver``. Committed locally; not pushed and not released.

What was built
~~~~~~~~~~~~~~

``seamm_scheduler`` (new package, decision Q1)
    One module per queueing system behind a ``Scheduler`` class. Copied from
    the layout of ``seamm_slurm``: versioningit, devops workflows, docs,
    HISTORY, uv CI with no ``test_env.yaml``.

    - ``scheduler.py``: the interface.

      - ``directives(resources, extra)``, ``directive_lines``,
        ``submit_cmd``/``parse_submit``, ``status_cmd``/``parse_status``,
        ``cancel_cmd``, ``count_cmd`` (the user's own queued jobs) and
        ``log_directives``.
      - ``poll(run, ids)`` composes these. SLURM overrides it, because it
        needs ``squeue`` and then ``sacct``.
      - ``poll_failed`` (see "Outages" below) and ``env_names``.
      - ``JobStatus`` with ``task_state``, ``TASK_STATES`` (pending ->
        queued, completed -> finished, cancelled -> lost, ...) and
        ``get_scheduler(name)``.
    - ``slurm.py``: SLURM.

      - The directive syntax and the state vocabulary.
      - The ``--json`` probe with text fallback, and SLURM 25.11's nested
        ``return_code``.
      - The historical ``SlurmBackend``/``LocalSlurm``/``SshSlurm`` and
        their error classes.
    - ``pbs.py``: PBS Professional / OpenPBS, tested only against mocked
      ``qsub``/``qstat``/``qdel`` output (no PBS site).

      - Resources become one ``select`` statement, and ``partition`` is
        the queue.
      - ``qstat -x -f -F json``, falling back to the ``qstat -x`` table plus
        ``qstat -x -f`` for exit statuses.
      - Exit 271 (a ``qdel``) means cancelled.
    - ``backend.py``: ``QueueBackend(scheduler, transport)``: ``submit``,
      ``poll_many``, ``cancel``/``cancel_many``, ``count_jobs``.
    - ``local.py``/``ssh.py``: ``LocalTransport``/``SshTransport``.
      ``TASK_SSH_OPTIONS`` and a command timeout are opt-in, so the
      JobServer's argv is unchanged.
    - ``stage.py``: as before, plus ``push``/``pull`` of many relative paths in
      one ``rsync --files-from=-``, which openrsync on macOS supports. The
      ssh options and the timeout are opt-in here too.
    - ``config.py``: ``TargetSection`` (``SlurmSection`` is an alias) with the
      task keys (below), ``build_task_backend``/``build_task_stager``,
      ``task_settings``/``from_settings`` for ``target.json``, ``load_target``
      (``load_slurm_config`` is an alias) and ``list_sections``.
    - ``script.py``: ``build_script(directives, payload, scheduler="slurm")``.

    167 tests: the 112 ported from ``seamm_slurm`` plus the interface, PBS,
    the task keys, staging, and SLURM 25.11 JSON captured on TinkerCliffs.

``seamm_slurm`` (the shim)
    Every module re-exports from ``seamm_scheduler``. ``local``, ``ssh`` and
    ``stage`` keep an ``import subprocess``, because callers patch
    ``seamm_slurm.<module>.subprocess.run``. Its own 112 tests pass
    unchanged, as do ``seamm_jobserver``'s.

``seamm_exec``
    - ``scheduler_backend.py``: ``SchedulerBackend``, the ``TaskBackend`` for
      ``tasks = queue``.

      - Each ``submit`` call is one bundle: one batch job that runs
        ``python -m seamm_exec.task_worker bundle.json`` in SEAMM mode.
      - Backend ids are ``<job id>#<bundle>.<n>#<key>``, so a restarted
        evaluator can rebuild everything from the manifest record
        (``adopt``).
      - ``room()`` is ``max_queued_tasks`` less the user's ``squeue --me -r``
        count, refreshed once per poll interval.
      - A QOS submit-limit error, or a failure to reach the cluster, raises
        ``QueueFull``, so the bundle is held, not failed.
    - ``bundle_runner.py``: the SEAMM-mode bundle.

      - It runs the tasks through a ``LocalPool`` sized to the allocation,
        with ``resolve_programs=True``. Programs come from the
        ``<program>.ini`` files where the bundle runs, then the resolver hook.
      - So ``{code}``/``{NTASKS}``, conda/modules, ``$TMPDIR`` scratch and
        ``return_files`` behave exactly as in the evaluator's pool, and tasks
        that fit side by side in the allocation run concurrently.
      - It writes ``DONE`` (in the task layer's own form, with the
        ``files`` list) or ``FAILED`` (with the reason) in
        ``tasks/<key>/``.
      - ``task_worker.py`` only loads it by name for ``"mode": "seamm"``,
        so the pure path still imports nothing from SEAMM (phase 1's test
        enforces this).
    - ``resolve.py``: the per-program resolver hook (phase 1 requirement a).

      - Entry-point group ``org.molssi.seamm.exec.resolvers``, named after
        the program.
      - ``hook(config, cmd, env, ce, root) -> (config, cmd, env)``.
      - ``read_config`` and ``register`` (for tests).
    - ``targets.py``: ``find_target`` (explicit, then ``<job>/target.json``,
      then ``$SEAMM_TARGET`` with ``$SEAMM_TARGETS``, then none) and
      ``write_target``.
    - ``TaskSet``:

      - It picks its back end from the target, and takes ``bundle_tasks``,
        ``bundle_walltime`` and ``inline_below`` from it. On a queue with
        neither bundling setting, each task is its own bundle.
      - Bundling is by count and by summed walltime (or estimate).
      - A back end with ``bundles = True`` gets one ``submit`` per bundle
        (with ``bundle=`` and ``markers=``), while it has ``room()``. The
        rest wait in ``_held``.
      - A failed ``submit`` restores the attempt count.
      - ``_reattach`` adopts queued/running tasks from a back end with
        ``adopt()``.
      - Lost tasks of one pass go back together, so a bundle's lost tasks
        return as one bundle.
      - A lost task's reason comes from the back end (with the tail of the
        bundle's log).
      - ``route()`` keeps a task that carries ``Task.config`` on this machine
        when the back end does not accept it (an ssh target), with one
        warning.
      - ``_restore`` lists the returned files for a pure-worker ``DONE``
        without a ``files`` list (phase 1 requirement b; the SEAMM-mode
        worker writes the list itself, and the TaskSet rewrites ``DONE`` on
        collection).
    - ``computational_environment()``: the job variables come from
      ``seamm_scheduler`` (SLURM, then PBS), and a ``_pbs()`` reader was
      added. ``Base``'s in-situ check uses the same variables.
    - 83 tests: 51 from phase 1, 32 new. The new ones run with a fake queue
      that executes each real batch script with bash, so the real worker,
      pool and markers are exercised.

``seamm_jobserver``
    It imports ``seamm_scheduler``. A section with ``tasks =`` is written as
    ``<job dir>/target.json`` before the job starts (and so before staging);
    sections without it write nothing. 87 tests.

Target keys (all optional; a section without ``tasks =`` means what it did)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

======================= ================================================================
``tasks``               ``pool`` | ``queue`` | ``taskserver`` (phase 7; refused for now)
``scheduler``           ``slurm`` (default) | ``pbs``
``shared_filesystem``   default: yes for the local task transport, no for ssh
``bundle_tasks``        tasks per bundle
``bundle_walltime``     SLURM time syntax; also the bundle's ``--time`` when its tasks
                        give no walltime
``max_queued_tasks``    the user's queued + running jobs, counted with ``squeue --me -r``
``inline_below``        seconds (default 60)
``remote_python``       ssh targets: a Python with ``seamm_exec`` on the cluster
``remote_seamm_root``   its ``<program>.ini`` files; default: the venv's root
``poll_interval``       seconds (default 30)
``url``                 ``tasks = taskserver`` (phase 7)
======================= ================================================================

The other keys of a section (``partition``, ``account``, ``qos``, ``export``,
``constraint``, ...) are the bundles' site defaults, and each bundle's resources
override them. ``setup`` lines run before the worker. For ``type = slurm`` (the
evaluator is itself a batch job) tasks always use the local transport with a
shared filesystem, because the evaluator is already inside the cluster.

``bundle_walltime`` is parsed with SLURM's rules: a bare number is minutes.
``config._parse_time`` used to read a bare number as seconds. This only matters
for a ``.limits`` bound written as a bare number, which no deployed file has.

Decisions made with the design session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. **How the evaluator finds its target.**

   - The JobServer writes ``<job dir>/target.json``, a new job-level file
     (added to the rollout promise in the design).
   - It works for an evaluator that is itself a batch job on a cluster that
     cannot read the JobServer's ini file, and it survives resubmission.
   - The order is: explicit, ``target.json``, ``$SEAMM_TARGET`` (for hand runs
     with ``run_flowchart``), then the ``LocalPool``.
2. **Tasks run on the compute node through seamm_exec's own ``_run_task``.**

   - The bundle runs with ``remote_python`` (ssh) or ``sys.executable`` (local
     transport), so the semantics are identical to the ``LocalPool``.
   - On ssh targets ``Task.config`` is ignored and the program is resolved
     where it runs. On the local transport ``Task.config`` is used as the pool
     uses it.
   - The pure worker path stays, for machines without ``seamm_exec``.
3. **The protocol has ``poll(run, ids)``**, with the SLURM override.
   Bundling and throttling live in the ``TaskSet``.

**Consequence for ORCA.**

- ``orca_step`` resolves its own configuration in the evaluator: it passes
  ``Task.config``, the Mac's ``library-path`` export prefix in ``cmd`` and the
  OpenMPI binding in ``env``.
- Those paths mean nothing on another cluster, so on an ssh target the
  ``TaskSet`` keeps ORCA (and MOPAC, which also passes ``config``) on the
  evaluator's machine, exactly as today.
- Sending them out needs an ``orca`` resolver hook in ``orca_step`` and a bare
  command. That comes with ``get_task`` in phase 3.
- Verified with ``Testing/test.flow`` and the job's target set to TinkerCliffs:
  both tasks ran locally, the warning was logged, and nothing was submitted.

How a bundle's partial completion is handled
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- The worker skips any task whose ``tasks/<key>/DONE`` exists. So running
  the same ``bundle.json`` again, after a walltime limit or a node failure,
  runs only what is left (tested). When a bundle's job ends, a task without
  ``DONE`` or ``FAILED`` is *lost*, and its reason names the job's state and
  the last line of its log.
- The ``TaskSet`` resubmits the lost tasks of one pass together. They keep
  their bundle name, so they become ``_bundles/<bundle>.<n+1>/`` with a
  ``bundle.json`` of just those tasks, and the finished ones are never rerun.
- Each lost resubmission counts an attempt (``max_lost_retries`` 2 per run,
  ``max_attempts`` 3 overall).
- On a shared filesystem, results are yielded as the worker writes each
  ``DONE``, while the bundle still runs. Without one, they come back when the
  bundle's job ends and its directories are pulled.

Outages (the laptop case)
~~~~~~~~~~~~~~~~~~~~~~~~~

Paul's laptop sleeps, changes networks and needs a VPN at home, so the evaluator
may lose the cluster for minutes to an hour.

- A job counts as missing (three polls in a row) only when the queue actually
  answered. ``Scheduler.poll_failed`` is set when ``sacct`` (or ``qstat``)
  could not be asked, and then nothing counts.
- A failed poll or a failed pull is logged and retried at the next poll.
- An ssh or rsync failure at submission holds the bundle (``QueueFull``)
  instead of failing it.
- The task layer's ssh uses ``BatchMode``, ``ConnectTimeout=30``,
  ``ServerAliveInterval=15``/``CountMax=4`` (a connection dead after sleep is
  given up within about a minute) and ``ClearAllForwardings``. The
  ``tinkercliffs`` alias has a ``LocalForward``, which otherwise fails to bind
  on every command. Commands time out after 300 s and rsync after an hour.
- The JobServer's ssh is unchanged.

Validation
~~~~~~~~~~

- **This Mac -> TinkerCliffs (ssh, rsync staging, SLURM 25.11).**

  - Setup:

    - ``phase2_driver.py`` (here) with eight B3LYP/def2-SVP single points,
      four MPI ranks each, bundled four to a job.
    - Target ``[tc]`` in ``Testing/phase2/targets.ini``: ``account = seamm``,
      ``normal_q``, ``tc_normal_short``, ``export = NONE``.
    - A development venv in ``/projects/seamm/psaxe/phase2/venv``: the frozen
      production stack plus these checkouts. ``/projects/seamm/SEAMM/venv``
      was not touched, and ORCA comes from the production ``orca.ini``
      (``installation = modules``).
  - The evaluator was killed with ``kill -9`` while both bundles were queued.
    The second run logged "still with queue:tc ... polling it rather than
    submitting again" for all eight tasks, staged back, and finished with
    ``attempts=1`` and no new jobs.
  - ORCA ran with "4 parallel MPI-processes" in ``/localscratch/<jobid>``
    (node-local, from ``$TMPDIR``).
  - The script carried ``--ntasks=4 --mem-per-cpu=1200M --time=00:40:00``
    (the tasks' walltimes summed).
- **This Mac -> MolSSI10 (ssh, rsync, SLURM 20.11, no ``--json``).** Eight
  PM6 MOPAC single points (conda installation) in three bundles, all finished.
  ``squeue --json``/``sacct --json`` are "unrecognized" there, so status came
  from the text path.
- **Cluster alone (an evaluator on TinkerCliffs, ``type = slurm``, so local
  transport, shared filesystem, no staging).** The driver on the login node
  gave the same eight ORCA energies as from the Mac, to the last digit.
  Results arrived while bundles were still running.
- **The SEAMM_DEV A/B comparison.**

  - The sides:

    - A: the current freeze with the released ``seamm-exec`` 2026.10.2,
      ``orca-step`` 2026.10.2.1, ``mopac-step`` 2026.10.2 and
      ``seamm-util`` 2026.10.2.
    - B: A plus the four checkouts.
    - Both were built beside the current version, as
      ``venvs/phase2-A``/``-B``, without switching.
  - ``seamm-manager --root ~/SEAMM_DEV compare`` on ``test.flow``,
    ``harness_water``, ``builder_loop`` and ``bsse``:

    - identical energies, charges and tables;
    - the only differences were versions and citation wrapping, pids,
      timings, timestamps, ``tasks/`` manifest ids and chargemol's last-digit
      noise, the same as in phase 1.
  - ``test.flow`` with ``SEAMM_TARGET=tc`` was likewise clean (above).

Review (2026-10-02)
~~~~~~~~~~~~~~~~~~~

An independent review of the four diffs reproduced five bugs and found nine
plausible ones. All are fixed except where noted. The scripts are in
``/private/tmp/claude-502/review/``; each fixed bug now has a test.

1. **A pull-back that failed once was never retried**, and the evaluator spun
   at 100% CPU (the job was terminal, so it was no longer polled). Jobs that
   ended but are not back are now worked out from every tracked job, the
   poll time is set on every pass, outages do not count as failures, and five
   real failures (e.g. purged scratch) make the tasks lost with that reason.
2. **The JobServer could not start the design's** ``[arc]`` (``type = local``,
   ``transport = ssh``): ``_build_cmd`` used the remote ``run_from_jobserver``
   for a local evaluator. Only ``type = slurm`` with ``transport = ssh`` now
   means a remote evaluator.
3. **A restart with changed inputs adopted the old bundle** and recorded its
   output under the new fingerprint. Adoption now requires the same
   fingerprint, ``DONE``/``FAILED`` are honoured only if their fingerprint
   matches (in the backend and in the worker), and the old job is cancelled
   when no adopted task still runs in it.
4. **Unparseable ``squeue``/``sacct`` output** (a banner, a schema change) read
   as "every job is gone". It now sets ``poll_failed``.
5. **PBS**: ``F`` without an exit status (deleted while queued) never ended;
   it is now cancelled (lost). SLURM spellings in a section are dropped with a
   warning; ``qselect`` counts and finds jobs.
6. **A submission whose outcome was unknown** (the connection dropped after
   ``sbatch`` ran) was submitted again as a new bundle. Each bundle's job name
   is unique (``seamm-<bundle>.<n>-<nonce>``), and the next attempt asks the
   queue for that name first.
7. The manifest was written only after all bundles were submitted. It is now
   written after each.
8. ``finally`` marked tasks cancelled even when ``scancel`` failed (an outage),
   so a restart resubmitted them. They are marked only when the cancel worked.
9. On a shared filesystem a just-written ``DONE`` hidden by attribute caching
   could make a task lost. A job gets one more poll after it ends, and markers
   are read from a fresh ``os.listdir``.
10. Remote job directories could collide across installations sharing a
    ``remote_root`` (job numbers are per datastore). The remote name now always
    carries a hash of the local path.
11. Clusters without SLURM accounting would have had ``poll_failed`` forever.
    "accounting storage is disabled" is recognised and ``squeue`` trusted.
12. Bundles submitted from inside an evaluator job inherited its ``SLURM_*``
    variables. The local task transport drops ``SLURM_*`` and ``PBS_*``.
13. Behaviour changes, kept and recorded in HISTORY: ``Base``'s in-situ default
    and ``computational_environment()`` now recognise PBS jobs (no SEAMM
    installation runs under PBS today); a bare ``.limits`` time is minutes; a
    bad task key in a section raises when the file is read, as a bad ``type``
    always did.
14. Minor: ``xargs -0`` for the remote marker cleanup; a bundle held for more
    than 15 minutes logs a warning; a staged task outside the job directory
    fails before anything is submitted. Not changed: ``_list_returned`` keeps
    ``Base``'s flat file names.

After the fixes, the live Mac -> TinkerCliffs and Mac -> MolSSI10 runs were
repeated: both clean, with the same energies.

Found on the way
~~~~~~~~~~~~~~~~

- **``computational_environment()`` crashed in any SLURM job without
  ``--ntasks``** (no ``SLURM_NTASKS``: ``KeyError``). Its hostlist expansion
  crashed on ``tc[053,059]`` and dropped the zero padding of ``tc[053-055]``.
  Both are fixed. The first was found when a probe bundle without ``ntasks``
  died before the worker started. Bundles now always request ``ntasks``.
- The first cluster-alone run had no evaluator root (no flowchart), so the
  worker had no ``orca.ini``. The local transport now falls back to
  ``seamm_util.root.current_root()``.
- A conda-installed code needs ``shell=True``, since ``Local.exec`` makes it a
  ``conda run ...`` string. Without it the whole string is taken as a program
  name. This is pre-existing and was hit by the driver, not by a plug-in.
- SLURM 25.11's ``squeue --json`` reports ``["TIMEOUT", "COMPLETING"]``:
  the first flag is the state.
- ``arc_pick.py`` (the arc-slurm-submit skill) could not read
  TinkerCliffs's state during a short network outage on this side; its
  choice was made by hand (``sbatch --test-only``).
- ``~/SEAMM_DEV/PaulsPersonal.local.ini`` is orphaned again: this Mac is now
  ``PaulVT.local``. Reported to the design session; left alone.

Deferred
~~~~~~~~

- The ``orca`` (and ``mopac``) resolver hooks and a bare command, with
  ``get_task``, in phase 3. Until then those steps' tasks stay local on ssh
  targets.
- Partial progress without a shared filesystem: results come back only when a
  bundle ends. A periodic pull of the markers would give them earlier.
- ``max_queued_tasks`` counts the user's jobs on the cluster, but two
  evaluators can still race for the last slots. A rejected ``sbatch`` is held
  and retried, so this is harmless.
- PBS has no ``count_cmd`` (``qselect -u`` needs the remote user name), so
  ``max_queued_tasks`` is ignored for PBS.
- TinkerCliffs's production venv is not versioned yet. That is a later rollout
  step.

Cleanup
~~~~~~~

- Test directories:

  - ``~/Work/SEAMM/Testing/phase2/`` (Job_9000xx)
  - ``tinkercliffs:/projects/seamm/psaxe/phase2/{tasks,Job_9000xx}``
  - ``molssi10:~/phase2/tasks``
- Development venvs:

  - ``tinkercliffs:/projects/seamm/psaxe/phase2/venv``
  - ``molssi10:~/phase2/venv``
  - ``~/SEAMM_DEV/venvs/phase2-{A,B}``, to be pruned once phase 2 is released.
