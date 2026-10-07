Rollout plan: phases 6 and 7, the orca.ini fix and the timing records
=====================================================================

Status: PLAN, written 2026-10-05 at Paul's ask ("fully update all installations
here, and on ChemAI, MolSSI10, ARC and the Mac mini"). To be executed once
seamm-exec and orca-step 2026.10.5.2 and seamm-manager 2026.10.5.1 are on PyPI
(all four released 2026-10-05: seamm_exec PR #46, orca_step PR #46,
seamm_manager PR #32, xnn_step PR #7). The record of what was actually done goes below the plan, as
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
5. **xnn-step 2026.10.5**: its installer uses that torch path, and ``torch`` is
   out of ``seamm-xnn.yml``. This is what lets the xnn-step hold on the clusters
   be lifted. It requires Python 3.12 or later (seamm-manager 2026.10.5.1 has no
   3.11 release); every seamm-manager venv is 3.12, but check each site's
   ``<root>/venv/bin/python --version`` before its update.

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
     - 2026.10.5
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
- ``<root>/venv/bin/python --version`` is 3.12 or later (xnn-step 2026.10.5 needs it).

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

**Change of approach (Paul, 2026-10-05, mid-rollout):** the explicit list was
the workaround for the installer hazard that seamm-manager 2026.10.5.1 and
xnn-step 2026.10.5 remove, so after the first two Macs the rollout switched to
the plain ``update --latest --all``, to see the new installer behave on a real
hand-built environment. The Macs' environments are SEAMM's own, so the list
made no difference there.

2026-10-05, this Mac (``~/SEAMM``)
    seamm-manager tool 2026.10.2.3 -> 2026.10.5.1. ``update --latest --force
    <list>`` (the ``--force`` for the link-started MCP servers, Paul's standing
    call; no jobs running; JobServer and web UI running, restarted by the
    update). New ``venvs/2026-10-05T18-33-32``, rollback
    ``venvs/2026-10-03T06-19-42``. seamm 2026.10.5, molsystem 2026.10.5,
    seamm-exec 2026.10.5.2, orca-step 2026.10.5.2, mopac/loop/read-structure/
    table/geometry-analysis/lammps/xnn-step 2026.10.5, forcefield-step
    2026.10.4, seamm-webui 2026.10.5; ``uv pip check`` clean. Installers:
    ``seamm-xnn`` got openssl 3.6.4 -> 3.6.5 (conda) and xnns 0.4.0 -> 0.7.0
    (``xnns>=0.4.0``); torch 2.14.0 left alone; ``seamm-lammps`` unchanged.
    ``import torch, xnn, mdi`` OK, MPS available. Test: water ORCA Energy
    (B3LYP/def2-SVP via a Model Chemistry step) with ``run_flowchart``: "Ran
    ORCA directly in the job directory", E = -76.31877069 Eh; first row in
    ``~/.seamm.d/timing/orca.csv``, machine ``Apple M3 Pro``, wall 2.5 s,
    ``nbf`` 24, 11 SCF cycles.

2026-10-05, ``~/SEAMM_DEV``
    ``--development update --latest seamm-exec orca-step``: new
    ``venvs/2026-10-05T18-39-35`` (rollback ``2026-10-05T14-26-50``), both at
    2026.10.5.2; then ``--development update --latest --all``: no further venv
    change, every code installer ran, seamm-webui 2026.10.3 -> 2026.10.5. Same
    ORCA test OK; second ``orca.csv`` row.

2026-10-05, Mac mini (``paul.local``)
    Tool -> 2026.10.5.1; ``update --latest <list>`` (no link-started
    processes, no jobs). New ``venvs/2026-10-05T18-41-48`` (rollback
    ``2026-10-03T06-27-01``); versions as on the Mac; seamm-webui 2026.8.13.1
    -> 2026.10.5; ``uv pip check`` clean; services running. Both
    ``seamm-xnn`` and ``seamm-lammps`` there were created by SEAMM (history
    ``env create --file .../seamm-*.yml``).

ChemAI (user ``seamm``), pre-checks 2026-10-05
    ``venvs/2026-10-04T15-37-21``, Python 3.12.14, tool 2026.10.2.3; no SLURM
    jobs, no running flowcharts; JobServer and web UI running. ``xnn.ini`` AND
    ``lammps.ini`` name ``seamm-lammps-xnn060``, created by ``conda create
    --clone seamm-lammps`` -- not SEAMM-made, no ``seamm-*.sha256`` records;
    torch 2.13.0+cu126, xnns 0.6.0, e3nn 0.4.4, vesin-torch 0.6.1. Driver CUDA
    12.2 (-> cu128 if torch were missing; it is present and must be left
    alone). Snapshots in ``~seamm/rollout-snap/`` (conda explicit + pip
    freeze of ``seamm-lammps-xnn060`` and ``seamm-lammps``, venv versions). The
    ``--all`` update itself was not run by the Claude session (its permission
    rules refused a change to the production machine); Paul ran it.

2026-10-05, ChemAI, ``update --latest --all`` (Paul, from his terminal)
    Tool -> 2026.10.5.1. New ``venvs/2026-10-05T19-00-48``, rollback
    ``2026-10-04T15-37-21``; it took longer than expected (the twelve code
    installers each probe their environment). Changed: seamm 2026.10.5,
    molsystem 2026.10.5, seamm-exec 2026.10.5.2, orca-step 2026.10.5.2,
    mopac/loop/read-structure/table/geometry-analysis/lammps/mbe-step 2026.10.5,
    **xnn-step 2026.9.28 -> 2026.10.5 (the hold lifted)**, seamm-manager in
    the venv 2026.10.5.1; strain-step 2026.10.5 appeared (a new requirement).
    ``uv pip check`` clean. JobServer and web UI restarted (systemd user units).
    **The hand-built ``seamm-lammps-xnn060`` is byte-for-byte unchanged**
    (conda explicit list and pip freeze identical before and after; torch
    2.13.0+cu126 still sees the GPU; ``import torch, xnn, mdi`` OK), as is
    ``seamm-lammps``: the history-based ownership rule (#31) held on the first
    real case. Test: the water ORCA Energy flowchart run as ``seamm`` with
    ``run_flowchart``: "Ran ORCA directly in the job directory", E =
    -76.31877069 Eh (identical to the Macs), so ``orca.ini`` is read and the
    Debian screen reader is no longer picked up. ``orca.csv`` row: machine
    ``AMD EPYC 7763 64-Core Processor`` (no cluster/partition: the test ran
    outside SLURM; a job through the ``ChemAI`` queue will carry them), wall
    5.4 s.

2026-10-06, TinkerCliffs (``/projects/seamm/SEAMM``), ``update --latest --all``
    Paul's ask ("include the mbe packages"); run by the Claude session over
    ssh (this one was not refused). Tool -> 2026.10.5.1. New
    ``venvs/2026-10-06T06-46-45``, rollback ``2026-10-04T15-23-39``; about 40
    minutes, most of it the format-2.0 flowchart scan over the NFS jobs (see
    below) and the thirteen code installers. seamm 2026.10.5, molsystem
    2026.10.6, seamm-exec 2026.10.5.2, orca-step 2026.10.5.2, mopac/lammps/
    xnn-step 2026.10.5 (xnn-step 2026.10.2 -> 2026.10.5), **mbe-step 2026.10.6
    and seamm-mbe 2026.10.6** (now in the package list, so ``--all`` took
    them), strain-step 2026.10.5, vasp-step 2026.10.3.1 (unchanged); ``uv pip
    check`` clean; no services there. Both ``xnn.ini`` and ``lammps.ini`` name
    ``seamm-lammps-xnn060`` (a clone, like ChemAI's); it and
    ``seamm-lammps-xnndev`` (the clone with the stale ``seamm-*.sha256``
    records from 2026-10-04) are **byte-for-byte unchanged**: the history rule
    held on both. ORCA test: on the login node the real ORCA started (so
    ``orca.ini`` is read) but was too slow to finish water in 10 minutes;
    repeated on a compute node through ``srun -p normal_q -n 1 -c 2``: "Ran
    ORCA in node-local scratch (/localscratch/7869980/...)", E = -76.31877069 Eh
    (identical to the Macs and ChemAI), wall 4.8 s; ``orca.csv`` row with
    machine ``tinkercliffs:normal_q:AMD EPYC 7702 64-Core Processor`` -- the
    full key, since the run was inside a SLURM allocation.

2026-10-06, MolSSI10 (test-only, OpenPBS), ``update --latest --all``
    Tool -> 2026.10.5.1. New ``venvs/2026-10-06T07-22-19``, rollback
    ``2026-10-03T17-50-02``; no PBS jobs, JobServer and web UI restarted by the
    update. seamm 2026.10.5, molsystem 2026.10.6.1, seamm-exec 2026.10.5.2,
    seamm-scheduler/jobserver 2026.10.5, orca-step 2026.10.5.2, mopac/lammps/
    xnn/loop/table/read-structure/geometry-analysis/strain-step 2026.10.5,
    forcefield-step 2026.10.4, seamm-webui 2026.10.5; ``uv pip check`` clean.
    Its code environments are SEAMM-made (``conda-env create --file
    .../seamm-*.yml``): ``seamm-xnn`` got xnns 0.4.0 -> 0.7.0 and the openssl
    patch, torch 2.14.0 left alone (no GPU there); ``seamm-lammps`` unchanged.
    Test: a MOPAC water Energy flowchart with ``run_flowchart`` finished (no
    ORCA on MolSSI10).

**Rollout complete 2026-10-06:** all six installations on the 2026.10.5.x
releases, the xnn-step hold lifted everywhere, every hand-built MLFF
environment untouched, ORCA read from ``orca.ini`` on ChemAI and TinkerCliffs,
and ``orca.csv`` rows from the Mac, ChemAI and TinkerCliffs.

The format-2.0 flowchart scan: ``update`` scanned every job's flowchart on
every run (about 15 minutes on TinkerCliffs' NFS). seamm-manager 2026.10.6
(dev) records a clean scan in ``<root>/installation.ini`` and skips it
afterwards; ``flowcharts status`` always scans and refreshes the record.

Second round, 2026-10-06: the 2026.10.6 releases
================================================

Paul's ask, once seamm-exec, seamm-manager and the six code steps were on
PyPI (the item-13 timing records and the scan-once fix). Tool
``seamm-manager==2026.10.6`` then ``update --latest --all`` everywhere; the
installer behaviour on the hand-built environments having been proven the
day before, no snapshots this time. ``--all`` also brought in what the first
round's explicit list had skipped (strain-step, energy/dimer-builder/
normal-mode-sampling steps, molsystem 2026.10.6.1).

================ ======================= ====================== ==============================
Site             new venv                rollback               notes
================ ======================= ====================== ==============================
Mac ``~/SEAMM``  2026-10-06T10-39-42     2026-10-05T18-33-32    MOPAC test: old 62 MB mopac.csv
                                                                set aside, new schema row
``~/SEAMM_DEV``  2026-10-06T10-42-14     2026-10-05T18-39-35
MolSSI10         2026-10-06T10-44-04     2026-10-06T07-22-19    flowchart record written
ChemAI           2026-10-06T10-45-12     2026-10-05T19-00-48    job 5175 was running (below)
Mac mini         2026-10-06T10-49-50     2026-10-05T18-41-48    reachable from work after all
TinkerCliffs     2026-10-06T10-46-31     2026-10-06T06-46-45    EC pilot was running (below)
================ ======================= ====================== ==============================

``uv pip check`` clean and services running on every site; every site's
``installation.ini`` now has ``[flowcharts] format = 3.0``, so the next update
skips the scan.

**ChemAI, a lapse:** the update ran while job 5175 (``seamm-5175``, 37 min in)
was running. The versioned switch left its venv (``2026-10-05T19-00-48``) in
place and the job continued on it; the restarted JobServer reattached ("previous
jobs: 2"). No harm, by design -- but the rule is to wait for running jobs, and
the pre-check should gate the update, not just print.

**TinkerCliffs, a second lapse of the same kind:** the switch at 10:46 happened
while the MBE session's EC pilot (job 7870006, started 07:21, with 18 bundle
jobs) was running from the main venv; the login-node check cannot see
compute-node evaluators, and the pre-check only counted my own SLURM jobs
without gating. Checked afterwards: the evaluator runs from the real path
``venvs/2026-10-06T06-46-45`` and writes that same real path into every
bundle's ``run.sh`` (``exec .../venvs/2026-10-06T06-46-45/bin/python -m
seamm_exec.task_worker``), so the whole job stays on one version; no bundle
failed. The versioned-venv design covered it, but **the rule for a cluster is:
ask the sessions with running evaluators (mbe) before switching, or wait.**
installation.ini has the flowchart record; ``uv pip check`` clean.

Rounds 3–4, 2026-10-06: the 2026.10.6.1 and 2026.10.6.2 releases
----------------------------------------------------------------

Two further rounds the same afternoon, driven by the installer downgrade
(seamm-manager 2026.10.6.1: version floors, a refused switch exits 1), the
ORCA NoCOSX batched-gradient guard (orca-step 2026.10.6.2), the fail-fast
MBE step (mbe-step 2026.10.6.2), the JobServer reattach KeyError fix
(seamm-jobserver 2026.10.6) and seamm-exec 2026.10.6.2. Tool
``seamm-manager==2026.10.6.1`` then ``update --latest --all``.

================ ====================== ====================== ==============================
Site             round 3 venv           round 4 venv           notes
================ ====================== ====================== ==============================
Mac ``~/SEAMM``  2026-10-06T14-02-59    2026-10-06T17-27-42    stray 14-34-47 (the downgrade)
                                                               to prune
``~/SEAMM_DEV``  2026-10-06T14-05-28    2026-10-06T17-31-58
MolSSI10         2026-10-06T14-07-30    2026-10-06T17-37-54
Mac mini         2026-10-06T14-08-37    2026-10-06T17-43-34
ChemAI           2026-10-06T14-10-28    2026-10-06T17-37-31    JobServer crash-loop (below)
TinkerCliffs     (skipped)              2026-10-06T20-05-52    gated on science's jobs (below)
================ ====================== ====================== ==============================

**ChemAI, 14:11–14:25:** the restarted JobServer (2026.10.5) crash-looped on
reattach: jobs that had finished while it was down have no ``slurm_job_id``
in the reattach record and the lookup raised ``KeyError``. One-line fix with
the worker session, released as seamm-jobserver 2026.10.6 and rolled out in
round 4.

**TinkerCliffs, the rule applied this time:** round 3 was skipped and round 4
held until the gate was met.  The science session's six label jobs
(ChemAI 5213/5217/5220–5223 = SLURM 7874435/39/44/48/49/50) finished at
20:03 EDT; the MBE session had agreed the switch could happen under its EC
pilot (7870006, with bundle 7876699), because the evaluator and every
bundle's ``run.sh`` pin the real path ``venvs/2026-10-06T06-46-45`` (do not
prune it until 7870006 has finished).  Update 20:05–20:25 EDT, rc=0,
``/projects/seamm/SEAMM/venv -> venvs/2026-10-06T20-05-52``; also picked up
strain-step 2026.10.6 and supercell-step 2026.10.6.  ``uv pip check`` clean;
no services on TinkerCliffs; the flowchart record meant no scan.  Science
told to release its held ion-shell relabels; MBE told the switch had happened.
