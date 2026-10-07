Phase 8 notes
=============

2026-10-05 -- candidate list for Paul (no design yet)

Phases 0-7 of the parallel-execution campaign are released. This page gathers what
was deferred along the way. The items were scattered through the phase 6 and 7 notes,
their reviews and the design session's messages. For each one: what it is, why it was
deferred, the evidence so far, a rough effort, and a recommendation. Paul picks; a
design note follows for whatever is chosen.

Effort: *S* is under a day, including the release; *M* is a few days; *L* is a week
or more, or needs live runs on the clusters.

Summary
-------

==  ==========================================================  ========  =========
#   item                                                        effort    recommend
==  ==========================================================  ========  =========
1   Warm evaluator per bundle (parallel-loop start-up)          L         measure
                                                                          first
2   Dimer builder wall-walk as per-point TaskSets on a queue    M         defer
3   Packaging: sdists that cannot be built (8 packages)         S         decided
4   Dashboard: child iterations as datastore rows               M-L       defer
5   devops: a summary line when the docs deploy fails           S         decided
6   Run-path tests of the code steps (mopac, ORCA first)        S-M       decided
7   Legacy structure-handling wording (strain_step)             S         done
8   seamm_exec test flakiness: the timing-sensitive tests       S         done
9   PBS site: job history and ``max_resubmits``                 S         done;
                                                                          verify
10  Task view at 500 iterations                                 S         measure
11  TaskServer as the default local queue                       S         defer
                                                                          until
                                                                          soaked
12  Separate placement of a parallel loop's codes, live         M         do with
                                                                          EC pilot
13  Timing helper adoption by the plug-ins (vasp-step#18)       S each    timing
                                                                          campaign
14  molsystem#121: periodicity 0 on an empty configuration      S         done
15  orca_step: cap the cores for small molecules                S         not needed
==  ==========================================================  ========  =========

The items
---------

1. Warm evaluator per bundle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* A bundle of parallel-loop iterations would run in one long-lived evaluator,
instead of starting a fresh ``run_from_jobserver`` for each iteration.

*Why deferred.* Phases 6 and 7 aimed at correctness and placement, and the start-up
cost had not been measured where it matters.

*Evidence.* About 5 s of evaluator start-up per iteration on the Mac. In the
tiny MOPAC loop, p1 took 11 s serial against 21 s in parallel with 2 at once. On
TinkerCliffs (``p8_tc``, about 10 s of MOPAC per iteration) start-up hid any speed-up:
1:48 serial, 1:50 inline with 4 at once, and 2:27 bundled as queue jobs. Nothing has
been measured on ChemAI, nor TC's start-up on its own (imports over GPFS, the plug-in
scan).

*Effort.* L. The iteration's independence contract (its own database snapshot,
checkpoint and merge) has to hold for several iterations in one process, with no
state leaking between them. The kill and resume soaks would need repeating.

*Recommendation.* Measure first (S): time ``run_from_jobserver`` start-up alone on
ChemAI and on a TC compute node, and set it against the EC pilot's real iteration
times. Build it only if start-up is a meaningful share of a real loop.

2. Dimer builder wall-walk as per-point TaskSets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* The wall walk's points would be TaskSets on a queue, rather than run in
turn inside the step.

*Why deferred.* This is dimer-builder work, not task-layer work; triaged in phase 7 as
"with its next campaign".

*Evidence.* None new.

*Effort.* M.

*Recommendation.* Defer to the dimer builder's next campaign.

3. Packaging: sdists that cannot be built
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* A source distribution from which no wheel can be built. Measured
2026-10-05 by building every package in the workspace with ``uv build``, which builds
the wheel from the sdist as ``pip`` does: 67 of 83 build. The released packages that
fail have two causes:

- **The sdist misses files ``setup.py`` reads** (``versioneer.py``,
  ``requirements.txt``): custom_step, forcefield_step, loop_step, supercell_step,
  table_step. The fix is two ``MANIFEST.in`` lines (``include requirements*``,
  ``include versioneer.py``). seamm_jobserver had the same gap, fixed in 2026.10.5.
- **An old vendored ``versioneer.py`` calling ``configparser.SafeConfigParser``**,
  removed in Python 3.12, so nothing builds at all: strain_step (last released
  2022), crystal_builder_step (2022), set_cell_step (2021). Their next release would
  fail in CI. The fix is the current ``versioneer.py`` from the cookiecutter template.

The other failures are local prototypes with no repository (conformer_search, data_sources,
pyscf, query, trajectory_analysis) or stale side copies (``forcefield_step_experimental``,
``strain_step_sv``).

A ``pyproject.toml`` is *not* needed: the 67 that build have none. The first version
of this item said otherwise.

*Impact.* None for users today. Every release ships a wheel, which ``pip``/``uv``
install, so the sdist is only used by something building from source.

*Decision (Paul, 2026-10-05).* Fix each package as part of its next release, not in
releases of its own; strain_step together with item 7. The release skill's pre-flight
now runs ``uv build && twine check`` and says to fix a failing build, never to fall back
to ``python -m build --sdist --wheel``, which builds both from the source tree and hid
seamm_jobserver's gap.

4. Dashboard: child iterations as datastore rows
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* Each parallel-loop iteration would be a row in the jobs database
(``parent_id``), listed, filtered and linked like a job.

*Why deferred.* Phase 6 left it for "later". Phase 7 delivered the in-job task view
instead: iterations and tasks read from the job's files, with no schema change.

*Evidence.* The task view works on job 4014 (24 iterations, 25 steps with tasks).
Nobody has asked for cross-job queries over iterations.

*Effort.* M-L. It needs:

- a datastore migration;
- the JobServer and evaluator writing the rows;
- the web UI showing them;
- the rows kept consistent with resume and merge.

*Recommendation.* Defer until a concrete need appears, such as searching iterations
across jobs or showing per-iteration status for remote jobs without fetching files.

5. devops: a summary line when the docs deploy fails
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* The Release workflow's docs deploy is ``continue-on-error``. That came from
devops#3 and the same change in seamm_webui's own copy. A failure is therefore only a
red step in a green job. A ``$GITHUB_STEP_SUMMARY`` line or a warning annotation
would make it visible.

*Why deferred.* A nit from the phase 7 re-check.

*Evidence.* properties_step's Release hit the ``gh-pages`` push collision twice in
phase 6.

*Effort.* S.

*Recommendation.* Do it with the next devops change. The phase 7 triage first
proposed one concurrency group for every workflow that pushes ``gh-pages``, but the
release notes rejected it: GitHub cancels all but one *pending* job in a group, so a
Release's deploy could be dropped. A retry with a ``git pull --rebase`` of
``gh-pages`` before the push would remove the collision itself.

*Measured (2026-10-05).* The docs deploy succeeded in the latest Release run of all 15
packages released in phases 6 and 7, properties_step's included. The only failures on
record are properties_step's two in phase 6, before devops#3. A failed deploy costs
nothing visible, since the package's Docs workflow publishes the same docs when the
merge lands on main.

*Decision (Paul, 2026-10-05).* No change of its own. Fold the ~10 lines (an ``id``
on the deploy step, and a step that runs on failure, writing a ``::warning::`` and a
``$GITHUB_STEP_SUMMARY`` line) into the next devops change, and the same into
seamm_webui's own ``Release.yaml``.

6. Run-path tests of the code steps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* An end-to-end test of each code step's run path:

- writing the input;
- finding the program through ``<code>.ini``;
- running the task;
- reading and analyzing the output;
- reporting the results.

No code step had one. In phase 7, lint alone caught a leftover ``success``
variable in ``MOPAC.run()`` that would have crashed every MOPAC run.

*Decision and plan (Paul, 2026-10-05).*

- **Two tests per code.** A *fake* program replays a real run's recorded output, so
  the whole path runs in CI. The *real* program is found as SEAMM finds it
  (``$<PROGRAM>_EXE``, the PATH, the installation's ``<code>.ini``), and its test
  is skipped where the code is not installed.
- **A shared harness, ``seamm_exec.testing``.** ``run_spec`` builds a flowchart
  from a spec and runs it with ``run_flowchart``, with its own ``HOME`` and
  ``SEAMM_ROOT``, so the user's timing files and ini files are never touched, plus
  a tiny ``Water`` step. ``fake_program``, ``find_program`` and ``table_value``
  complete it. Each code's test is then about 20 lines plus its recorded output.
- **Done (2026-10-05):**

  - mopac_step: ``tests/test_run_path.py`` (fe3c248, on dev). It was checked to
    fail on the old ``success`` bug, and it moves to the harness once seamm_exec
    is released.
  - The harness, in seamm_exec 6a8914a.
  - The ORCA test (B3LYP/def2-SVP water, 1 core). It found two problems:

    - **A sub-step's tasks lost the SEAMM root** (fixed, seamm_exec afb3e6d).
      ``TaskSet`` and ``Evaluator`` read ``node.global_options``, which is empty on
      a sub-step, so ORCA's Energy, Optimization and BSSE tasks never read
      ``<root>/orca.ini`` and took ``orca`` from the PATH. ORCA must be started by
      its full path. On ChemAI and MolSSI10, ``/usr/bin/orca`` is the Debian screen
      reader, and TinkerCliffs has no ``orca`` on the PATH. No ChemAI job since the
      2026-10-04 rollout used the ORCA step, so production had not hit it yet.
      The orca.ini files: ChemAI and TinkerCliffs name the full ORCA 6.1.1 path and
      the OpenMPI library path; the Mac's is the same ORCA as its PATH; MolSSI10's
      ``[local]`` has no ``code``.
    - **ORCA 6.1.1 aborts water on 11 processes** (the grid error "the number of
      points read from the grid does not match the expectation"). orca_step gives
      even a 3-atom molecule every core it sees. A cap for small molecules is a
      separate decision.

- **Next, each when its package is next released:** Gaussian (g09 on the Mac), then
  VASP. VASP is not on the Mac; it is on ChemAI, MolSSI10 and ARC, so its real-code
  test runs there.
- **MDI codes (LAMMPS, xnn) separately.** They need a fake MDI engine, a design
  question of its own.
- **Rarely used codes** (dftbplus, packmol, torchani, fhi_aims, atomic_charges,
  psi4, xtb): only when someone works on them.
- **Release order:** seamm_exec (harness and root fix, pin ``seamm-util>=2026.9.27.1``)
  first, then orca_step and mopac_step pinned to it.

7. Legacy structure-handling wording (strain_step)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* seamm#221 (closed) translates legacy ``structure handling`` spellings such as
"be put in a new configuration" when parameters are read, so old flowcharts run. But
strain_step still *defines* its own parameter with the old wording: its default and
enumeration in ``strain_step/__init__.py``.

*Why deferred.* Outside the campaign's packages.

*Evidence.* ``strain_step/__init__.py:24``: ``"default": "be put in a new
configuration"``.

*Effort.* S: switch to seamm's standard ``structure_handling_parameters`` and the
current choices, plus a release.

*Recommendation.* Do it, and grep the other steps for local copies of the old
wording.

*Done (Paul, 2026-10-05/06).* strain_step 2026.10.5 has SEAMM's standard choices
(overwrite, new configuration -- still the default -- or new system; not discard),
names a new configuration after the strains, and fixes the cell table when
overwriting and a non-periodic system ending the flowchart. It also carries its item 3
fix (the cookiecutter's ``versioneer.py``; ``test_env.yaml`` removed). strain_step
2026.10.6 keeps the current names for a new system or configuration. The same choices
went into supercell_step 2026.10.6 (overwrite stays the default), which also fixes
supercells of bonded structures (bonds looked up by row position; each copy's bonds
mapped from the previous copy's) and its item 3 ``MANIFEST.in``. Doing so found three
bugs in molsystem's ``lower_symmetry`` (bonds by position, ``other=`` putting the
coordinates on the source, Cartesian cells), fixed in molsystem 2026.10.6, which
supercell_step requires.

8. seamm_exec test flakiness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* Timing-sensitive tests that fail on loaded CI runners.

*History.*

- ``test_tasks_run_concurrently`` asserted a total elapsed time under 2.5 s. Phase 7
  changed it to measure the overlap of the tasks' running intervals.
- ``test_bundles_through_the_taskserver`` failed on Ubuntu with Python 3.12 in the
  2026.10.5.1 PR. The backend has a bundle's results a moment before the runner marks
  the TaskServer job completed. It now polls for up to 30 s (02b59d2, test-only).

*Evidence.* Two cases in three releases, each fixed in the test, not the code.

*Effort.* S.

*Recommendation.* Do a sweep: grep the tests for ``time.sleep`` and elapsed-time
assertions, and give each one a poll with a deadline.

*Done (2026-10-06).* A sweep of seamm_exec's and seamm_scheduler's tests for elapsed-time
assertions and fixed sleeps. Most are safe: they wait for a condition with a deadline,
clear a fault after a delay without asserting on time, or bound time in the direction
a slow runner only makes safer. Two raced a slow runner and now wait for the state they
need: seamm_exec's ``test_cancel_failure_leaves_tasks_adoptable`` (slept 1 s and assumed
the task was submitted; 56b5a31) and seamm_scheduler's floor test (slept 1.5 s so that
the second job would be the newest; 1127e5c). seamm_exec's last 40 CI runs showed no
other flaky test: the outage, the PyPI cache lag and the TaskServer bundle race fixed
in 02b59d2. Both go out with each package's next release.

9. PBS site: job history and ``max_resubmits``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* MolSSI10 is the PBS test site. ``qstat -x`` must keep finished jobs long
enough for a laptop asleep over a long weekend; the phase 7 note asks for
``job_history_enable`` and at least 7 days of ``job_history_duration``. The
``max_resubmits`` guidance is the same as for SLURM.

*Status.*

- The jobserver user guide now covers ``max_resubmits`` for SLURM, PBS and the
  TaskServer, and how long PBS must keep job history (seamm_jobserver 2026.10.5).
- The phase 7 notes record ``job_history_duration`` at ``168:00:00`` on MolSSI10.
- The 22 stale staged directories were removed on 2026-10-05, and
  ``~/seamm_dev_remote_jobs`` was empty when checked again later that day.

*Effort.* None, unless the check fails.

*Recommendation.* Done. Re-verify ``qmgr -c 'p s'`` once at the next MolSSI10 visit.

10. Task view at 500 iterations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* ``/api/jobs/{id}/tasks`` walks the job's files and reads every iteration's
``job_data.json`` and manifests on each 15 s refresh.

*Why deferred.* It was only exercised at 24 iterations.

*Evidence.* No measurement at scale.

*Effort.* S to measure, with a synthetic job of 500 iteration directories in the test
fixture's layout. M if it needs caching, for example by the files' modification
times.

*Recommendation.* Measure, and act only if a refresh costs more than about 1 s.

11. TaskServer as the default local queue
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* Make ``type = queue, scheduler = seamm`` the default ``[local]`` section of a
new installation, instead of ``type = local``.

*Why deferred.* Paul's D3 made it opt-in, SEAMM_DEV first.

*Evidence.* The soak on SEAMM_DEV ran jobs 4015-4019, two jobs sharing 4 cores and
4 GB, and the kill soak. The soak setting was removed on 2026-10-05, when SEAMM_DEV
went back to ``type = local`` on the released packages.

*Effort.* S in seamm_manager's install. The real cost is the soak time.

*Recommendation.* Defer. Opt in on one everyday installation, the Mac's ``~/SEAMM`` or
paul.local, for a few weeks of real use after the 6+7 rollout, then decide.

12. Separate placement, live
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* A parallel loop with ``placement = separate tasks``, where the iterations'
codes are tasks on the target rather than run inline.

*Why deferred.* Phase 6 validated inline placement and bundled iterations live, but
not separate placement. The Loop's user guide marks it experimental.

*Evidence.* Unit tests and local runs only.

*Effort.* M, with live TC runs.

*Recommendation.* Validate with the EC pilot, which is the first real loop that needs
it, before removing the "experimental" label.

13. Timing helper adoption
~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* ``seamm_exec.timing`` (2026.10.5.1) gives a locked, rotating append for
``~/.seamm.d/timing/<program>.csv``. No plug-in uses it yet; mopac_step, lammps_step,
gaussian_step and vasp-step write their own files.

*Why deferred.* Each code adopts it in its own release (vasp-step#18, still open).

*Effort.* S per plug-in.

*Recommendation.* Do it as each code step is next released, starting with vasp-step,
whose cluster runs share a file over NFS.

*Handed over (Paul, 2026-10-06).* The timing campaign (``campaigns/2026-10-05``,
the design session) owns it: MOPAC, Gaussian and VASP move to ``record_task_timing``
in its Phase 4, LAMMPS and the rest in Phase 5. A survey for it: mopac_step and
vasp_step run through the task layer; gaussian, lammps, psi4 and dftbplus still use
``executor.run``. On Paul's Mac ``lammps.csv`` is 707 MB, and mopac's, dftbplus's and
gaussian's 47-63 MB.

14. molsystem#121
~~~~~~~~~~~~~~~~~

*What.* Setting ``periodicity = 0`` on an empty periodic configuration raises.

*Why deferred.* Filed during the phase 4/5 work; not on the campaign's path.

*Effort.* S.

*Recommendation.* Do it with the next molsystem release.

*Done (2026-10-06).* molsystem 2026.10.6.1: the cell's ``to_fractionals`` and
``to_cartesians`` convert an empty list of coordinates to an empty one, so the
periodicity setter (and any other caller) works with no atoms. read_structure_step's
2026.10.4 workaround is harmless and stays.

15. orca_step: cap the cores for small molecules
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* orca_step gives a calculation every core it sees: ``%pal nprocs`` is the
available cores, capped only by the step's or the global ``ncores``. ORCA 6.1.1 then
aborts small molecules in its start-up.

*Evidence (2026-10-05).* Water at B3LYP/def2-SVP on the Mac with 11 processes
aborted in ORCA's start-up: "the number of points read from the grid does not
match the expectation" (``orca_startup_mpi``). On 1 core it runs in 3.7 s. Found
while writing the run-path test (item 6), which runs on one core for that reason.

*Effort.* S. Cap ``n_cores`` by the size of the calculation, for example by atoms or
basis functions, using the same descriptors the timing records now hold. The cap
belongs in ``orca_base`` where ``%pal`` is set. The threshold needs a few measured
points: at what size does ORCA accept 2, 4 or 11 processes?

*Recommendation.* Do it (Paul, 2026-10-05: its own item). Measure the threshold on
the Mac first, then check it on a cluster node with more cores.

*Result (2026-10-06): not needed.* ORCA 6.1.1 ran water, methane, ethanol,
benzene, hexane and decane (3-32 atoms) on 2, 4, 6, 8 and 11 processes without a
single failure, and so did the failing run's exact input and the same flowchart
through SEAMM. The abort reproduces only with Homebrew's OpenMPI 5.0.8 first on
the PATH: ORCA 6.1.1 needs OpenMPI 4.1.x on macOS, which orca_step puts on the PATH
from ``library-path`` in ``orca.ini``. The failing run predates the sub-step root
fix (seamm-exec 2026.10.5.2), so ``orca.ini`` was not read and ORCA used Homebrew's
``mpirun``. Follow-up done (Paul, 2026-10-06): orca_step 2026.10.6 checks, before a
parallel run, the ``mpirun`` ORCA will use and stops with a clear message if it is
OpenMPI 5 or missing (skipped on one core and for ``installation = modules``).

Not phase 8, recorded here for completeness
-------------------------------------------

- **The phase 6+7 rollout:** Paul decides when, timed with the EC pilot.
  ChemAI only on an explicit ask; TinkerCliffs with an explicit package list and a
  dry run, never ``--all``.
- **Cleanup (2026-10-05), all done:**

  - The idle test venvs ``~/SEAMM_DEV/venvs/phase4-A``, ``phase4-B`` and
    ``phase7-B``, and the empty test queue ``~/SEAMM_DEV/taskserver`` with its
    test-sized ``taskserver.ini``: deleted by Paul (2026-10-05).
  - The local harness runs in ``Testing/phase4/runs`` and ``Testing/phase2`` (the
    ``Job_9000xx`` runs and logs) were deleted on Paul's word (2026-10-05); the
    harnesses and ``targets.ini`` stay.

