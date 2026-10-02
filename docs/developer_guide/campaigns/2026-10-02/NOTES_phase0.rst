Phase 0 notes
=============

2026-10-02 -- versioned environments in seamm-manager
-----------------------------------------------------

Done on ``seamm_manager`` ``dev`` (uncommitted to main, unreleased): the rule
"never update a venv in place while jobs run".

- ``<root>/venv`` becomes a symlink to ``<root>/venvs/<stamp>``. An update builds
  the new version from ``uv pip freeze`` of the current one (4 s with uv's cache,
  1.3 GB; editable and local installs survive), applies the change there, and
  switches the link.
- CPython does not resolve the link when it locates a venv, so every launcher
  (launchd plist, systemd unit, service bundle, desktop apps, the migrated
  scripts' shebangs) is rewritten to the real path. A switch is refused while a
  process started through the link exists; ``--force`` overrides.
- New: ``environment versions | migrate | switch | rollback | prune``;
  ``recreate`` builds beside instead of deleting.
- Validated on ``~/SEAMM_DEV``: migration under a running JobServer; a real
  update of ``seamm`` 2026.10.2 to 2026.10.2.2 whose switch was refused by a
  link-started process and completed later with ``environment switch``;
  rollback and back; prune keeps in-use and young versions; job 4002 reported
  ``sys.prefix`` under ``venvs/<stamp>``.

Found on the way
~~~~~~~~~~~~~~~~

- ``Uv.install(..., upgrade=True)`` without a lock passes every installed
  package's requirements as ``-r`` context; with ``--upgrade`` uv treats those
  as requests and upgrades them too (the old environment's ``seamm`` and
  ``seamm-manager`` moved during the development-packages step). The
  development packages now ride along in the new build, which closes the leak
  for updates, but the ``-r`` semantics deserve a look.
- ``venv-webui`` is still updated in place (no jobs run from it; a restart is a
  brief outage). Same treatment later if wanted.
- The plug-in installers update the codes' conda environments in place; the
  Python-based engines (xnn, the MOPAC MDI engine) import lazily too.

Still to do in phase 0
~~~~~~~~~~~~~~~~~~~~~~

- The comparison harness: run a flowchart in two installations and diff the
  results files and tables.
- Release seamm-manager and apply the migration to ~/SEAMM, paul.local, then
  ChemAI (on an explicit ask) and ARC.
