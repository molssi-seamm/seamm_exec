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
