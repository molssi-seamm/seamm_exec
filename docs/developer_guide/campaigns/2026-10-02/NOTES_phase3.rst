Phase 3 notes
=============

2026-10-03 -- plan and decisions
--------------------------------

Phase 3 is the Model Chemistry batch contract (``get_task``/``analyze_task``) for
ORCA and MOPAC, the facade that chooses MDI or batch, and the consumers on it.

Paul's decisions:

- Fix the MDI basis bug first, as separate bugfix releases (below).
- MOPAC's batch "energy" is the heat of formation, as its MDI engine reports
  it, so the facade's choice of path never changes results.
- Convert four consumers: the Energy step, ORCA's counterpoise (BSSE, as a
  fan-out with ghost atoms), the Dimer Builder, and the finite-difference
  Hessian of Normal Mode Sampling.

The design session's decisions:

- The facade lives in ``seamm_exec``, not ``seamm``. ``seamm_exec`` already
  depends on ``seamm``, and how things run is its job. ``seamm_mdi`` is
  imported only on the MDI path.
- A provider may set ``prefers_batch`` in its options. ORCA sets it: its MDI
  engine runs a subprocess per evaluation, so the pool's concurrency beats a
  sequential warm engine. MLFFs, MOPAC and xTB stay on MDI locally.
- ``analyze_task`` raises on a missing property; it never returns partial
  numbers.
- A test checks that the batch path equals the MDI path for ORCA and MOPAC
  water; it also protects the heat-of-formation decision.
- The local pool applies resolvers only to tasks without ``config``. The
  command must be exactly today's; the SEAMM_DEV A/B on ``test.flow`` and
  ``bsse.flow`` is the proof, before ``orca_step`` is released.
- ``options.ghost_atoms`` must be enough for ``seamm_bsse``'s N-fragment SSFC
  inputs (ghosts plus per-fragment charge and multiplicity), which is also the
  MBE step's counterpoise path.
- Release order: ``seamm_exec``, then ``orca_step`` and ``mopac_step`` with
  pins, then the consumers.

2026-10-03 -- the MDI basis bug (bugfix releases)
-------------------------------------------------

Found by the phase 3 survey and reproduced. ORCA, driven as an MDI engine by
the Energy step, ran def2-SVP whatever basis was chosen.

- Cause: ``match_model_chemistry`` built the published ``_model_chemistry``
  from the first matching offering, user's basis substituted. But it kept the
  offering's ``options`` -- and ``options["mdi_basis_arg"]`` was the advertised
  example basis, which the Energy step and the Dimer Builder pass to the
  engine ahead of ``mc["basis"]``.
- Normal Mode Sampling and LAMMPS passed ``method=mc["method"]`` alone: no
  basis (so ORCA's default def2-SVP), and the model-chemistry spelling rather
  than ORCA's keyword.
- The ORCA MDI engine could not take a ``bse:NAME`` basis at all.

Fixes, each with a test:

- ``model_chemistry_step``: the match copies the options and puts the user's
  basis in ``mdi_basis_arg``.
- ``orca_step``: the MDI engine fetches a ``bse:`` basis from the Basis Set
  Exchange into ``basis.bas`` (``%basis GTOName``), as the ORCA step does. With
  the real binary, the energy matches ORCA's own def2-SVP to 1e-6.
- ``normal_mode_sampling_step`` and ``lammps_step``: ``mdi_method_arg`` and
  ``mdi_basis_arg``, like the Energy step.
- ``energy_step`` (test only): a regression test through the real chain
  (match, method/basis rule, engine command, MDI engine, ORCA). It asserts the
  generated input's basis, for ``def2-TZVP`` and ``bse:def2-TZVP``, and that
  the energy equals ORCA run directly (-76.4258395903 Eh, to 1e-8).
- ``dimer_builder_step`` (test only): its method/basis rule takes the user's
  basis. Its code was already right once the match was fixed.

PRs: model_chemistry_step #7, orca_step #37, normal_mode_sampling_step #1
(CI moved to uv) and lammps_step #114, all 2026.10.3, independent of each
other.

Lesson
~~~~~~

**An advertised offering's options must never override a user's explicit
choice.** A program advertises a few example bases so the list stays short.
Anything carried along with an example must be re-derived from the user's
selection when it is matched, not copied. The bug was silent: no error, just
the wrong basis, found only by reading the code.

2026-10-03 -- the MOPAC MDI engine's forces (bugfix release)
------------------------------------------------------------

Found by the first run of the MOPAC batch == MDI test. The heats of formation
agreed to 2e-7 kJ/mol, but the MDI gradients were 3.57 times the batch ones.

- A central difference of the MOPAC binary's heat of formation (O z of water:
  6.951 kcal/mol/Å) matched the batch path (6.959).
- The factor is (bohr/Å)². ``mopac_mdi.py`` converted kcal/mol/Å to
  hartree/bohr with ``* BOHR_PER_ANG`` instead of ``* ANG_PER_BOHR``, so every
  MOPAC force over MDI has been 3.57 times too large since 2026-06-23
  (992909f). Energies were right.
- Affected: the Energy step's MOPAC gradients, LAMMPS QM/MD with a MOPAC model
  chemistry, and Normal Mode Sampling's finite-difference Hessian with MOPAC
  (frequencies about 1.89 times too high). xTB's engine works in atomic units
  throughout and is unaffected; ORCA's agrees with the batch path.
- Fixed in ``mopac_step`` 2026.10.3 (PR #158). A regression test compares the
  engine's forces with a finite difference of its own energies.

After the fix, MOPAC's batch and MDI gradients differ by about 0.1%: the
single-SCF gradient depends on where the SCF stops. On water's O z it is 29.116
(binary default), 29.082 (``SCFCRT=1e-12``), 29.072 (mopactools) and 29.084
kJ/mol/Å (a finite difference). The invariant test states 0.3% + 0.02 kJ/mol/Å
for MOPAC gradients and 1e-4 kJ/mol for heats.

Also found: a ``conda run -n <env> python`` engine command runs whichever
``python`` is first on the PATH. With a SEAMM venv's ``bin`` ahead of conda's,
the MOPAC engine started the wrong Python and could not import mopactools.
This is a pre-existing fragility of the MOPAC (and other ``conda run``) engine
launchers. It showed up here only because of how the tests were started.
Recorded, not fixed.

Lesson
~~~~~~

**Two independent paths to the same number are a test.** The batch == MDI
invariant was written to protect a design decision. Its first run found a units
bug that had been in production for three months, because nothing had compared
the MOPAC engine's forces with anything else.

2026-10-03 -- what was built
----------------------------

Committed locally on ``dev`` in each package; nothing pushed except the
bugfix releases above.

``seamm_exec``
    ``evaluator.py`` holds :class:`Evaluator`, :func:`choose_path`,
    :class:`Geometry`, :func:`structure_data`, :func:`check_properties` and
    :func:`mdi_method_and_basis`.

    - ``submit(configuration, key=None, options=None)`` and ``results()``
      yield ``EvaluatorResult`` objects: ``ok``, ``energy`` (kJ/mol),
      ``gradients`` ((n, 3) kJ/mol/Å), ``stress``, ``reason``, ``restored``,
      ``path`` and ``elapsed``.
    - The MDI path is the Energy step's old loop, moved: one engine per
      (elements, charge, multiplicity, periodicity), stress only where the
      engine supports it. ``seamm_mdi`` is imported only there.
    - The batch path is a ``TaskSet`` of the provider's tasks, read back by its
      ``analyze_task``; a failed or partial task gives ``ok=False`` with the
      reason.
    - The local pool now configures every task without ``config`` from that
      machine's ini file and the program's resolver (``_configure``). Tasks
      with ``config`` are untouched; ``resolve_programs`` is kept but no
      longer changes anything.

``orca_step``
    - ``batch.py``: ``get_task`` writes the MDI engine's own input: the
      helpers are loaded from ``data/orca_mdi.py``. That covers the keyword
      plus AutoAux, the ``bse:`` basis file, the DLPNO block and ``EnGrad``.
      ``options`` gives ``atom_indices``, ``ghost_atoms`` (written ``O:``),
      ``charge`` and ``multiplicity``. Defaults: 1 rank, ``%maxcore 2000``;
      ``resources`` overrides them.
    - ``analyze_task`` uses the engine's parsers, with the same pint
      conversions as ``seamm_mdi``. Methods without an MDI engine (CCSD(T)
      and the like) get their real keyword and can run energy-only as tasks.
    - ``resolver.py``: the ``orca`` resolver (full path, ``mpi_env``,
      ``{orca_2aim}``, PATH fallback), registered in
      ``org.molssi.seamm.exec.resolvers``. ``_mpi_env`` became the module
      function ``mpi_env``.
    - The ORCA step's own tasks no longer carry ``config``, so ORCA steps can
      now go to remote targets.
    - ``run_orca_job`` is split: ``orca_job_task`` builds the task.
    - BSSE builds all its sub-jobs and runs them as one ``TaskSet``.
    - Every ORCA offering declares ``prefers_batch``.

``mopac_step``
    - ``batch.py``: a single SCF with gradients and ``AUX(PRECISION=9)``. The
      energy is the heat of formation (Paul's decision). Only the lowest spin
      state, and molecules only; open-shell and periodic MOPAC stay on MDI,
      where equality has not been shown.
    - ``resolver.py``: the ``mopac`` resolver (ini, else ``which mopac``; it
      never writes files, unlike the MOPAC step).
    - The MOPAC step's own runs keep their ``config`` this phase (its
      configuration code writes default ini files). Consider each code
      separately.

``energy_step``
    ``run()`` is on the Evaluator, with keys ``c<configuration id>``.
    Properties, ``store_results`` and ``energies.csv`` are unchanged. The
    MDI-capable requirement is dropped. Failures are listed and raised after
    the successful structures are stored. The report says "N engine sessions"
    (MDI) or "N separate calculations, M finished in an earlier run" (tasks).

``dimer_builder_step``
    ``_open_energy_engine`` returns ``_MDIEnergies`` (the engine plus
    ``energies``) or ``_TaskEnergies`` (the Evaluator). Grids -- the 11-point
    minimum search, the outward profile, the interpolation points -- are
    evaluated together. The inward wall walk stays one point at a time, since
    it is sequential by nature. The van der Waals radii are now cached, and
    mendeleev's SQLAlchemy sessions are collected on the main thread. Left
    to the garbage collector inside a pool worker thread, SQLite refused to
    close them and logged frightening, harmless errors.

``normal_mode_sampling_step``
    The Hessian is analytic over MDI when the engine offers ``<HESSIAN``.
    Otherwise it is the finite difference: as tasks
    (``_fd_hessian_tasks``, 6N displaced structures, keys
    ``c<id>-fd<j><p|m>``) when the Evaluator chooses the batch path, else
    over the warm engine as before. A model chemistry without an MDI engine
    now works through the tasks.

Validation
~~~~~~~~~~

- Unit and integration tests:

  ======================== ===== ===================================================
  Package                  Tests New
  ======================== ===== ===================================================
  ``seamm_exec``           98    the Evaluator: path rule, batch path, failures,
                                 restart, helpers
  ``orca_step``            178   resolver equivalence (4 cases), batch == MDI, ghost
                                 atoms, partial results, BSSE on a TaskSet
  ``mopac_step``           39    batch == MDI (heat of formation), refusals,
                                 ``.aux`` parsing, forces vs finite difference
  ``energy_step``          21
  ``dimer_builder_step``   59
  ``normal_mode_sampling`` 42    FD-by-tasks assembly on a quadratic potential
  ======================== ===== ===================================================
- **The resolver** reproduces the old command, environment and configuration:
  serial and parallel, with and without ``orca_2aim``.
- **ORCA batch == MDI** for water at def2-SVP and ``bse:def2-SVP``: exact (1e-6
  kJ/mol) on the first geometry; within the SCF convergence on later ones,
  where the engine reuses the previous orbitals (3e-3 kJ/mol, 1e-2
  kJ/mol/Å; observed 2.5e-4).
- **MOPAC batch == MDI**: heats to 1e-4 kJ/mol (observed 2e-7), gradients to
  0.3% (see the force bug above).
- **Live flowcharts** on this Mac (``Testing/phase3_*.flow``):

  - the Energy step over six molecules with ORCA (tasks, 5.9 s; a rerun
    restored them all) and MOPAC (MDI);
  - ``bsse.flow`` with its sub-jobs on one TaskSet;
  - the Dimer Builder with energy contacts: ORCA as tasks, 69 evaluations in
    76 s, and MOPAC over MDI.
- **The NMS Hessian** of HF/def2-SVP water by finite difference as tasks
  against ORCA's analytic one: frequencies 1790.2 / 3972.5 / 4062.3 against
  1790.9 / 3972.9 / 4062.4 cm⁻¹; the largest Hessian element difference is
  1.1e-4 Eh/bohr².
- **Remote ORCA through the resolver, Mac -> TinkerCliffs.**

  - The Energy step over six molecules with ``SEAMM_TARGET=tc`` and
    ``inline_below = 0`` sent config-less ORCA tasks in two bundles. Without
    that setting, the inline rule rightly kept water's 1 s estimates local.
  - TinkerCliffs' ``orca.ini`` (``installation = modules``) resolved them
    through the ``orca`` resolver, in a development venv in
    ``/projects/seamm/psaxe/phase3``.
  - Energies are identical to the local run to all printed digits; forces
    agree to 1e-4 kJ/mol/Å.
- **The SEAMM_DEV A/B comparison** (``venvs/phase3-A``: today's releases;
  ``phase3-B``: plus the six checkouts) on ``test``, ``bsse``,
  ``harness_water``, ``builder_loop``, ``phase3_energy_mopac`` and
  ``phase3_energy_orca``:

  - ORCA's final energies, the BSSE energies and the MOPAC results are
    identical.
  - The ORCA Energy step over MDI (A) and as tasks (B) agrees to every
    printed digit.
  - The only differences are versions, pids and timings, plus the new
    ``tasks/`` layouts (BSSE's sub-jobs share one manifest in the step
    directory; the ORCA Energy step runs as tasks).

Review (2026-10-03)
~~~~~~~~~~~~~~~~~~~

An independent review of the six diffs. Fixed, each with a test unless noted:

1. **ORCA's batch path accepted periodic structures.** With ``prefers_batch`` the
   Energy step would have stored a gas-phase cluster energy for a periodic
   configuration; the MDI path had refused it.

   - Programs now offer ``can_run_task(configuration, model_chemistry,
     options)``. A structure for which it is False goes to the program's MDI
     engine, if it has one, even when the rest run as tasks; otherwise it gets a
     failed result.
   - ORCA's ``get_task`` refuses periodic structures and initial guesses (not
     supported yet), and ``can_run_task`` refuses open-shell DLPNO.
2. **A queue target forced MOPAC's periodic and open-shell structures onto tasks
   it cannot run.** Same mechanism: MOPAC's ``can_run_task`` keeps them on MDI.
   A ``get_task`` that raises now fails that structure alone, instead of
   stopping the whole ``results()``.
3. **Pins.**

   - ``orca_step`` and ``mopac_step`` require ``seamm-exec>=2026.10.3``. The
     released 2026.10.2.1 never resolves config-less tasks in the evaluator's
     pool, so a new ``orca_step`` on it would lose ORCA's full path, the OpenMPI
     paths and ``{orca_2aim}``.
   - The three consumers now list ``seamm-exec>=2026.10.3``.
   - The pool's error names the missing resolver when a plug-in is not
     installed where a task runs (no test).
4. **Lone atoms.** ``get_task`` lacked the ORCA step's one-center ``NoCOSX``
   guard, so ``seamm_bsse``/MBE jobs for a bare Li⁺ or Na⁺ would get the ~5
   kJ/mol RIJCOSX artifact. It is added to both ``get_task`` and the MDI engine
   (``single_center_method``), so the two paths still agree. Real ORCA, Na⁺:
   batch == MDI exactly.

   This is a behaviour change of the ORCA MDI engine for single atoms (a fix),
   and goes in orca_step's HISTORY.
5. **NMS imported ``seamm_mdi`` unconditionally**, and failed when no engine
   could start, e.g. on a queue target with ORCA installed only on the cluster.
   It now imports lazily and falls back to the finite difference as tasks.
6. **The Dimer Builder's single points are slow on the task path. Deferred.**

   - Each point of the inward wall walk is its own one-task ``TaskSet`` (the
     manifest is re-read and re-written per point). On a queue target, a point
     above ``inline_below`` would be its own batch job.
   - Small ORCA dimers stay local by the inline rule. The rest needs
     speculative blocks of inward points, or one long-lived ``TaskSet`` per
     ``_TaskEnergies``.
7. **MOPAC's ``.aux`` gradients** stop at the next ``KEY=`` line and must be
   exactly 3n values (``AnalysisError`` otherwise).
8. **BSSE sub-jobs were treated as "concurrent"**, so the pool set
   ``OMP_NUM_THREADS=1`` and binding ``none`` on them, under SLURM too. They
   run one at a time anyway, each taking every core. A task that takes the
   whole pool is no longer "concurrent". Old per-sub-job restart records are
   not reused: one recompute after upgrading.
9. Minor:

   - a configuration selected twice is evaluated once by the Energy step;
   - the ``mopac`` resolver keeps a conda or modules installation whose
     ``code`` is empty;
   - DLPNO open-shell refused energy-only too, matching MDI (unchanged).

Design session's review (2026-10-03)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A second independent read, at the heads with the fixes above. Fixed:

1. **A refused structure on a queue target, with the code only on the cluster,**
   went to a local MDI engine that could not start, and the error stopped every
   other structure. Such a structure now gets a failed result, "cannot run
   here: ...", and the rest finish (``_mdi_results(fallback=True)``).
2. **NMS fell back to the finite difference on any engine failure,** so a
   transient failure (a port, the environment) silently turned ORCA's analytic
   Hessian into 6N tasks. It now falls back only when the code is not installed
   here, says so through the step's printer, and closes the engine.
3. **Tests.** A task with ``config`` never meets a registered resolver, in the
   pool and in ``bundle_runner``; a config-less one does. The Energy step's
   ``c<id>`` keys are stable on rerun, a duplicate selection is skipped,
   failures are stored then raised, and the report says how the work ran. The
   Dimer Builder maps out-of-order results back by key.
4. **The stress is required for a periodic structure** (``check_properties(...,
   periodic=True)``), so a future periodic batch program cannot return none.
5. **This note and both design copies** now carry the contract as coded:
   classmethods ``(configuration, model_chemistry, *, ...)``, no citations, the
   stress as the program gives it, and ``can_run_task``.
6. **One ``mdi_method_and_basis``:** ORCA's and MOPAC's ``get_task`` call
   ``seamm_exec``'s, so the basis-wins rule lives in one place.
7. **The resolver registry** is built under a lock; pool threads asked for it
   at the same time and a second build dropped a ``register()``.
8. **Wording:** MOPAC's refusal says the structure "runs on MOPAC's MDI engine
   where one is available"; "Where ORCA runs" says ``code`` must be ORCA's full
   path on module sites.

Addendum, also fixed:

a. **NMS rule: on a queue target, the target wins.** The finite difference runs
   as tasks on the cluster, and no local engine is started, even to ask for
   ``<HESSIAN``. Otherwise the analytic Hessian goes over MDI when the program
   has one: ORCA now says so in its model-chemistry options
   (``analytic_hessian``, from ``method_has_analytic_hessian``), so no live
   probe is needed; other programs are still asked. Failing that, the finite
   difference runs as tasks if the evaluator chose them, or over the warm
   engine.
b. NMS closes the engine on the fallback, catches only the "no code here" case,
   and prints the reason.
c. ``_can_run_task`` logs a provider's exception before treating it as False,
   so a buggy hook does not reroute silently.
d. orca_step's HISTORY records the MDI engine's behaviour change: a single
   centre now runs without COSX, which moves lone-ion energies by about 5
   kJ/mol from before.
e. The Energy step's report says how a mixed run went: "X over MDI in N engine
   session(s) and Y as separate calculations".
f. The MOPAC batch == MDI test has a doublet, OH. Heats agree to 1e-3 kJ/mol;
   the gradients to 0.06 kJ/mol/Å, because the MDI path (mopactools) gives
   spurious components of about 0.03 kJ/mol/Å perpendicular to the bond.
