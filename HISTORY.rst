=======
History
=======
2026.10.6.2 -- Cancelling a task set; one queue count per cluster; Docker removed
    * ``TaskSet.cancel()`` (and ``Evaluator.cancel()``), callable from another
      thread while the set runs: the tasks in flight are cancelled on their back
      end, the held ones dropped, all marked ``cancelled`` in the manifest so a
      later run submits them afresh, and a ``cancelled`` result is yielded for each.
      A step running several task sets at once (the MBE step's levels) can stop the
      others when one fails instead of letting hours of work finish for nothing.
    * The user's job count that ``max_queued_tasks`` is checked against is shared by
      every back end in a process that submits to the same queue system, so
      several task sets running at once no longer each see the same room and
      overshoot the limit together.
    * Docker is no longer supported: the Docker executor, the ``installation = docker``
      choice for a code and the ``docker`` Python dependency are removed. Conda,
      modules and local installations run exactly as before.

2026.10.6.1 -- A cost model fitted to the timing records, and predictions from it
    * ``seamm_exec.timing_model``: ``fit`` reads a program's timing records (all
      files, schema 1) and fits the separable model of the 2026-10-05 campaign --
      a start-up constant per machine class, a unit cost as a power law in the
      program's size variables with an intercept per method class and an offset
      per task, a parallel exponent when the records span core counts, and a
      shrunk offset per machine class -- by ridge regression in numpy, and writes
      it to ``~/.seamm.d/timing/models/<program>.json`` with a report (R^2, the
      fraction within 1.3x and 2x). ``predict`` evaluates a model for a
      calculation's descriptors, cores and machine at a chosen quantile of the
      residuals (the 95th for a queue walltime, the median for packing), taking the
      task's iteration count from its fitted distribution unless given, and
      widening the spread for a machine class it has not seen. Command line:
      ``python -m seamm_exec.timing_model fit|predict``.
    * ``seamm_exec.timing_benchmark``: the seed benchmark that places a machine
      class in the model -- per code a few molecules spanning two orders of
      magnitude of size, two or three method classes, single points and
      optimizations, run once per core count for a parallel code -- built from a
      spec with the installed plug-ins and run with ``run_flowchart``; its rows
      carry ``benchmark=<set>``. ``python -m seamm_exec.timing_benchmark --codes
      orca,mopac --cores 1,4,8 --fit``.
    * What a program's cost model is made of -- its size variables, method class,
      task and unit columns -- is declared by the code step and passed when it
      records a run (``record_timing(..., spec=)``, ``seamm_exec.TimingSpec``);
      seamm-exec writes it once beside the records as ``<program>.spec.json`` and
      the fit reads it from there, so no plug-in is imported and any code can
      join. The specs of the 2026.10.6 steps remain as a fallback until each step
      writes its own.
    * A model keeps itself current: ``predict`` refits it when its records have
      grown by a fifth since the fit, or when it is a week old and the records
      have changed, under a lock so concurrent runs do not all refit; a refit
      fitted to fewer rows or predicting worse is not taken. The first prediction
      on an installation with records but no model fits one.
2026.10.6 -- Timing records from every code step
    * ``seamm_exec.timing.record_timing`` writes the timing record of a run made
      outside the task layer (a step that still runs its code with
      ``executor.run``), with the same common columns as ``record_task_timing``;
      ``structure_descriptors`` gives the atoms, heavy atoms, electrons, charge,
      multiplicity, periodicity and volume every code's record shares. The code
      steps' own CSV writers (MOPAC, Gaussian, VASP, LAMMPS, Psi4, DFTB+) move to
      these (vasp-step#18; parallel-execution campaign phase 8 item 13).
2026.10.5.2 -- Timing records for a cost model; sub-steps find the SEAMM root again
    * Bugfix: a calculation started by a sub-step -- ORCA's Energy, Optimization and
      Frequencies, and its counterpoise jobs -- did not know the SEAMM root, so
      ``<root>/orca.ini`` was not read and ORCA was taken from the PATH: on Debian
      and Ubuntu systems ``/usr/bin/orca`` is a screen reader of the same name, and
      clusters that load ORCA as a module have none. The root now comes from the
      step, else the run's root (``--root``, ``SEAMM_ROOT``, the installation's).
    * ``seamm_exec.timing.record_task_timing(task, result, descriptors)``: one call
      by which a code step writes the timing record of a run it has made through
      the task layer. The common columns -- the machine class, the task's
      resources, the wall time from the task manifest, the estimate it was given,
      the outcome -- come from here; the step adds the numbers that describe the
      calculation. ``machine_class()`` keys a machine by cluster, partition and CPU
      model rather than by hostname, so rows from the nodes of one partition pool
      and those of different clusters separate.
    * A timing row with new columns sets the file aside and begins a new one with
      the wider header, rather than dropping the columns; ``read_timings(...,
      all_files=True)`` reads the files set aside too.
    * The design of the records, the model to be fitted to them and the per-code
      descriptors: ``docs/developer_guide/campaigns/2026-10-05``.
    * ``seamm_exec.testing``: helpers for testing a code step's run path end to
      end -- a flowchart built from a spec and run by ``run_flowchart`` with its
      own HOME and root, a fake program replaying a real run's output for CI, the
      installed program found as SEAMM finds it, and the results read back.
    * Requires seamm-util 2026.9.27.1.
2026.10.5.1 -- Calculations on the TaskServer; retries within the queue's limit
    * A calculation run by the TaskServer (seamm_scheduler 2026.10.5) sees its own
      cores and memory.
    * A task that ran out of time is retried with more time, but never more than the
      queue's longest walltime (``max_walltime``).
    * The copies of a bundle's directories left on a cluster without a shared
      filesystem are removed once they have been brought back.
    * ``seamm_exec.timing``: plug-ins can append their timing records to
      ``~/.seamm.d/timing/<program>.csv`` safely when many runs write at once, and the
      files are set aside when large instead of growing without bound.
    * Requires seamm-scheduler 2026.10.5.
2026.10.5 -- The iterations of a parallel loop as tasks
    * Each iteration of a loop run in parallel (loop_step 2026.10.5) is a task of the
      program ``seamm``, whose resolver runs ``run_flowchart`` on the machine that
      runs it, within the iteration's share of cores and memory (``SEAMM_CE``). The
      iteration's evaluator resumes the job's flowchart into just that iteration,
      never from the top, and keeps its files in the iteration's ``_evaluator``
      directory; ``seamm_exec.iteration`` merges what it did back: the database, the
      job-level files (appended parts in order, other files as the last iteration
      left them) and the citations, all safe to redo after an interruption.
    * ``Task.keep`` leaves files in place when a task run in its directory finishes;
      a ``TaskSet`` finds the job's ``target.json`` in the job directory.
    * Requires molsystem and seamm 2026.10.5.
2026.10.4.1 -- Resume a stopped flowchart; task retries
    * ``run_flowchart --resume`` (or ``SEAMM_RESUME=1``, which the JobServer sets
      when it resubmits a lost job) continues a job in its own directory from its
      checkpoint: at the first step, or Loop iteration and step, that had not
      finished. Each step's database writes are saved with the checkpoint when the
      step finishes; a step stopped part way runs again and reuses its finished
      calculations. A rerun without ``--resume`` starts from the top as before, and
      says so if the previous run could have been resumed. See "Resuming a job" in
      the documentation.
    * Resuming keeps ``references.db``, adds to ``job.out``, records the resumes in
      ``job_data.json`` and notes packages whose versions changed since the
      checkpoint. A relative ``--database`` is relative to the job directory.
    * A task that stopped only because the evaluator stopped (killed, out of
      walltime) no longer uses up one of its three attempts, so a job resumed
      several times still runs it.
    * A task whose bundle the queue stopped for running out of time is retried with
      twice the estimated time, and twice that again after a second timeout, within
      ``bundle_walltime``. Before it was retried with the same time and could never
      finish.
    * Requires seamm 2026.10.4, molsystem 2026.10.4 and seamm-scheduler 2026.10.4.

2026.10.4 -- Exit status on failure; rerunning a job in its own directory
    * ``run_flowchart`` exits with status 1 when the flowchart fails, after
      recording the failure as before, so batch scripts, pipelines and tasks see
      it. It exited 0 (#40).
    * Running a flowchart again in a job directory that already has a job
      database (e.g. after a failure) moves the old ``seamm.db`` and
      ``references.db`` to ``previous/<date and time>/`` and starts from the top,
      saying so in ``job.out``. The step directories stay, so finished
      calculations are reused. Before, the stale database made the first Read
      Structure step fail (#41). Not done for ``--read-only`` or a ``--database``
      elsewhere.

2026.10.3.2 -- Bugfix: task bundles: one node, enough time; inline only tasks that fit
    * A bundle of tasks always runs on one node: a task's ranks share the node and
      its node-local scratch. Before, a bundle of 8-rank VASP tasks could be spread
      over several nodes, and ranks without the inputs failed (#37).
    * A cheap task runs in the evaluator itself only if its cores fit the
      evaluator's allocation; otherwise it goes to the queue. Before, a 4-rank ORCA
      calculation could run in a 1-core evaluator and fail for lack of slots (#38).
    * A bundle whose tasks give no walltime now asks for twice their estimated time
      plus ten minutes, within the bundle limit (the step's ``bundle_walltime``),
      instead of the queue's default. Before, a bundle of eight 8-minute VASP
      fragments got a one-hour default and was killed in its last task.

2026.10.3.1 -- Tables in the job database; resources for batch calculations
    * The flowchart's database is committed after every step, which is part of
      keeping tables in the job's database (seamm 2026.10.3).
    * Before a flowchart starts, steps too old to work with tables in the database
      (table_step, loop_step, properties_step, geometry_analysis_step before
      2026.10.3) are refused with a message saying what to update.
    * The ``Evaluator`` takes ``resources`` (ranks, memory per rank) for each
      calculation on the batch path and passes them to the program's ``get_task``,
      so e.g. ORCA fragments can run on 4 ranks. Without it, nothing changes.
    * Documented the stress contract: a program that returns a stress declares
      whether it is a pressure or a stress (``stress_convention``), for both the batch
      and MDI paths.

2026.10.3 -- One model chemistry, many structures: over MDI or as tasks
    * A new ``Evaluator`` gives a step the energies, gradients and stress of many
      structures with the flowchart's model chemistry, and chooses how they are
      computed: as tasks on a queue target, otherwise a warm MDI engine, unless the
      program prefers tasks (ORCA). Both ways give the same numbers, and the task
      way reuses finished structures when a job is rerun. The Energy step, the Dimer
      Builder, Normal Mode Sampling and ORCA's counterpoise correction use it.
    * A structure that a program cannot run as a task (a periodic system for ORCA or
      MOPAC) goes to its MDI engine; if there is none here, that structure fails
      with the reason and the others finish.
    * A task that names only its program is now configured on this machine too, from
      ``<root>/<program>.ini`` and the program's resolver, as it already was on a
      cluster. Tasks that carry their own configuration run exactly as before.
    * Sub-calculations that run one at a time on the whole machine, such as the
      parts of a counterpoise correction, now get every core's threads instead of
      one each.

2026.10.2.1 -- Tasks on a queue: SLURM and PBS back ends for the task layer
    * A job's tasks can now run as batch jobs of a queueing system. The job's target
      (a section of the JobServer's ``<root>/<jobserver-name>.ini`` with
      ``tasks = queue``, which the JobServer writes into the job directory as
      ``target.json``) sends them to SLURM or PBS on this machine or, over ssh, on a
      cluster, with the task directories copied there and back when the filesystem
      is not shared. Small tasks share an allocation in bundles (``bundle_tasks``,
      ``bundle_walltime``), and ``max_queued_tasks`` respects per-user queue limits.
    * Each program is configured where it runs, from that machine's
      ``<root>/<program>.ini``, so a code need not be installed where the flowchart
      runs. Plug-ins can register a resolver for their program (entry-point group
      ``org.molssi.seamm.exec.resolvers``).
    * A rerun polls bundles still in the queue instead of submitting them again, a
      bundle that ran out of time leaves its finished tasks done, and a network
      outage on the evaluator's side (a laptop asleep, a VPN) never turns running
      tasks into lost ones.
    * Without a target, or with ``tasks = pool``, nothing changes. Steps that
      configure their program themselves (ORCA and MOPAC today) keep running on the
      flowchart's machine when the target is a remote cluster.
    * ``computational_environment()`` no longer fails in a SLURM job submitted
      without ``--ntasks``, expands node lists such as ``tc[053,059-061]``
      correctly, and recognizes PBS jobs.
    * Requires ``seamm-scheduler`` 2026.10.2.
2026.10.2 -- The task layer: external codes as tasks, with restart
    * Steps can now hand their external calculations to ``seamm_exec`` as *tasks*: the
      program's name, its input files, the command, what it needs (cores, memory) and
      the files to keep. A ``TaskSet`` runs any number of them concurrently in a pool
      sized to the machine or the SLURM allocation, each in its own process group, and
      records each task's state in ``tasks/manifest.json`` in the step directory with a
      ``DONE`` marker per task. Rerunning a job in the same directory never recomputes
      a finished task: its results are restored from the marker.
    * A task whose program exits 0 but reports failure (ORCA's "error termination")
      can declare the text that marks success, so it is counted as failed and tried
      again. A failed task is not retried within a run; across reruns it is tried up
      to three times, and the count resets when its inputs change. A rerun kills a
      process the previous run left behind, only after checking that it is the same
      process, and never kills a process started under ``nohup``.
    * Finished tasks can be packed into one tar per bundle to keep the number of files
      down, and a bundle worker script runs a list of tasks in one allocation for the
      scheduler back ends of a later release.
    * Nothing changes for plug-ins that have not been converted: ``executor.run()``
      behaves exactly as before and writes no new files. ORCA and MOPAC (orca_step and
      mopac_step 2026.10.2) are the first converted steps.
    * The MOLSSI shared CI now runs on uv: ``devtools/conda-envs/test_env.yaml`` is
      removed, so ``pyproject.toml`` is the one dependency list.

2026.9.27 -- Datastore credentials by installation
    * A flowchart run straight into a datastore looked for its credentials in
      ``~/.seamm.d/seammrc`` under ``[Dashboard: dev]`` when the root's path contained
      "dev", and ``[Dashboard: localhost]`` otherwise. It now first tries a section
      named after the installation's root directory (``[Dashboard: SEAMM_DEV]``,
      ``[Dashboard: SEAMM_NEW]``, ...), so each of several installations can have its
      own, then falls back to the old sections and the host name as before.

2026.8.8 -- Internal: stop writing job status directly to the datastore under a JobServer
    * When run under a JobServer, a flowchart no longer writes its own
      terminal status directly to the datastore -- the JobServer now reads
      ``job_data.json`` (already written unconditionally beforehand) and
      writes the datastore itself. This removes a duplicate/racy write, and
      is required for a JobServer that dispatches to a remote SLURM cluster
      with no shared filesystem, where the running job cannot reach the
      datastore file at all. No effect on running a flowchart by hand
      against a Dashboard-connected datastore.

2026.8.6 -- Bugfix: job_data.json header missing its newline on the error path
    * ``run_from_jobserver()``'s exception handler wrote the ``!MolSSI job_data
      1.0`` header without the trailing newline every other writer of this file
      uses, so the header text and the JSON blob landed on the same first line.
      Any reader that does ``readline()`` then ``json.load()`` -- including
      ``seamm_datastore.Job.parse_job_data`` -- silently failed to parse the
      file and treated it as absent whenever a job failed via this exact path.
      Now uses the same ``header_line`` constant as every other writer.

2026.8.1 -- Bugfix: avoid NFS-unsafe scratch I/O for MPI codes under a scheduler
    * ``Base.run``'s ``in_situ`` option now defaults to auto-detecting whether to
      run a code directly in the job directory or in a private temporary
      directory. Under a batch scheduler (currently SLURM) it now runs in a
      temporary directory, which honors ``$TMPDIR`` and so lands on node-local
      scratch on sites that set it, instead of the job directory -- which is
      commonly NFS-mounted shared storage and unsafe for an MPI code's parallel
      scratch I/O (see molssi-seamm/orca_step#20). Outside a scheduler, or when a
      step explicitly requests one or the other, behavior is unchanged. Steps opt
      in by passing ``in_situ=None`` instead of ``in_situ=True``.
    * ``Base.run`` now reports where a code actually ran: the returned result
      dictionary includes ``in_situ`` (bool) and ``directory`` (the run
      directory actually used), so a step can tell the user in job.out/step.out
      whether it ran in place or in scratch, and where.
2026.7.15 -- Bugfix: avoid "database is locked" errors when jobs start together
    * When registering and finishing jobs, SEAMM now waits a short, configurable
      time (the "database-timeout" option, default 20 seconds) for the job
      database to be free instead of failing immediately with "database is
      locked". This fixes failures seen when many jobs start at the same time,
      for example a batch of jobs on a cluster.
