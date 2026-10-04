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
     - --
     - --
   * - seamm-manager (tool)
     - 2026.10.1.1
     - 2026.10.2.3
     - 2026.10.2.3
     - 2026.10.2.3

``pip check`` is clean on both. properties-step, mbe-step and seamm-mbe are not
installed on either (``update`` only upgrades what is installed).

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

Test jobs
---------

- ChemAI job **5165**, queue ``ChemAI`` (local SLURM): the MOPAC table
  flowchart; same table as every earlier run; the table is in its ``seamm.db``.
- ChemAI job **5166**, queue ``tinkercliffs_debug`` (SLURM job 7850707 on
  TinkerCliffs, run in the new environment): same table.

Left for Paul: xnn-step on ChemAI (2026.9.28), to be updated deliberately
without applying ``seamm-xnn.yml`` to ``seamm-lammps``; the pyxtal-step
installer's ``pkg_resources`` import; mbe-step/seamm-mbe if ChemAI should run
the MBE step.
