Rollout plan: phases 6 and 7, the orca.ini fix and the timing records
=====================================================================

Status: PLAN, written 2026-10-05 at Paul's ask ("fully update all installations
here, and on ChemAI, MolSSI10, ARC and the Mac mini"). To be executed once
seamm-exec and orca-step 2026.10.5.2 and seamm-manager 2026.10.5.1 are on PyPI
(all three released 2026-10-05: seamm_exec PR #46, orca_step PR #46,
seamm_manager PR #32; xnn-step 2026.10.5 follows seamm-manager onto PyPI). The record of what was actually done goes below the plan, as
``NOTES_rollout_2026-10-04.rst`` did for the last one.

What this rollout carries
-------------------------

1. **Phase 6** (bundling of a step's tasks and parallel loops; opt-in) and **phase
   7** (the TaskServer queue; opt-in): the 2026.10.5 releases. No configuration
   change is needed, since both are off by default.
2. **The orca.ini fix** (seamm-exec and orca-step 2026.10.5.2): a sub-step's
   ``run_task`` took the root from the sub-step's empty ``global_options``, so
   ``<root>/orca.ini`` was never read and ORCA came from the PATH. On ChemAI and
   MolSSI10 ``/usr/bin/orca`` is the Debian screen reader; TinkerCliffs has no
   ORCA on the PATH. Every ORCA job run through a sub-step on a cluster is broken
   until this is deployed. No ChemAI job since the 2026-10-04 rollout used the
   ORCA step, so production has not hit it yet.
3. **The timing records** (same releases, campaign ``2026-10-05``): every ORCA
   run appends a row to ``~/.seamm.d/timing/orca.csv`` with the machine class.
   The clusters are where the useful rows come from.
4. **seamm-manager 2026.10.5.1** (and 2026.10.5): a plug-in installer leaves
   alone a conda environment it did not make, now read from conda's own history
   rather than SEAMM's marker records alone (#28, #31); a plug-in whose code
   needs PyTorch installs the build for the machine's NVIDIA driver and checks
   it (#31); ``update`` runs no installer for a refused package (#29); a service
   stopped on purpose stays stopped (#26).
5. **xnn-step 2026.10.5**, once released: its installer uses that torch path,
   and ``torch`` is out of ``seamm-xnn.yml``. This is what lets the xnn-step hold
   on the clusters be lifted.

Versions
--------

Latest on PyPI, 2026-10-05, and what each site has (the Macs read today; the
clusters from the 2026-10-04 and phase 5 rollout records -- re-read them at
execution and fill in the table).

.. list-table::
   :header-rows: 1

   * - Package
     - Target
     - ~/SEAMM (Mac)
     - ~/SEAMM_DEV
     - Mac mini
     - ChemAI
     - TinkerCliffs
   * - seamm-manager (tool)
     - 2026.10.5.1
     - 2026.10.2.3
     - 2026.10.5
     - 2026.10.2.3
     - 2026.10.2.3
     - 2026.10.2.3
   * - seamm
     - 2026.10.5
     - 2026.10.2.2
     - 2026.10.5
     - 2026.10.2.2
     - 2026.10.4
     - 2026.10.4
   * - molsystem
     - 2026.10.5
     - 2026.9.25
     - 2026.10.5
     - 2026.9.25
     - 2026.10.4
     - 2026.10.4
   * - seamm-exec
     - **2026.10.5.2**
     - 2026.10.2.1
     - 2026.10.5.1
     - 2026.10.2.1
     - 2026.10.4.1
     - 2026.10.4.1
   * - seamm-scheduler
     - 2026.10.5
     - 2026.10.2
     - 2026.10.5
     - 2026.10.2
     - 2026.10.4
     - 2026.10.4
   * - seamm-jobserver
     - 2026.10.5
     - 2026.10.2
     - 2026.10.5
     - 2026.10.2
     - 2026.10.4
     - 2026.10.4
   * - seamm-webui (own venv)
     - 2026.10.5
     - --
     - (phase7-webui)
     - --
     - 2026.10.3
     - (none)
   * - orca-step
     - **2026.10.5.2**
     - 2026.10.1
     - 2026.10.5.1
     - 2026.9.29
     - 2026.10.3.2
     - 2026.10.3.2
   * - mopac-step
     - 2026.10.5
     - 2026.10.1
     - 2026.10.5
     - 2026.7.27
     - 2026.10.3.2
     - 2026.10.3.2
   * - loop-step
     - 2026.10.5
     - 2026.9.18
     - 2026.10.5
     - 2026.9.18
     - 2026.10.4
     - 2026.10.4
   * - read-structure-step
     - 2026.10.5
     - 2026.9.18.1
     - 2026.10.5
     - 2026.9.18.1
     - 2026.10.4.1
     - 2026.10.4.1
   * - table-step
     - 2026.10.5
     - 2026.9.30
     - 2026.10.5
     - 2025.6.1
     - 2026.10.3
     - 2026.10.3
   * - geometry-analysis-step
     - 2026.10.5
     - 2026.3.1
     - 2026.10.5
     - 2026.3.1
     - 2026.10.3.1
     - 2026.10.3.1
   * - forcefield-step
     - 2026.10.4
     - 2026.9.27
     - 2026.10.4
     - 2026.9.27
     - 2026.10.4
     - 2026.10.4
   * - properties-step
     - 2026.10.5
     - (not installed)
     - ?
     - (not installed)
     - (not installed)
     - (not installed)
   * - lammps-step
     - 2026.10.5
     - 2026.10.1
     - 2026.10.5
     - 2026.9.25
     - 2026.10.3 (HOLD, below)
     - (not installed)
   * - xnn-step
     - 2026.10.5 (when released)
     - 2026.9.28
     - (not installed)
     - 2026.9.28
     - 2026.9.28 (HOLD, below)
     - 2026.10.2
   * - mbe-step / seamm-mbe
     - 2026.10.5 / 2026.10.5.1
     - --
     - ?
     - --
     - 2026.10.4 / 2026.10.3.1
     - 2026.10.4 / 2026.10.3.1

MolSSI10 is not in the table: it was rebuilt on 2026-10-03 for OpenPBS
(jobserver, scheduler, webui 2026.10.3) and is test-only; read its versions at
execution. Its ``orca.ini`` has no ``code`` and ORCA is probably not installed
there, so no ORCA test on MolSSI10.

The explicit package list
-------------------------

Never ``--all`` on ChemAI or TinkerCliffs. The list, on every site where the
package is installed (``update`` only upgrades what is installed)::

    seamm-manager update --latest \
        seamm molsystem seamm-exec seamm-jobserver \
        orca-step mopac-step loop-step read-structure-step table-step \
        geometry-analysis-step forcefield-step

plus, where present, ``seamm-webui`` (its own venv), ``properties-step``,
``mbe-step`` and ``seamm-mbe`` (if the manager still refuses the last two as
"not a SEAMM package", install them in place afterwards as on 2026-10-04:
``uv pip install mbe-step==2026.10.5 seamm-mbe==2026.10.5.1`` into the new
venv). seamm-scheduler is not in the package list and comes in through
seamm-exec's requirement; check it lands at 2026.10.5. ``--dry-run`` first on
every site, and read the dry run for anything not on the list.

**Held back in this round, on every cluster:** ``xnn-step`` and ``lammps-step``.
Their installers apply ``seamm-xnn.yml``/``seamm-lammps.yml`` to the environment
``xnn.ini``/``lammps.ini`` names, which on ChemAI (``seamm-lammps``, shared with
Sina) and TinkerCliffs (``seamm-lammps-xnndev``) is hand-built. seamm-manager
2026.10.5.1 leaves such an environment alone, deciding from conda's history
that SEAMM did not create it (#31; the 2026.10.5 rule keyed on SEAMM's
``seamm-*.sha256`` records alone, which the TinkerCliffs environment acquired in
the 2026-10-04 incident, so it looked SEAMM-made). The rule has been tested
against real histories on the Mac, not yet on a live cluster environment.

So: this round updates the list above with the two steps excluded; a **separate,
deliberate step** afterwards -- with seamm-manager 2026.10.5.1 as the tool and
xnn-step 2026.10.5 on PyPI -- lifts the hold on one site, TinkerCliffs first,
where the environment is not shared with another user: ``conda list --explicit``
and ``pip freeze`` of the MLFF environment before, ``update --latest xnn-step``,
the same after, and the diff must be empty (the installer should print that the
environment "was not created by SEAMM, so it is left as it is"). Only then does
ChemAI follow. On the Macs the two steps update freely: their
``seamm-xnn``/``seamm-lammps`` environments are SEAMM's own, and the Mac's
``seamm-xnn`` gets torch chosen for it (PyPI's wheel, with MPS).

Order of sites, and why
-----------------------

1. **~/SEAMM (this Mac).** Lowest stakes, exercises the manager-tool upgrade
   and the migrate path (this venv is on 2026-10-03). Also the first place the
   ORCA timing rows appear.
2. **~/SEAMM_DEV.** Already at phase 7 (``venvs/2026-10-05T14-26-50``, rebuilt
   from PyPI 2026-10-05 with no editable installs, manager 2026.10.5), so only
   seamm-exec and orca-step move. Its phase-7 soak configuration was removed the
   same day (``[local]`` back to ``type = local``, the soak copy kept as
   ``PaulVT.local.ini.phase7-soak``; ``taskserver/`` and the test venvs deleted),
   so there is no configuration to preserve. Resuming a TaskServer soak on an
   everyday installation (NOTES_phase8 item 11) is a separate decision for Paul.
3. **Mac mini (paul.local, ``ssh macmini``).** Furthest behind of the Macs
   (mopac-step 2026.7.27, table-step 2025.6.1); a real update of a whole
   installation before the clusters. Non-login zsh there needs
   ``PATH=$HOME/.local/bin:$PATH``. ``~/phase7_test`` (a private venv and queue)
   is the phase-7 validation leftover; it is not ``~/SEAMM`` and is untouched by
   this rollout -- its removal is on the phase 8 cleanup list.
4. **MolSSI10.** Test-only, OpenPBS; proves the list on a Debian box with no
   services to protect. No ORCA there, so the MOPAC table flowchart only.
5. **TinkerCliffs** (``/projects/seamm/SEAMM``, no services). Check that no
   running job uses the venv (each job's command; the ORCA/VASP farm scripts
   under ``/projects/seamm/psaxe`` do not). The manager's closing scan over the
   NFS jobs took about 15 minutes last time. No dashboard runs there, so the
   job-array/dashboard restart rule does not apply.
6. **ChemAI**, last, on Paul's explicit ask for this list (hands-off rule).
   Nothing in SLURM for ``seamm``, nothing submitted or running in the
   datastore; stop the JobServer and web UI, update, start them (``services
   start jobserver`` then the web UI; with manager 2026.10.5 the update should
   restart only what was running -- verify, since #26 is new). Since ChemAI's
   ARC queues submit to TinkerCliffs' venv, TinkerCliffs goes first so that
   both ends run the same seamm-exec/scheduler when the first job crosses.

Per-site checklist
------------------

Before:

- ``grep conda-environment <root>/*.ini`` -- know which code environments exist
  and which are hand-built.
- No job uses the venv; services stopped between jobs where there are services.
- Record ``readlink <root>/venv`` (the rollback) and the versions (the table).

Do:

- Right after a release, uv may still see the old versions even with
  ``--refresh``: it reads the JSON form of PyPI's simple index (``Accept:
  application/vnd.pypi.simple.v1+json``), which PyPI's CDN caches separately
  from the HTML page. Before an install that needs a new version, check that
  form shows it::

      curl -s -H 'Accept: application/vnd.pypi.simple.v1+json' \
          https://pypi.org/simple/seamm-manager/ | grep -o '2026\.10\.5\.1' | head -1

- ``uv tool install --force --python 3.12 seamm-manager==2026.10.5.1`` (the tool,
  not the venv).
- ``seamm-manager update --latest --dry-run <list>``; read it.
- ``seamm-manager update --latest <list>`` (+ ``seamm-webui`` where it has one).
- ``uv pip check`` in the new venv.
- Start services where they were running; confirm the queues list (ChemAI: 16).

Verify:

- ``seamm-flowchart steps`` lists ORCA and MBE where expected.
- **The MOPAC table flowchart** (job 5165's; three molecules from SMILES) on each
  site's own queue, as on 2026-10-04 and 2026-10-05; results identical to jobs
  5167/5168 (molsystem's fixed RDKit seed).
- **An ORCA flowchart with a sub-step** (Energy or Optimization of water, def2-SVP)
  on ChemAI's ``ChemAI`` queue and on ``tinkercliffs_debug``: this is the proof
  that ``orca.ini`` is read (``code`` is ``/home/software/orca_6_1_1_avx2/orca`` on
  ChemAI, ``/apps/common/software/ORCA/6.1.1-gompi-2023b-avx2/bin/orca`` on
  TinkerCliffs, both with ``library-path``). The step's output says "Ran ORCA
  directly in ..." or "in node-local scratch ..."; ``orca.out`` must be ORCA's,
  not the screen reader's.
- ``~/.seamm.d/timing/orca.csv`` on the Mac, ChemAI and TinkerCliffs has a row
  from that job whose ``machine`` column is sensible (e.g.
  ``tinkercliffs:normal_q:AMD EPYC 7702 64-Core Processor``). If a cluster's
  environment lacks ``SLURM_CLUSTER_NAME``, the key degrades to partition and
  CPU model -- note it here, since the fit keys on it.
- ChemAI only: the held xnn-step 2026.9.28 and lammps-step carried over
  unchanged, and the ``seamm-lammps`` environment's ``conda-meta/history`` is
  unchanged (last written 2026-09-19).

Rollback: switch ``<root>/venv`` back to the recorded ``venvs/<stamp>`` (the
manager's ``environment`` commands); the old venv is kept.

Risks
-----

- **A code installer touching a hand-built environment** -- covered by the hold
  and the sha256-record check above; the only risk of this rollout that has
  bitten before (2026-09-27, 2026-10-04).
- **Updating a venv in use** -- the build-beside-and-switch mechanism refuses the
  switch while processes started through ``<root>/venv`` run; still check first.
- **ChemAI ↔ TinkerCliffs skew** -- avoided by order (TinkerCliffs first) and by
  doing both the same day; otherwise ``max_resubmits`` on ChemAI's ARC queues
  would need attention.
- **The dry run is the control**: anything in it that is not on the list is a
  reason to stop and look, not to proceed.

Record of execution
-------------------

(to be filled in: date, per site the versions before/after, the new and rollback
``venvs/<stamp>``, the test jobs and their results, anything unexpected)
