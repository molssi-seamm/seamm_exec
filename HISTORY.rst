=======
History
=======
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
