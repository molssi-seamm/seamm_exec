2026-10-05 -- Predicting the time of calculations from timing records
=====================================================================

Status: design written 2026-10-05 and discussed with Paul. Phases 0 and 1 (the
record, the machine class, ORCA's records) released 2026-10-05 (seamm-exec and
orca-step 2026.10.5.2); Phases 4 and 5 (MOPAC, VASP, Gaussian, LAMMPS, Psi4 and
DFTB+ writing the record through ``record_timing``) released 2026-10-06
(seamm-exec and the six steps 2026.10.6) and rolled out to every installation
the same day. Phase 2 (the fit, the model file and ``predict``, in
``seamm_exec.timing_model``, and the seed benchmark in
``seamm_exec.timing_benchmark``) implemented 2026-10-06 on dev. Phase 3 (ORCA's
``estimated_seconds`` from ``predict``) implemented 2026-10-06 on orca_step dev:
``orca_base.predicted_seconds`` builds the run's descriptors before it runs --
the basis functions counted from the Basis Set Exchange's definition of the
basis for the atoms and ghosts -- and asks the model for the median (the task
layer adds its own margin), falling back to the hand formula without a model.
On this Mac's model, water B3LYP/def2-SVP is predicted at 1.44 s and took 1.24. The campaign continues the parallel-execution campaign
(``campaigns/2026-10-02``), whose task layer already carries an
``estimated_seconds`` for each task and turns it into a queue walltime
(``scheduler_backend``) and into the inline rule (``TaskSet``), and whose MBE
pilot fitted the first real cost model (``vasp_step.batch.COST_FIT``).

Purpose
=======

When a calculation goes to a queue -- as a task of a step, as a bundle of tasks,
or as a whole job -- SEAMM has to say how long it will take. Too short and the
queue kills it; too long and it waits longer for a slot, or does not fit the
queue at all. Today each code step guesses by hand (``orca_step.estimated_seconds``
is a power of the atom count times a few fudge factors) and the VASP step alone
has a fit to real data.

Several code steps have, since 2024, appended one row per run to
``~/.seamm.d/timing/<program>.csv``. The intention was always to fit a model to
those rows. This campaign designs the rows so that a model *can* be fitted, the
model itself, how a new machine is calibrated, and where the predictions are
used, in a way that covers every kind of calculation -- energies, gradients,
optimizations, Hessians, dynamics -- on every machine SEAMM runs on, rather than
one class of job on one machine.

What exists, and why it cannot be fitted
========================================

The files on this Mac on 2026-10-05:

============ ======= =====================================================================
file         rows    notes
============ ======= =====================================================================
gaussian.csv 280,565 87% from one Mac; median run 1.2 s; has ``nbf``
mopac.csv    227,890 every row ``nproc=1``; median run 1.3 s
lammps.csv   166,493 ``keywords`` is the step's whole parameter dictionary as JSON
dftbplus.csv  33,064 same
torchani.csv   9,093 same
vasp.csv         411 stores the POSCAR, INCAR and KPOINTS *text*
psi4.csv          63
============ ======= =====================================================================

Three things stop a fit:

1. **The descriptors are text, not numbers.** SMILES, formulas, raw keyword
   strings, parameter dumps and input files cannot enter a regression without a
   featurization step that would itself be the hard part. Only Gaussian records
   ``nbf``, the one column that predicts cost.
2. **The machine is the hostname.** On ARC that is a node name, so TinkerCliffs,
   Falcon and Owl are indistinguishable, and ``cpu`` is ``ARM_8`` or ``X86_64``
   with ``cpu_speed`` empty. Rows from machines of different speed are mixed with
   nothing to separate them.
3. **The rows are dominated by sub-second runs**, where process start-up, not
   the calculation, sets the time. A model fitted to them learns nothing about
   the hour-long jobs that need predicting.

The one exception shows what works. ``vasp_step.batch`` fits
``log t = a + b log Ne + c log(V (ENCUT/500)^1.5)`` to 396,528 Gamma-point single
points on TinkerCliffs: 68% of runs within a factor 1.3, 95% within 2, and a
separate exponent for the number of ranks. The design below is that fit made
general.

Requirements
============

- Predict the wall time of a *calculation* -- one run of one code -- from what
  is known before it runs: the structure, the method, the task, the cores, the
  machine class.
- Return a **safe** time for a queue, not an expected one: a high quantile of
  the error distribution, since an under-estimate kills the job and an
  over-estimate only delays it.
- Cover all kinds of calculation with one model *shape* per code: single
  points, gradients, optimizations, analytic and numerical Hessians, dynamics.
- Separate the machine from the calculation, so that rows from any machine
  improve the fit, and a new machine needs a few minutes of standard runs to
  be calibrated.
- Extrapolate sensibly: the job being submitted is often larger than any in the
  history.
- Cost nothing at prediction time beyond numpy (the predictor runs in the
  evaluator, in the JobServer and in the GUI); the fit may use pandas and
  scipy/scikit-learn, in a command run occasionally.
- Keep writing rows safely from many nodes at once on a network file system
  (``seamm_exec.timing.append_timing`` already does), and let the schema evolve
  without corrupting old files.

Design
======

1. The record
-------------

Every code step writes one row per run through one call,
``seamm_exec.timing.record_task_timing(task, result, descriptors)``, after it
has parsed the output. The row has three parts:

**Common columns**, filled by ``seamm_exec`` from the ``Task``, the
``TaskResult`` and the machine:

========================================== ======================================================================
column                                     meaning
========================================== ======================================================================
schema                                     the record schema version (``1``)
date                                       UTC ISO timestamp of the run's end
machine                                    the machine class key (below)
cluster, partition, cpu_model, cpu_cores   the parts of the key, and the node's cores
host                                       the hostname, for forensics only
program                                    ``orca``, ``mopac``, ...
ntasks, cpus_per_task, mem_per_cpu         the task's resources as run
wall                                       seconds from the task's start to its end, from the task manifest
estimated                                  the ``estimated_seconds`` the step gave the task (to score it)
state                                      ``finished`` or ``failed``
timed_out                                  whether the queue stopped it
attempts                                   how many attempts the task took
in_situ                                    whether it ran in place or in node-local scratch
========================================== ======================================================================

**Descriptors**, numbers and short categorical values from the step, chosen so
that the cost model below can be fitted. They are per code (section 6), but the
names are shared where the meaning is: ``task`` (energy, gradient, opt, freq,
numfreq, md, ...), ``method_class``, ``method``, ``basis``, ``n_atoms``,
``n_heavy``, ``n_electrons``, ``nbf``, ``charge``, ``multiplicity``.

**Outcomes parsed from the output**: the iteration counts that let the fit learn
a per-iteration cost (``scf_cycles``, ``scf_runs`` -- geometry steps or
displacements -- ``md_steps``) and the code's own clock (``code_seconds``), which
separates the calculation from queue and copy-in/out overheads. Failed runs are
recorded too, with their state: a model of *how often* a method fails on a size
of system is worth having, and a wall time with no iteration count says the
run was killed.

A row is not written for a restored result (the task layer found the finished
run of an earlier attempt), nor when the step only wrote input.

Rows go to ``~/.seamm.d/timing/<program>.csv`` through ``append_timing``, which
locks, writes a row in one ``write`` and sets a file aside when it is large.
One change: when a row has columns the file's header lacks, the file is set
aside and a new one begun with the new header, instead of the columns being
dropped. So a schema change starts a new file and the fit reads every file for
a program, current and set aside, keeping the rows whose ``schema`` it
understands.

2. The machine class
--------------------

The key that separates machines of different speed is not the hostname but

    ``machine = cluster : partition : cpu_model``

with empty parts omitted: ``tinkercliffs:normal_q:AMD EPYC 7702 64-Core
Processor``, ``falcon:a30_normal_q:AMD EPYC 7413 24-Core Processor``,
``Apple M3 Pro`` for the laptop. The cluster and partition come from the
scheduler's environment (``SLURM_CLUSTER_NAME`` and ``SLURM_JOB_PARTITION``;
``PBS_SERVER`` and ``PBS_QUEUE``), the CPU model from ``/proc/cpuinfo`` or
``sysctl machdep.cpu.brand_string``. ``seamm_exec.timing.machine_class()``
returns these, cached per process. A GPU model is added to the key when the task
asked for GPUs, so a code's GPU and CPU runs on one node are distinct classes.

The same CPU in two clusters gets two keys. That is deliberate: memory, network
and file systems differ, and the hierarchical fit (below) pools them anyway when
their speeds agree.

3. The model
------------

For each code the wall time of a run is modelled as a product of separable
factors, fitted as a sum in log space::

    log(t - t0) = log u(size, method_class) + log m(task, iterations)
                  + log p(cores) + s(machine) + noise

- **t0**, a start-up constant per code and machine class, fitted from the
  smallest runs and subtracted outside the log, so that the sub-second runs do
  not corrupt the power laws.
- **u**, the cost of one unit of work -- one SCF energy, one energy+gradient,
  one MD step -- as a power law in a few physics-motivated size variables:
  ``log nbf``, ``log n_electrons``, ``log n_atoms`` for molecular codes; valence
  electrons, cell volume, cutoff and k-points for plane waves; atoms and
  neighbours for force fields. Each method class has its own intercept and,
  where the data support it, its own exponents: HF and hybrid DFT scale as
  roughly N^2.5-3 with RI/COSX, MP2 as N^4-5, coupled cluster as N^7,
  semiempirical as N^2-3, force fields as N.
- **m**, the task multiplier: an optimization is ``scf_runs`` gradient units, a
  numerical Hessian ``6N+1``, dynamics ``md_steps``. Because the rows record the
  iteration counts actually taken, ``u`` is fitted cleanly to unit cost, and the
  *distribution* of iteration counts for a task -- how many geometry steps an
  optimization of this size typically takes -- is fitted separately and entered
  at its own high quantile. This decomposition is what lets one model serve
  every task.
- **p**, the parallel factor per code, Amdahl's law or ``cores^-alpha`` with a
  fitted ``alpha`` (VASP's is 0.5 above 8 ranks). It needs rows at several core
  counts, which the seed benchmark provides and which routine runs on a cluster
  will add.
- **s**, the machine-class offset, per code and machine class, fitted as a
  random effect: a class with few rows is shrunk toward the pooled mean, one
  with many stands on its own. This is the calibration: a standard benchmark
  gives a new class its offset directly.

The target is a **quantile**, not the mean. The fit's residuals in log space give
the spread; the predictor returns ``exp(prediction + z_q * sigma)``, with the
quantile chosen by the caller: the 95th for a queue walltime, the median for
packing tasks into a bundle, which only needs the sum right. The spread is per
code and task (optimizations scatter more than single points), and reported
alongside, so the task layer's "3x the estimate, at least an hour" rule can
become the quantile the data support.

The fit is linear regression in log space with categorical offsets (ridge; a
mixed model when the machine effect is fitted properly), not a tree or neural
model. Power laws extrapolate to larger systems and more cores than the history
holds, which is exactly the case that matters when a job goes to a queue;
tree models predict the largest case they have seen. A gradient-boosted model
may later refine residuals *within* the range of the data.

4. Fitting, storing and using the model
---------------------------------------

*Implemented 2026-10-06 as* ``seamm_exec.timing_model`` *(Phase 2). Notes from
the implementation:* size variables whose log values correlate above 0.98
(electrons and basis functions of one basis set) are reduced to the first, since
the fit cannot tell them apart and would split a slope arbitrarily; the
start-up constant is a fraction of the 5th percentile of each machine's smallest
runs, the fraction (0, 0.3, 0.6 or 0.9) chosen by the fit that leaves the
smallest residuals; machine offsets are shrunk by 5 rows toward the pooled fit
and centred into the intercept; residual quantiles are kept per task and
overall; a size variable missing at prediction time takes the records' mean.
The model is pure numpy and the file is JSON, as designed. The ``seamm-exec
timing fit`` subcommand below is ``python -m seamm_exec.timing_model fit``.

*Two refinements from the first benchmark on the Mac (2026-10-06):* where the
code reports its own time, the start-up constant is measured directly as the
median of wall minus code time (about 1 s for MOPAC, 0.2 s for ORCA here), and
runs whose time is nearly all start-up are left out of the power-law fit (they
are predicted by the constant alone); and the parallel exponent depends on
size, ``alpha = a0 + a1 (log size - mean)``, because small molecules gain
nothing from more cores while caffeine ran 3x faster on 5 cores than on 1 -- a
single exponent fitted to both came out as zero.

*Where the specification lives (2026-10-06, Paul's ask):* each code step
declares what its cost model is made of (``TIMING_SPEC`` in the step: size
variables, method-class columns, task column, unit column, multiplier, default
parallel exponent) and passes it when it records a run; seamm-exec writes it
once as ``~/.seamm.d/timing/<program>.spec.json`` and the fit reads it from
there. So the spec travels with the data, the fit imports no plug-in, a
third-party code joins by writing records and a spec, and ``predict`` needs no
spec at all (the model file carries its columns). VASP's computed grid variable
became a descriptor the step writes. A fallback table for the 2026.10.6 steps
stays in seamm-exec until each has released its spec.

*Keeping the model current (2026-10-06):* ``predict`` calls
``refresh_if_stale`` first: the model records the bytes and date of the record
files it was fitted from (a stat, not a read, tells whether they have grown);
growth of a fifth, or a week's age with changed records, triggers a refit under
a non-blocking ``lockf`` lock (another process refitting means this one uses the
model as it is), and the new model replaces the old only if it has at least 90%
of the rows and predicts within 2x at least as often, less a tenth. A refit that
fails or is refused notes the records' state so it is not retried on every
prediction. The ``fit`` command stays for reports and refits on demand.

*And from the core sweep:* a sweep must stay within one kind of core. This
Mac has 5 performance and 6 efficiency cores; its 8-process ORCA runs were
slower than its 4- and 5-process ones, and one aborted in OpenMPI's shared-memory
set-up. The driver therefore sweeps only to the performance cores on Apple
silicon (``hw.perflevel0.physicalcpu``), to the physical cores elsewhere; the
Mac's 8-core rows were removed from its records. On a cluster node the cores
are alike and the sweep can go to the node's width.

``seamm-exec timing fit [program]`` (a subcommand, or ``python -m
seamm_exec.timing``) reads every timing file of a program, drops rows whose
schema it does not know, fits the model above and writes
``~/.seamm.d/timing/models/<program>.json``: the coefficients, the exponents per
method class, the parallel factor, the machine offsets with their row counts,
the residual spread per task, the date and the row count. It prints a report:
rows used, R^2 in log space, the fraction within 1.3x and 2x, the worst
machine classes. Pandas and scipy are allowed here.

``seamm_exec.timing.predict(program, descriptors, ntasks, machine=None,
quantile=0.95)`` loads the JSON (cached) and evaluates it with numpy alone. With
no model for the program, or no offset for the machine, it falls back: the
pooled fit without a machine offset, then the step's own hand estimate, saying
which it used.

The machine-independent part of a fit can ship *with the step* -- as
``vasp_step.batch.COST_FIT`` does today -- so a fresh installation predicts
reasonably before it has any rows of its own, with only the machine offset
local. Whether to publish fitted models centrally (the ``seamm_packaging``
nightly, Zenodo) is left open: the mechanism is one JSON per program either way.

Where predictions are used, in order of adoption:

1. The ``Task.estimated_seconds`` each step sets today, which the scheduler
   back end turns into a walltime and the ``TaskSet`` into the inline decision.
   The step calls ``predict`` with its descriptors instead of its hand formula.
2. Bundling and the TaskServer: the sum of median estimates sizes a bundle, the
   quantile of the sum sets its walltime.
3. The whole job's walltime in the JobServer, when a flowchart's steps can each
   estimate their own tasks and the loop counts are known (a loop over a table
   or a structure file is countable; a convergence loop is not, and the
   flowchart's own hint stays). A job estimate is a sum over steps, each at its
   quantile, so it is conservative; that is the right side to err on.
4. The GUI: the step dialog shows the estimated time for the current structure
   and settings, when a model and a machine class are known.

5. Seeding a new machine class
------------------------------

*Implemented 2026-10-06 as* ``seamm_exec.timing_benchmark`` *(the rest of
Phase 2). The flowchart is built from a spec at run time with the installed
plug-ins rather than shipped, since a shipped flowchart would pin plug-in
versions; molecules are water, ethanol, toluene, caffeine, icosane (62 atoms)
and hectane (302 atoms, MOPAC only, in both regimes); the core sweep is run
by capping* ``SEAMM_CE`` *per run. The installer and JobServer hooks below are
not yet written.*

A standard flowchart, ``seamm_exec/data/timing_benchmark.flow`` (Phase 2), runs
per code a few molecules spanning two orders of magnitude of size, two or three
method classes, each as an energy and a gradient, and a core sweep of 1, 4, 8
and 16 where the code is parallel. It takes minutes on one node. Its rows carry
``benchmark=1`` so the fit can weight them (they are chosen to span the space)
and so that a code or compiler change can be checked against earlier runs of
the same flowchart. The installer offers to run it; the JobServer runs it when
it first sees a machine class with no offset.

The seed gives the machine offset ``s`` and the parallel exponent for that
class. Everything else -- the exponents, the method-class intercepts, the
iteration-count distributions -- comes from the pooled history, so a new
machine is calibrated by a handful of runs, as Paul proposed.

6. Descriptors per code
-----------------------

**ORCA** (Phase 1, done): ``task`` from the keyword line (energy, gradient,
numgrad, opt, freq, numfreq); ``method_class`` from the model string via the
step's metadata (HF, local, GGA, meta-GGA, global hybrid, range-separated
hybrid, double hybrid, MP2, DLPNO-CC, CC, semiempirical); ``method``,
``basis``, ``model``; ``n_atoms``, ``n_heavy``, ``n_ghosts``, ``charge``,
``multiplicity``; parsed from ``orca.out``: ``n_electrons``, ``nbf``,
``scf_runs``, ``scf_cycles``, ``code_seconds`` (ORCA's TOTAL RUN TIME); from
the input: ``maxcore_mb``, ``ri`` (RIJCOSX/RIJK/NoCOSX) and ``dispersion``.

**MOPAC** is the special case Paul raised. It runs on one core, so the parallel
factor is 1, but it has **two regimes with different scaling**: the traditional
SCF, between N^2 and N^3 in the number of basis functions (4 per heavy atom, 1
per hydrogen), and MOZYME, the localized-orbital method, roughly linear in N.
The step switches to MOZYME at ``nMOZYME`` atoms (default 300) when the setting
is "for larger systems", always or never otherwise; MOZYME handles most but not
all calculations, so the regime that actually ran is read from the output, not
assumed from the input. The rows therefore carry ``regime`` (``scf`` or
``mozyme``) and the model fits ``u`` per regime, with a free exponent for each,
and a Hamiltonian offset (PM7, PM6-ORG, ...). The prediction for a system near
the threshold is the regime the settings would pick, and the fit's report shows
where the two curves cross so the default threshold can be checked against
data. ``task`` separates 1SCF, gradients, optimizations (``scf_runs`` from the
geometry cycles), force constants and IR; the follow-up calculation after
MOZYME is a second row with ``task=follow-up``.

**Gaussian**: as ORCA; ``nbf`` is already parsed; ``method_class`` from the
functional tables; symmetry (used or not) as a flag, since it changes the cost
several-fold.

**VASP**: the variables of the existing fit -- valence electrons, cell volume,
``ENCUT``, k-points -- plus ``ALGO``, ``EDIFF``, ``PREC``, spin, the
``NELM``/ionic steps taken and VASP's own ``Elapsed time``; the rows replace the
input-file text with these numbers.

**LAMMPS**: atoms, MD steps, the pair style and cutoff, whether k-space is on,
the forcefield class (classical, MLFF with its model) and the GPU; the unit is
one step per atom.

**Psi4, DFTB+, TorchANI, xTB**: the molecular set of descriptors, with the
method class the code uses.

7. Phases
---------

========= ====================================================================================
Phase     Content
========= ====================================================================================
0 (done)  ``seamm_exec.timing``: ``machine_class()``, ``record_task_timing()``, files set
          aside on a header change; tests.
1 (done)  ``orca_step`` records the new rows from ``run_orca_job`` (every ORCA run: Energy,
          Optimization, Frequencies, counterpoise sub-jobs, the model-chemistry task);
          ``method_class`` and the output parsing above; tests on the recorded output.
2 (done)  The fit: ``python -m seamm_exec.timing_model fit``, the model JSON, ``predict()``,
          the report, auto-refit; the seed benchmark, declared per step
          (``TIMING_BENCHMARK``, 2026.10.7.1); first fits on the Mac and TinkerCliffs.
3 (done)  ORCA's ``estimated_seconds`` from ``predict()``; the guards against what the
          records do not cover (2026.10.7).
4 (done)  MOPAC (two regimes), Gaussian, VASP, LAMMPS, Psi4 and DFTB+ converted to the
          record, each declaring its ``TIMING_SPEC``; the old ``_timing_data`` code removed.
          Open: the hand fit in ``vasp_step.batch`` is still used for VASP estimates.
5 (next)  **Where a task runs, and where its time is predicted** (section 8): the worker
          stamps the machine class into the result and the record uses it; a prediction
          names the target machine; models travel to the installation that needs them.
6         Bundle and job walltimes from the predictions (the JobServer, with the target's
          model); the GUI estimate; VASP's estimate from the model; a bundle timeout
          under an estimate below the real time does not use up an attempt.
========= ====================================================================================

8. Where a task runs, and where its time is predicted
-----------------------------------------------------

Three things are tied today to the process that calls ``analyze_task`` -- the
evaluator -- rather than to the machine that ran the task, and to the user's
home on that machine rather than to the installation:

* **The record's machine class.** ``record_task_timing`` calls
  ``machine_class()`` in the evaluator's process. A ``TaskResult`` says
  nothing about where its bundle ran. This holds while the whole flowchart
  runs on the target (today's routing: a job submitted on ChemAI to a
  TinkerCliffs queue runs entirely on TinkerCliffs, its bundles on the same
  cluster), but already mislabels within a cluster: TinkerCliffs' ``normal_q``
  has 296 AMD and 16 Intel nodes, and a bundle on an Intel node is recorded
  with the evaluator node's CPU model, since the key is
  ``cluster:partition:cpu_model``.
* **The prediction's machine class.** ``predict()`` defaults to the caller's
  machine. An estimate made where the work will not run (the JobServer choosing
  a job's walltime for a cluster queue, the GUI before submission) is the
  caller's number with no offset and a widened spread.
* **Where the model lives.** Records and models are in ``~/.seamm.d/timing``
  of the user on each machine. TinkerCliffs' model exists only in that user's
  home there; nothing copies it, and a second user on the cluster has none
  until they seed.

The design:

1. The task worker stamps ``machine_class()`` into the finished result when it
   writes ``DONE`` (the manifest carries it; ``TaskResult`` gets ``machine``).
   ``record_task_timing`` prefers that stamp to its own environment, so a row
   says where the bundle ran, whatever ran the evaluator.
2. ``predict()`` takes the machine class of the queue the task is bound for
   (the queue section's class, learned from the first stamped result or from
   the seed run), not the caller's. A step's ``estimated_seconds`` passes it
   through from the evaluator's target.
3. Records stay where they are written (large, needed only by the fit). Models
   (kilobytes of JSON) live under the installation, keyed by machine class:
   ``<root>/timing/models/<machine>/<program>.json``, written by the fit and
   read by ``predict()`` for any class, local or not. A JobServer queue that
   reaches a cluster over ssh pulls that cluster's models into the local
   installation's directory whenever it polls, so a job submitted from ChemAI
   to TinkerCliffs is estimated with TinkerCliffs' model. The per-user
   directory remains the fallback for a machine without an installation root.
4. Seeds are then per machine class that bundles land on: each TinkerCliffs
   node type (the Intel nodes with ``--constraint``), ChemAI, the Macs; Owl and
   Falcon only when a queue targets them.

This answers open question 1 for the common case: models are per installation
and per machine class, copied between installations by the queues that connect
them; publishing them centrally stays an option for machine classes a site has
never run on.

Open questions
--------------

1. Should fitted models be published centrally (with the step, via
   ``seamm_packaging``, or Zenodo), or stay per installation? The JSON is the
   same either way.
2. The quantile for a queue walltime: 95th, or the task layer's current 3x
   rule, or the larger of the two until the fit is trusted?
3. Whether the benchmark flowchart runs at install time (minutes of compute on
   a login node are not always allowed) or only through the JobServer.
