Phase 1 notes
=============

2026-10-02 -- the task layer in seamm_exec, and ORCA and MOPAC on it
--------------------------------------------------------------------

Done on ``dev`` in ``seamm_exec``, ``orca_step`` and ``mopac_step``. Committed
locally, not pushed and not released.

What was built (``seamm_exec``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- ``tasks.py``: ``Resources``, ``Task``, ``TaskResult``, the ``TaskBackend``
  protocol, ``Manifest`` and ``TaskSet``, plus ``run_task()`` for a step that
  runs one calculation.

  - The manifest is ``<step dir>/tasks/manifest.json``. It is written lazily
    (dirty flag, at most once a second, and always at the start and end of a
    run), because a 2,800-task frame would otherwise rewrite a multi-megabyte
    file thousands of times.
  - Whether a task finished is recorded by its ``tasks/<key>/DONE``, which is
    written at once.
- ``local_pool.py``: ``LocalPool``.

  - Its capacity is ``computational_environment()``: cores, available
    memory and GPUs.
  - Tasks start in submission order as soon as their cores and memory are
    free. A task larger than the pool is clamped to it and runs alone.
  - Each task gets its own ``ce``: ``NTASKS``, ``CPUS_PER_TASK``,
    ``MEM_PER_CPU`` and ``MEM_PER_NODE`` from its ``Resources``.
  - A task runs through the executor's ``_run_task()`` in a worker thread,
    and ``Local.exec`` starts it with ``Popen(start_new_session=True)``.
  - SIGTERM and SIGHUP to the evaluator kill the task process groups, then
    chain to the previous handler. The JobServer stops a job with
    ``process.terminate()``, so this covers it.
- ``task_worker.py``: the pure-Python bundle worker, run by path. It applies
  ``success_text`` too.
- ``base.py``: the old ``run()`` body is now ``_run_task()``, unchanged except
  for two opt-in knobs the concurrent pool uses:

  - ``set_umask=False``: the umask is process-wide, so concurrent tasks must
    not change it.
  - ``keep=[...]``: the in-situ cleanup must not prune the ``tasks/``
    bookkeeping of the step directory.

  ``run()`` builds a one-task ``TaskSet`` with ``manifest=False`` and a
  synchronous one-slot ``LocalPool`` that runs in the caller's thread. It passes
  the caller's ``ce`` unchanged, leaves the environment untouched, and lets
  exceptions propagate. It writes no new files.
- ``local.py``: under the pool only (a thread-local hook), ``Popen`` in a new
  session instead of ``subprocess.run``. The shim path is unchanged.

Decisions taken with the design session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

These are now in the design's task API section.

- ``Task.directory``: None means ``tasks/<key>/``, the default only for fan-out.
  The ORCA and MOPAC call sites pass their own directories, so ``orca.out``
  and ``mopac.out`` stay where users and the Dashboard expect them.
- ``Task.config``: a local override of the ``<program>.ini`` section. ORCA and
  MOPAC keep resolving their own configuration, and remote back ends will
  ignore it. ``LocalPool`` adds ``{code_dir}`` (the directory holding
  ``code``); ORCA's ``orca_2aim`` now uses it instead of an absolute path baked
  into the command.
- ``Task.fingerprint``: the restart identity. The default hashes ``cmd`` and
  ``files``, never ``env``. ORCA gives one that ignores ``%pal`` and
  ``%maxcore``, so a different machine size does not force a rerun.
- ``Task.success_text``: ``{file: text or [texts]}``. Added after the live test
  below found that **ORCA exits 0 after an error termination**, so a failed run
  was being marked ``DONE``. Both ORCA call sites set
  ``{"orca.out": "ORCA TERMINATED NORMALLY"}``, the test ``_parse_output``
  already used. MOPAC sets none: its parser checks nothing today and its
  ``success.dat`` marks a run done whatever the outcome, so it is unchanged
  (consider-each-code-separately). ``mopac.out`` ends with
  ``== MOPAC DONE ==`` and ``mopac.arc`` has ``JOB ENDED NORMALLY`` if that is
  wanted later.
- Restart:

  - Within a run a failed task is never retried, and a lost one is retried up
    to twice.
  - Across runs a failed or lost task is tried again until it has had three
    attempts in all, then reported failed with its history. Each history entry
    and the manifest record carry a ``reason``: the return code, the failed
    success check and the file, or lost.
  - ``LocalPool`` does not reattach: it kills a leftover process group (same
    host, pid alive) and reruns the task.
- MPI binding (``OMPI_MCA_hwloc_base_binding_policy=none``) and
  ``OMP_NUM_THREADS = cpus_per_task`` are set **only when the pool runs more
  than one task**, on or off SLURM, and never over a value the task sets. A
  lone task gets exactly the environment it always had.
- Bundling: ``TaskSet`` assigns bundles in the order tasks are added
  (``bundle_tasks``; one bundle by default). ``archive=True`` appends a
  bundle's ``tasks/<key>/`` directories to ``tasks/<bundle>.tar`` once all of
  its tasks are done, removes the directories, and records the tar in the
  manifest. Restart reads ``DONE`` and the returned files from the tar.
  ``LocalPool`` does not go through the worker script.
- Inline rule: ``TaskSet.route()`` sends a task with ``estimated_seconds <
  inline_below`` (60 s) to the evaluator's own pool when the program is
  installed here (``task.config`` given, or a ``[<executor>]`` section in
  ``<root>/<program>.ini``). Phase 1 has no target plumbing, so it is tested
  with a fake remote back end.
- ``estimated_seconds`` heuristics, both rough:

  - ORCA: method class and basis family from the ``!`` line, times
    ``n_atoms**2.5``. Water at B3LYP/def2-SVP comes out at about 1.5 s.
  - MOPAC: a cube in the atom count, times 30 for an optimization.

Validation
~~~~~~~~~~

- ``seamm_exec`` has 41 tests:

  - the shim's result dictionary and files, exception propagation, and
    ``@subdir+``;
  - concurrency, slot partitioning by cores and memory, whole-pool and
    oversized tasks, and binding only when concurrent;
  - ``{code_dir}`` and configuration from the ini file;
  - in-situ pruning per task and in the step directory, and scratch
    copy-back;
  - restart: skip finished tasks, rerun changed inputs, the fingerprint
    override, env not hashed, and the attempts cap;
  - SIGKILL and SIGTERM of a real evaluator subprocess, then rerun;
  - one tar per bundle, with restore from the tar;
  - the inline rule, the worker, ``success_text``, and a broken executor
    that fails its task instead of hanging.

  All pass repeatedly in about 18 s. ``orca_step`` passes 164 and
  ``mopac_step`` 35, each with tests of the new helpers.
- ``Testing/test.flow`` (ORCA energy, then DDEC6 through ``.wfx``) runs
  clean. Rerun in place without clearing ``tmp/``, ORCA reports "ORCA had
  already finished this calculation, with the same input ... using those
  results", and ``job.out`` differs from the first run only in timestamps and
  that line.
- ``harness_water.flow`` and ``builder_loop.flow`` (MOPAC in a Loop) run
  clean. Each iteration has its own directory, so nothing is skipped between
  iterations; energies are C −13.43, CC −14.52, CCC −17.43.
- ``phase1_driver.py`` (here) runs four ORCA (two cores each) and eight MOPAC
  calculations concurrently through one ``TaskSet``. After ``kill -9`` once
  two ORCA runs had finished, both running ORCA process groups were still
  alive. The rerun killed them, restored the two finished runs, ran the other
  ten, and left no processes behind.

Found on the way
~~~~~~~~~~~~~~~~

- ``seamm-dev`` had a stale ``seamm`` (2026.9.29), so 115 ``orca_step`` tests
  failed with or without these changes (``applies_when``). Fixed with
  ``make install`` in ``seamm``.
- A test double without ``_run_task`` made a pool thread die before it freed
  its slot, and the ``TaskSet`` then waited forever. The thread's setup is now
  inside its ``try``, so any error fails the task.
- The pool's memory is the machine's *available* memory, which was 4 GB under
  load on this Mac. With 2 GB per ORCA task that allowed two at a time, which
  is correct but conservative. Total memory, or a site setting, may be the
  better capacity later.

Deferred, for later phases
~~~~~~~~~~~~~~~~~~~~~~~~~~

- **Phase 2 requirement: the per-program resolver hook.** An entry point
  that, on the executing machine, turns ``program`` + ``Resources`` into the
  ``cmd`` prefix, ``env`` and configuration. It would replace what ORCA
  (``full_orca_path``, ``library-path`` → ``_mpi_env``) and MOPAC (default
  ini, ``which mopac``) do in the evaluator today. ``Task.config`` stays as the
  local override.
- Phase 2: the worker's ``DONE`` records no ``files`` list. The scheduler back
  end's ``fetch`` must rewrite it in the ``TaskSet`` form after staging back,
  or ``_restore`` must list the returned files from ``return_files``.
- Several evaluators on one machine each think they own it (four jobs × the
  whole pool). This is not a regression, since four ORCA jobs each use all
  cores today. A machine-wide slot lock would fix it.
- A multi-node allocation is shared out as one pool of ``NTASKS`` cores; the
  per-task ``NODELIST`` is not narrowed.
- Tasks of the ``docker`` installation and executor cannot be killed by the
  pool, which has no process to signal.
- Release order: ``seamm_exec`` first; then ``orca_step`` and ``mopac_step``
  with ``seamm-exec>=<that version>`` in ``requirements.txt`` **and**
  ``devtools/conda-envs/test_env.yaml``, each with its ``HISTORY.rst`` entry
  (release-shared-widgets-first). Before that, compare against the previous
  ``~/SEAMM_DEV`` version with ``seamm-manager --root ~/SEAMM_DEV compare``.
