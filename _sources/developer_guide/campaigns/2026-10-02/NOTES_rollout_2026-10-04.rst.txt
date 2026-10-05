Rollout of 2026-10-04: ChemAI and TinkerCliffs
==============================================

Paul's explicit ask (2026-10-04): update ChemAI's and TinkerCliffs' SEAMM
installations to the current releases (phase 4, the PBS chain, seamm_exec
2026.10.4, read_structure_step 2026.10.4). Done the same day.

Versions
--------

.. list-table::
   :header-rows: 1

   * - Package
     - ChemAI before
     - ChemAI now
     - TinkerCliffs before
     - TinkerCliffs now
   * - seamm
     - 2026.10.2
     - 2026.10.3
     - 2026.10.2.2
     - 2026.10.3
   * - molsystem
     - 2026.9.25
     - 2026.10.3
     - 2026.9.25
     - 2026.10.3
   * - seamm-exec
     - 2026.9.27
     - 2026.10.4
     - 2026.10.2.1
     - 2026.10.4
   * - seamm-scheduler
     - (none)
     - 2026.10.3
     - 2026.10.2
     - 2026.10.3
   * - seamm-jobserver
     - 2026.9.27
     - 2026.10.3
     - 2026.10.2
     - 2026.10.3
   * - seamm-slurm
     - 2026.8.13
     - 2026.10.2
     - 2026.10.2
     - 2026.10.2
   * - seamm-webui (own venv)
     - 2026.10.1.1
     - 2026.10.3
     - (no web UI)
     - --
   * - table-step
     - 2026.9.30
     - 2026.10.3
     - 2026.9.30
     - 2026.10.3
   * - loop-step
     - 2026.9.18
     - 2026.10.3
     - 2026.9.18
     - 2026.10.3
   * - geometry-analysis-step
     - 2026.3.1
     - 2026.10.3.1
     - 2026.3.1
     - 2026.10.3.1
   * - orca-step
     - 2026.10.1
     - 2026.10.3.2
     - 2026.10.1
     - 2026.10.3.2
   * - mopac-step
     - 2026.10.1
     - 2026.10.3.2
     - 2026.10.1
     - 2026.10.3.2
   * - read-structure-step
     - 2026.9.18.1
     - 2026.10.4
     - 2026.9.18.1
     - 2026.10.4
   * - vasp-step
     - 2026.9.29
     - 2026.10.3.1
     - 2026.9.29
     - 2026.10.3.1
   * - lammps-step
     - 2026.10.1
     - 2026.10.3
     - --
     - --
   * - xnn-step
     - 2026.9.28
     - 2026.9.28
     - 2026.9.28
     - 2026.10.2
   * - seamm-manager (tool)
     - 2026.10.1.1
     - 2026.10.2.3
     - 2026.10.2.3
     - 2026.10.2.3

``pip check`` is clean on both. properties-step is not installed on either
(``update`` only upgrades what is installed). mbe-step 2026.10.4 and seamm-mbe
2026.10.3.1 were added to ChemAI afterwards (below); TinkerCliffs has neither.

ChemAI
------

1. No jobs: nothing in SLURM for ``seamm``, nothing submitted or running in the
   datastore (the 112 old "started" rows are stale). JobServer and web UI
   stopped.
2. seamm-manager 2026.10.2.3 (``uv tool install --force --python 3.12``).
3. ``environment migrate``: the old venv is ``venvs/2026-10-04T06-14-09`` (the
   rollback). It reported starting the JobServer; it was not running
   (seamm_manager#26).
4. **Not** ``update --all``: xnn-step's installer would have applied
   ``seamm-xnn.yml`` with ``conda env update`` to ``seamm-lammps``, which
   ``xnn.ini`` names and which has no xnn applied-marker -- the LAMMPS/MLFF
   environment the rollout must not touch. Paul chose to update everything
   except xnn-step: ``update --latest`` with every installed package but xnn-step,
   plus ``seamm-webui``. New venv ``venvs/2026-10-04T06-18-41`` (19 changes). The
   other code installers found their environment files unchanged since
   2026-09-28 (markers under 7 days old) and did nothing: every code environment's
   marker and ``conda-meta`` are unchanged; ``seamm-lammps``' history was last
   written 2026-09-19. The pyxtal-step installer failed (``import pkg_resources``),
   a problem of that plug-in, not of the update.
5. JobServer 2026.10.3 started (``services start jobserver``), web UI running
   (55155) with all 16 queues; the legacy dashboard left stopped.

TinkerCliffs
------------

``/projects/seamm/SEAMM`` was already versioned with seamm-manager 2026.10.2.3.
None of the jobs running there used it (the MBE session's bundles use its
private venv; the ``qzDimer2`` array runs ORCA directly), so
``update --latest --all`` built ``venvs/2026-10-04T06-12-15`` and switched; the
previous version ``venvs/2026-10-03T06-33-37`` is the rollback.

**Damage:** ``--all`` included xnn-step, whose installer applied
``seamm-xnn.yml`` to the env ``xnn.ini`` names, ``seamm-lammps-xnndev`` (the ARC
MLFF env). At 06:14-06:15 that upgraded openssl 3.6.4 -> 3.6.5 (conda) and
replaced xnns 0.5.0 from git ``a8958df6`` with xnns 0.6.0 from PyPI (pip,
``xnns>=0.4.0``), and wrote the ``seamm-xnn-step``/``seamm-lammps-step`` markers.
The development session had installed 0.6.0 at 05:58, but into the separate
clone ``seamm-lammps-xnn060``; its pending ``xnn060_val`` job (7849692) compares
the two envs. The same mistake was avoided on ChemAI and missed here.
Restored at Paul's word (2026-10-04, job 7849692 still pending):
``pip install --no-deps "xnns @ git+https://github.com/molssi-ai/xnn@a8958df6..."``
into ``seamm-lammps-xnndev``; its ``pip freeze`` is now identical to the
development session's ``envbuild/freeze_dev.txt`` and ``pip check`` is clean. The
openssl 3.6.5 patch and the two markers were left.

Test jobs
---------

- ChemAI job **5165**, queue ``ChemAI`` (local SLURM): the MOPAC table
  flowchart; same table as every earlier run; the table is in its ``seamm.db``.
- ChemAI job **5166**, queue ``tinkercliffs_debug`` (SLURM job 7850707 on
  TinkerCliffs, run in the new environment): same table.

Left for Paul: xnn-step on ChemAI (2026.9.28), to be updated deliberately
without applying ``seamm-xnn.yml`` to ``seamm-lammps``; the pyxtal-step
installer's ``pkg_resources`` import.

MBE on ChemAI
-------------

Paul's ask, same day: install mbe-step and seamm-mbe on ChemAI.
``seamm-manager install`` refused both ("not a SEAMM package"): they are not yet
in the nightly package list. They were added to the current venv
(``venvs/2026-10-04T06-18-41``) in place with
``uv pip install --python ~/SEAMM/venv/bin/python mbe-step==2026.10.4
seamm-mbe==2026.10.3.1``, with nothing queued that used the venv. Their
dependencies were already there, so nothing else changed; ``uv pip check`` is
clean and ``MBE`` is registered under ``org.molssi.seamm``. mbe-step has no
installer (no code environment). No service restart: each job imports the
plug-ins afresh. Until the two are in the package list, a later
``update --all`` keeps them but ``environment recreate`` would drop them.

Phase 5 rollout (2026-10-04, afternoon)
=======================================

Paul's explicit ask (relayed by the design session, confirmed by Paul): ChemAI and
TinkerCliffs together, by explicit package list only -- never ``--all``, no
xnn-step/lammps-step, no code installers (none of the eight packages has one).

============================ ============== ============
Package                      Before         After
============================ ============== ============
molsystem                    2026.10.3      2026.10.4
seamm                        2026.10.3      2026.10.4
seamm-scheduler              2026.10.3      2026.10.4
seamm-exec                   2026.10.4      2026.10.4.1
loop-step                    2026.10.3      2026.10.4
read-structure-step          2026.10.4      2026.10.4.1
forcefield-step              2026.9.27      2026.10.4
seamm-jobserver              2026.10.3      2026.10.4
============================ ============== ============

Same on both sites. ``seamm-manager update --latest molsystem seamm seamm-exec
loop-step read-structure-step forcefield-step seamm-jobserver``; seamm-scheduler is
not in the package list ("not a SEAMM package") and came in through seamm-exec's
requirement. ``uv pip check`` clean on both.

- **TinkerCliffs** (``/projects/seamm/SEAMM``, no services): none of the 50-odd
  running jobs used the venv (ORCA/VASP farm scripts under ``/projects/seamm/psaxe``,
  checked by each job's command). New ``venvs/2026-10-04T15-23-39``; rollback
  ``venvs/2026-10-04T06-12-15``.
- **ChemAI**: nothing in SLURM for ``seamm``, nothing running or submitted in the
  datastore (2482 jobs; the 112 stale "started" rows as before). JobServer and web
  UI stopped, updated: new ``venvs/2026-10-04T15-37-21``; rollback
  ``venvs/2026-10-04T06-18-41``. mbe-step 2026.10.4, seamm-mbe 2026.10.3.1 and the
  held xnn-step 2026.9.28 carried over unchanged. The update reported starting the
  JobServer but it was not running (seamm_manager#26 again); started it, then the
  web UI; all 16 queues listed.

Test jobs (job 5165's flowchart, three molecules from SMILES, MOPAC):

- ChemAI job **5167**, queue ``ChemAI``: finished.
- ChemAI job **5168**, queue ``tinkercliffs_debug`` (run in TinkerCliffs' new venv):
  finished, identical to 5167.

Both differ from this morning's 5165/5166 (e.g. -11.3502818 vs -11.3070296
kcal/mol) because of molsystem 2026.10.4's fixed RDKit seed: the SMILES give other
starting conformers. Shown, not assumed: the same flowchart run locally with
molsystem 2026.10.3 reproduces 5165/5166 and with 2026.10.4 reproduces 5167/5168,
to every printed digit.

Since both sites moved together, no ``max_resubmits`` change was needed on ChemAI's
ARC queues.

MBE on TinkerCliffs (2026-10-04, Paul's ask)
--------------------------------------------

So that MBE runs use the main install instead of the MBE session's private venv:
``uv pip install mbe-step==2026.10.4 seamm-mbe==2026.10.3.1`` into
``/projects/seamm/SEAMM/venv`` (``venvs/2026-10-04T15-23-39``), in place as on ChemAI
this morning (``seamm-manager install`` refuses packages not in the package list).
A dry run first showed only these two; nothing used the venv. ``uv pip check``
clean; ``MBE`` registered under ``org.molssi.seamm`` and listed by
``seamm-flowchart steps``. ``vasp.ini`` there (changed by the MBE session with
Paul's OK; backup ``vasp.ini.bak-2026-10-04``) names ``dftd4 =
/projects/seamm/conda-envs/dftd4/bin/dftd4``; neither package has an installer, so
nothing rewrote it. Until mbe-step and seamm-mbe are in the package list
(seamm_packaging PR #3), ``environment recreate`` would drop them.
