=======
History
=======
2026.7.15 -- Bugfix: avoid "database is locked" errors when jobs start together
    * When registering and finishing jobs, SEAMM now waits a short, configurable
      time (the "database-timeout" option, default 20 seconds) for the job
      database to be free instead of failing immediately with "database is
      locked". This fixes failures seen when many jobs start at the same time,
      for example a batch of jobs on a cluster.
