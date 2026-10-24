=======
History
=======
2026.10.24 -- Jobs share a queue fairly, and bundles say whose they are
    * A queue's max_queued_tasks is now shared equally between the jobs with
      bundles queued, so one job's long bundles cannot hold every place while
      another job waits (2026-10-24: an atom-energy campaign held 590 of 600
      places for hours, and shorter labelling jobs got a trickle). A job alone may
      use all of it.
    * The count is of the jobs in the section's QOS, since sites limit a user's
      jobs per QOS, so queues in different QOSes no longer hold each other back.
    * Bundles are named s<job>.<bundle>-<random>, e.g. s5396.12.1-2ff93c for bundle
      12 of SEAMM job 5396, so squeue's narrow name column shows whose each is
      (they all showed as 'seamm-bundle'). Bundles already queued keep their names.
    * A task set can limit its own bundles queued at once (max_queued), which a
      Loop's new 'Queued at most' sets.
    * Needs seamm-scheduler 2026.10.24 for the per-QOS count and the shares;
      with an older one it counts all of the user's jobs, as before.

2026.10.23 -- A calculation stopped part way leaves its output
    * A calculation running in scratch (node-local storage under a scheduler) had
      its files put in place only when it finished, so one stopped by the job's time
      limit or a cancel lost them with the scratch -- e.g. the orca.out of large ORCA
      calculations that timed out, which would have shown where the time went. While
      it runs, its returned files (each up to 50 MB) are now copied to its directory
      every 10 minutes as '<name>.partial', and once more when the evaluator is
      stopped (SIGTERM). A calculation that finishes removes the copies.
      SEAMM_PARTIAL_INTERVAL sets the seconds between the copies.

2026.10.20 -- Bugfix: a refused resume reran the job over its files
    * When a job asked to resume could not, it ran again from the top in the same
      directory, rewriting the finished steps' output and job.out. It now stops
      without running anything, saying why, and leaves the job's files as they
      were; the job ends in error, so the JobServer does not run it again. To run a
      job again from the start, resubmit it without asking to resume.
    * A run from the top in a directory that already holds a run now keeps that
      run's job.out in previous/<time>/, with its database, instead of deleting it.
    * Uses the resume check of seamm 2026.10.20 when it is installed, and works with
      earlier versions of seamm.

2026.10.19 -- A calculation run alone in a job gets the job's cores
    * Structures evaluated as tasks in a job's own cores, e.g. by the Energy step, ran
      on one core each, which suits many structures side by side. An optimization or
      scan evaluates one at a time, so the other cores stayed idle (ORCA ran serially
      on 8 allocated cores). For a program whose tasks can use several cores, fewer
      structures than cores now share the cores and memory out equally. Tasks sent to
      a queue, and the resources a step sets, are unchanged.

2026.10.18 -- Returned files are no longer read into memory
    * ``executor.run()`` read every returned file into memory, and the task layer kept
      each file's contents in its results. An MD run returns gigabytes of trajectories,
      so several runs at once could exhaust a machine. The files are now left on disk:
      ``result[name]`` gives the ``"path"`` of each, not its ``"data"``, and a task's
      ``result.files`` gives where each file is, with ``result.read(name)`` to read one
      from disk or from the task archive. Files from a scratch directory are moved back
      rather than copied through memory. Steps that used the contents must read the
      files, and need this version.

2026.10.17 -- Bugfix: full bundles of tasks had no margin for slow tasks
    * A bundle of tasks asks for twice their estimated time plus ten minutes, within
      the target's bundle walltime. But tasks were packed until their estimates alone
      reached the bundle walltime, so a full bundle got only about its estimate and ran
      out of time whenever the tasks were a little slower; the retry was capped in the
      same way. Tasks are now packed so that what the bundle asks for, margin
      included, fits within the bundle walltime. A single task too long for the limit
      with its margin is warned about.

2026.10.16.3 -- Bugfix: finished tasks ran again after an evaluator was stopped
    * When a bundle of tasks finished, its task directories were packed into the
      bundle's archive and removed before the task manifest recorded the archive, and
      the manifest is written at most once a second and may wait while the evaluator
      is busy. An evaluator stopped in between -- cancelled, or stalled -- left tasks
      recorded with neither their directory nor the archive, so a restarted evaluator
      ran them again. The manifest now records the archive before the directories are
      removed, and tasks that an earlier version left that way are found in their
      bundle's archive and not run again.

2026.10.16.2 -- Where each time estimate came from
    * Each timing record says where the task's estimate came from, in a new column
      ``estimate_source``: ``model`` (the fitted cost model), ``formula`` (the step's
      own rough formula) or ``none``. ``seamm-timing check`` flags groups with
      estimates from the formula.
    * When the model cannot estimate a calculation, the step's log says why, once per
      reason: as a warning when the step did not give a size the model needs, which
      is a bug that makes every estimate come from the formula, otherwise as
      information (no model yet, or a calculation unlike any in the records).
    * The timing commands are available as ``seamm-timing`` (``fit``, ``predict``,
      ``check``), which also avoids the warning that ``python -m
      seamm_exec.timing_model`` printed.
    * For step developers: ``timing_model.estimate()`` gives an estimate with its
      source and the reason, and ``timing_model.missing_sizes()`` checks in a step's
      tests that its estimates have every size its timing records have.

2026.10.16.1 -- Checking the time estimates against the times
    * A new command, ``python -m seamm_exec.timing_model check [program] [--since 2h]``,
      compares the estimates the tasks were given with the times they took, from the
      timing records, for each machine and calculation. It flags groups whose
      estimates are more than twice or less than half the times, and calculations
      estimated the same on machines of different speed -- the sign that a step fell
      back to its own rough formula instead of the fitted model. Run it after every
      update: it would have caught both of the ORCA estimate errors of 2026-10-16 in
      the first finished runs.

2026.10.16 -- Bugfix: writing a timing record no longer holds up the Evaluator
    * Every run appends a row to the shared timing file, under a lock. On a cluster
      the file is on a network file system, whose lock daemon can take many seconds
      to hand the lock on, and a run waited for it. Many Evaluators writing at once
      took turns at the lock, which capped how fast they could take in results and
      submit more tasks. A run now tries for the lock for a second at most, and
      otherwise writes its row to a file of its own,
      ``~/.seamm.d/timing/<program>.pending/<host>-<pid>.csv``. It moves those rows
      into the shared file the next time it gets the lock, and the rows are read with
      the others in the meantime.

2026.10.15 -- Checking the flowchart before a job runs
    * A job now checks its flowchart as a whole before running any step (with
      ``seamm.builder.validate``), and stops at once if there are problems, for
      example an analysis step whose MD does not sample what it needs. Before, such a
      job ran all its MD and failed only at the analysis. An iteration of a parallel
      loop is not checked again, and a failure of the check itself never stops a job.

2026.10.11 -- Bugfix: longer walltimes on retry; cancelled jobs no longer keep running
    * A task that gives its own walltime was retried after running out of time with
      that same walltime, so it ran out of time again on every retry. Its walltime is
      now doubled after each timeout, as an estimate's is, within the queue's longest
      walltime.
    * The Loop step's "time per iteration" is an estimate, and is now treated as one:
      a bundle of iterations asks for twice their estimates plus ten minutes, within
      the target's guards (estimate_scale, min_task_walltime). Before, it was used as
      the exact walltime, with no margin.
    * A cancelled job could keep running outside the queue. While running tasks, a
      flowchart catches SIGTERM to stop them first, and it kept doing so afterwards.
      Python acts on a caught signal only between its own instructions, so a
      flowchart busy in compiled code (e.g. RDKit building a large structure) ignored
      the cancel. Under Slurm's proctrack/linuxproc the job's shell then died first
      and the flowchart was lost to Slurm. The handler is now removed once no task is
      running, so a busy flowchart stops at once.

2026.10.10.4 -- Bugfix: the parallel exponent survives new size columns
    * The parallel exponent comes from paired runs -- the same calculation on several
      core counts. The fit dropped every row lacking a size column added later (e.g.
      ORCA's "neighbours", from 2026-10-09), and with them all the paired runs, which
      were older; the exponent was then assumed. Paired runs now come from every
      usable row, whatever size columns it has.
    * A refit that finds no paired runs keeps the exponent the previous fit measured,
      rather than assuming one.

2026.10.10.3 -- Bugfix: estimates too low when every run used the same number of cores
    * When all of a program's timing records were on one core count, the cost model
      assumed the parallel exponent but fitted the times without it, so every
      prediction for a run on n cores was low by about n**0.8 (3x on 4 cores), while
      the model reported itself accurate. Bundles then asked for far too little time
      and ran out of it. The fit now applies the assumed exponent as the predictions
      do.
    * Each version of the model is kept in its own file (models/<program>.v3.json),
      so a long job still running an older seamm-exec cannot replace the model with
      one from its older fit.
    * Two guards in a target section against estimates that are too low:
      estimate_scale multiplies each task's estimate, and min_task_walltime gives
      each task at least that much time in its bundle. Requires seamm-scheduler
      2026.10.10.

2026.10.10.2 -- Option flags in the cost model, learned where the records contrast them
    * A step may name the column holding a run's options (``flags`` in its spec, with
      the words that are not options: the task, basis sets, method). Each option gets a
      factor of its own, shrunk toward none -- but only where the records contrast it:
      at least three calculations run both with and without it on one machine. Fitted
      on ARC's ORCA records without that rule, the factors took up differences between
      machines and campaigns, and Owl's predictions got worse.
    * The fit's report lists the learned option factors, and possible cost drivers:
      recorded values and options without a factor whose runs are still
      systematically off.
    * A prediction with an option the records have never seen gets a wider spread
      above the median.
    * A model is refitted when the step's spec, or the fitting method, has changed.
    * A seed-benchmark chemistry may carry its own variants (a toggle ladder: a few
      runs repeated with one option changed), instead of its task's.

2026.10.10.1 -- The cost model's parallel start-up and parallel exponent from paired runs
    * A parallel run pays a fixed start-up that a serial one does not -- launching the
      MPI processes: for ORCA about 11 s on Owl and 1-3 s on TinkerCliffs, nearly the
      same for 4 or 16 processes. The cost model now fits it per machine, on top of
      the start-up of every run; tiny molecules, which ran slower on more cores, are
      no longer predicted to speed up.
    * The parallel exponent (how the time falls with the cores, as a function of
      size) is taken from paired runs -- the same calculation on several core counts,
      as the timing benchmarks make -- with the start-up removed, and held fixed in the
      main fit. Production cannot show it, since its core counts follow its sizes.
    * The start-up of every run is measured on the smallest runs (the gap between the
      wall time and the code's own time grows with the run).
    * On ARC's ORCA records, predictions within 2x of the time rose from 86 % to 96 %
      for Owl's production runs and from 44 % to 82 % for its benchmarks.

2026.10.10 -- Each task records the code that ran it; a set from mixed codes is noted
    * A finished task records what ran its program -- the modules loaded, the conda
      environment, or the command from ``<program>.ini`` -- in its ``DONE`` marker, the
      manifest and its result (``TaskResult.code``), from the evaluator's machine or
      the node of a queue bundle.
    * A rerun still reuses finished results whatever the code is now: for a mature
      code a new version rarely changes them. But when a set's results come from more
      than one code -- e.g. some reused from a run with VASP/6.6.0 and the rest run
      with 6.6.1 -- a warning says so, with the counts (``TaskSet.codes``), so a mixed
      campaign is visible.

2026.10.8.2 -- Bugfix: timeouts and cancellations no longer use up a task's attempts
    * A task that runs out of time no longer uses up one of its attempts, the first
      four times (``TaskSet(max_timeouts=4)``): its estimate was too low, and each
      retry asks for twice the time. Estimate-caused timeouts had exhausted the three
      attempts of the FEC pilot's ORCA tasks, which were then never resubmitted. Later
      timeouts count, so a task that cannot finish in the longest time the queue
      allows still stops.
    * Bugfix: cancelling a task set marked every task in flight ``cancelled``, even
      one that had already finished -- in a bundle still running, or between polls --
      and its result was lost to the run. Such tasks are now collected with their
      results. A cancelled task also gets its attempt back, so cancelling and
      rerunning a step no longer exhausts its tasks' attempts.

2026.10.8.1 -- Bugfix: tasks in queue bundles record their wall time; a fixed cost per run; the neighbour count
    * A step's spec may name a column (``setup_by``) whose values each get a fixed
      cost per run, in iterations, found by the fit: the work is ``iterations +
      setup``. For codes whose first step is much dearer than the rest -- MOPAC's
      MOZYME localizes the orbitals once, then runs fast cycles -- so a single point
      and a long optimization share one per-cycle cost, and an optimization much
      shorter or longer than the records' is predicted right.
    * Bugfix: a task run in a queue bundle never had a wall time in its timing record:
      the worker on the node knew when it started but that never reached the
      manifest, so every such record -- all 1,725 of the FEC pilot's on TinkerCliffs
      -- was left out of the fit. The bundle worker now writes each task's start in
      its ``DONE``, and the result carries it back. For older records the fit uses
      the code's own time in place of the missing wall time.
    * ``structure_descriptors`` adds ``neighbours``, the mean number of other atoms
      within 8 Å of an atom (``neighbour_count``, sampled over up to 256 atoms,
      periodic cells by minimum image or explicit images): how crowded a structure
      is. MOZYME's cost per atom is about 8 times higher in a 1,000-atom water cluster
      than along a 900-atom alkane chain.

2026.10.8 -- The cost model's fit: no negative exponents, an exponent per regime, weighted by time
    * No size exponent is negative any more. Related size variables (basis functions
      and electrons over a mix of basis sets) could split one effect into a large
      positive and a negative exponent, which fitted the records and predicted a
      larger basis to run faster. While any exponent is negative the fit leaves out
      the size variable whose absence fits best, and the report says so.
    * A step's spec may name a column (``slope_by``) whose values each get their
      own size exponents, for codes with regimes that scale differently: MOPAC's
      MOZYME (roughly linear) and traditional SCF (roughly cubic).
    * Each run weighs in the fit in proportion to its time. The cost exponent grows
      with size, so a fit counting every run equally bent low at the large end:
      the largest ORCA runs of the TinkerCliffs and ChemAI benchmarks were predicted
      at 0.5-0.65 of their time, and are now at 0.8-1.1.
    * In a step's ``TIMING_BENCHMARK`` a chemistry may name the tasks it runs
      (``"tasks": ["Energy"]``), so a basis-set ladder runs energies and gradients
      without optimizations.
    * Requires Python 3.12.

2026.10.7.1 -- Bugfix: a provider that does not take the task; each code step declares its own timing benchmark
    * Bugfix: 2026.10.7 passed the task to a program's ``analyze_task`` whenever
      its signature took ``**kwargs``; mopac-step's forwards them to a function
      that does not take ``task=``, so every MOPAC batch task failed in analysis
      (``TypeError``). The evaluator now falls back to calling such a provider
      without the task.
    * The seed benchmark no longer carries the codes' molecules, model
      chemistries and tasks: each code step declares its own benchmark as
      ``TIMING_BENCHMARK`` beside its ``TIMING_SPEC`` -- the systems that drive its
      cost (molecules for ORCA, boxes for LAMMPS, cells for VASP) as the steps
      that build them, its chemistries, tasks and variants with size limits per
      tier -- and seamm_exec finds them through the step entry points. seamm_exec
      keeps only the spec assembly, the core-sweep driver, the benchmark tag and
      the fit. ``--codes`` defaults to every installed step that declares one.
      Requires steps that declare it: orca-step and mopac-step from their next
      releases.

2026.10.7 -- Bugfix: runs under the task layer record their timing
    * The task layer's evaluator now passes the task that produced a result to a
      program's ``analyze_task`` when it takes ``task=`` (ORCA's and VASP's do, and
      record the run's timing from it). Before, only a step's own runs were recorded:
      every run under the task layer -- the MBE step's fragments and cells -- was
      missing from the timing records, so the EC and FEC pilots on TinkerCliffs left
      no ORCA or VASP rows for the cost model.
    * The cost model no longer predicts what its records do not cover. A fit to
      TinkerCliffs' records -- small ion-water clusters, run on 4 cores when small
      and 8 when large -- gave the MBE pilot's 20-atom fragments 10 minutes where
      they take 35, and 11 hours at a larger basis, so the pilot's bundles timed
      out one task in. Now: when the core count follows the size the parallel
      exponent is assumed rather than fitted (the fit cannot tell the two apart);
      a prediction needs every size variable, a method class and a task the
      records have seen, and a size within the fitted range (widened by 1.5x);
      otherwise there is none and the step's own estimate is used.

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
      modules and local installations run exactly as before. A ``<program>.ini``
      that still says ``installation = docker`` gets a clear message saying so.

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
