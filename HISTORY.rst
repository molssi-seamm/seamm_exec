=======
History
=======
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
