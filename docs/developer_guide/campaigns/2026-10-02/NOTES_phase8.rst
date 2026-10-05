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

==  ==========================================================  ======  =========
#   item                                                        effort  recommend
==  ==========================================================  ======  =========
1   Warm evaluator per bundle (parallel-loop start-up)          L       measure
                                                                        first
2   Dimer builder wall-walk as per-point TaskSets on a queue    M       defer
3   Packaging: MANIFEST.in gaps and no ``pyproject.toml``        S       do
4   Dashboard: child iterations as datastore rows               M-L     defer
5   devops: a summary line when the docs deploy fails           S       do
6   mopac_step: a smoke test of the run path                    S       do
7   Legacy structure-handling wording (strain_step)             S       do
8   seamm_exec test flakiness: the timing-sensitive tests       S       do
9   PBS site: job history and ``max_resubmits``                 S       done;
                                                                        verify
10  Task view at 500 iterations                                 S       measure
11  TaskServer as the default local queue                       S       defer
                                                                        until
                                                                        soaked
12  Separate placement of a parallel loop's codes, live         M       do with
                                                                        EC pilot
13  Timing helper adoption by the plug-ins (vasp-step#18)       S each  do, per
                                                                        code
14  molsystem#121: periodicity 0 on an empty configuration      S       do
==  ==========================================================  ======  =========

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

3. Packaging: MANIFEST.in gaps and no ``pyproject.toml``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* The problems:

- loop_step's and forcefield_step's ``setup.py`` read ``requirements.txt`` and import
  ``versioneer``, but their ``MANIFEST.in`` ships neither, so a wheel cannot be built
  from their sdist.
- None of the three has a ``pyproject.toml`` declaring ``versioneer`` as a build
  dependency, so an isolated ``uv build`` fails (``No module named 'versioneer'``).

seamm_jobserver's ``MANIFEST.in`` was fixed in 2026.10.5, but it still has no
``pyproject.toml``.

*Why deferred.* Found during the phase 7 release; the Release workflow builds from the
source tree, which works, so it was not blocking.

*Evidence.* seamm_jobserver: ``python -m build`` failed building the wheel from the
sdist until the two files were added to ``MANIFEST.in``. ``uv build`` fails for all
three.

*Effort.* S per package: two ``MANIFEST.in`` lines and a minimal
``[build-system]``. Ride along with each package's next release.

*Recommendation.* Do it. It's also worth a ``grep`` across the workspace for other
``setup.py``-only packages with the same gaps.

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

6. mopac_step: a smoke test of the run path
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

*What.* A test that drives ``MOPAC.run`` end to end, with a stub or a tiny real MOPAC.

*Why deferred.* Found in the phase 7 review fixes. Lint caught a leftover ``success``
variable in ``mopac.py`` that would have crashed every MOPAC run, and no unit test
exercised that path. It was checked with a real flowchart at the time.

*Evidence.* mopac_step has 46 unit tests, none of them through ``run()``.

*Effort.* S: a fake ``mopac`` executable writing a minimal ``.out``/``.aux``, or
``pytest.importorskip`` on a real MOPAC.

*Recommendation.* Do it. The same gap probably exists in other code steps; check
orca_step and lammps_step when they are next touched.

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

14. molsystem#121
~~~~~~~~~~~~~~~~~

*What.* Setting ``periodicity = 0`` on an empty periodic configuration raises.

*Why deferred.* Filed during the phase 4/5 work; not on the campaign's path.

*Effort.* S.

*Recommendation.* Do it with the next molsystem release.

Not phase 8, recorded here for completeness
-------------------------------------------

- **The phase 6+7 rollout:** Paul decides when, timed with the EC pilot.
  ChemAI only on an explicit ask; TinkerCliffs with an explicit package list and a
  dry run, never ``--all``.
- **Cleanup still to do (2026-10-05):**

  - The idle test venvs ``~/SEAMM_DEV/venvs/phase4-A``, ``phase4-B`` and
    ``phase7-B``, and the empty test queue ``~/SEAMM_DEV/taskserver`` with its
    test-sized ``taskserver.ini``. Their deletion was blocked by a permission
    check, so Paul is deleting them by hand.
  - The local harness runs in ``Testing/phase4/runs`` (11 MB) and
    ``Testing/phase2`` (the ``Job_9000xx`` runs and logs, 2.3 MB), on Paul's word.

