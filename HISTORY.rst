=======
History
=======
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
