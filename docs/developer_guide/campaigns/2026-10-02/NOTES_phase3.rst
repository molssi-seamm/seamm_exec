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
