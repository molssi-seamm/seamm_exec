Phase 4 notes
=============

2026-10-03 -- plan and decisions
--------------------------------

Phase 4 moves flowchart tables from in-memory pandas DataFrames into the job's
SQLite file (``seamm.db``), beside the structures and properties.

The design doc called for a DataFrame-shaped facade with cell writes going
straight through to SQL. The survey below showed that the four steps that
touch tables directly depend on pandas idioms a facade would have to imitate:

- ``pandas.concat`` and then a *new* DataFrame put back into the handle;
- ``table[col] = default`` to add a column;
- ``.at`` (including its enlargement, which creates rows), ``.iat``,
  ``.index.get_loc``, ``.dtypes``, ``.shape`` and the ``to_*`` writers.

Paul's decisions:

- **Go the whole way now.** The database is the only copy of a table. No
  facade that imitates pandas.
- **No mirror.** The alternative considered was to keep the DataFrames as the
  truth and sync changed tables into ``seamm.db`` after each node. Rejected as
  temporary and brittle: two copies of every table, and a sync layer that
  phases 5 and 6 would depend on.
- **Cut over in one release; no switch and no in-memory backend.** The design
  doc's switch predates phase 0. Rollback is now
  ``seamm-manager environment rollback`` to the previous venv, which is one
  command, covers every package in the release, and is what any other release
  relies on. The soak and the A/B comparison happen in ``~/SEAMM_DEV`` before
  the release, between two venvs, so they need no switch either. Dropping the
  switch removes the second backend, halves the test matrix, and removes the
  trap of a backend that runs serially but cannot checkpoint or merge.
- **A read-only structure database cannot hold tables.** A flowchart run with
  ``--read-only`` that writes a table stops with a clear message saying so.
  Today such a run can build tables only because they live in memory. Reads,
  such as a Loop over rows, still work.

The worker's proposal, reviewed and refined by the design session:

- **An explicit, small SEAMM Table API** (below), which the consumers are
  converted to once.
- **One backend:** SQLite in ``seamm.db``. (The proposal had an in-memory
  DataFrame backend behind a switch as the rollback; Paul dropped it, above.)
- **A stable internal row id** resolves today's label-versus-position
  ambiguity of ``current index``.

The design session's points, all adopted:

1. Build the SQLite backend on molsystem's existing ``_Table``
   (``molsystem/table.py``) rather than writing a second SQL table class.
2. Iterations of a parallel Loop *update* existing rows, they do not only
   append. The commonest pattern is a Loop "For rows in table" with
   ``store_results`` writing into the current row. So the backend keeps a
   change journal, and phase 6 merges by replaying each child's journal in
   iteration order. This answers the design doc's open question 2 (two
   iterations writing the same cell): it becomes a policy on the journal.
3. Row ids are internal and are not stable across a parallel-Loop merge
   (children's appended rows collide and are renumbered). The user-visible key
   is the index column, or the 1-based position for a table without one.
4. molsystem commits per operation, so a transaction per node is not free; do
   not try. The evaluator commits table writes before it writes
   ``checkpoint.json`` (phase 5), so after a crash the database is at most one
   node ahead of the checkpoint, never behind.
5. The risk is in materialization fidelity, not the API: exports and
   ``to_dataframe()`` must reproduce today's files (see *Fidelity*).
6. ``handle["table"]`` for unconverted code returns a *fresh* DataFrame on
   every read, with a logged warning that writes to it are not saved.
7. Layering: the SQLite backend in molsystem (it owns the connection, WAL,
   ATTACH for the merge, copy and diff); the SEAMM-facing API (handles, the
   registry's meaning, current row, exports, variables, ``store_results``) in
   seamm. molsystem does not learn about flowchart variables.
8. (Superseded by the cut-over decision.) The design session proposed
   removing the in-memory backend at the flip and gating the parallel Loop and
   checkpoint resume on SQLite meanwhile; with one backend there is nothing
   to gate.
9. Creating the current row on first write is a requirement, not a quirk to
   reconsider: ``get_table`` makes an empty table with ``current index`` 0 and
   the first ``store_results`` creates row 0, the commonest table pattern in
   the corpus. ``set_cell`` on a current row one past the end appends it; a
   row further past the end is an error. (Today ``.at`` would create any
   label; only "Go to the next row" twice without a write reaches that.)
10. **Prevent, don't catch, mixed versions.** At job start, before any node
    runs, the evaluator checks the flowchart's step types against a small
    minimum-version table in seamm for the four converted plug-ins and refuses
    with "update X to >= Y". The run-time refusal of a plain pandas handle stays
    as the backstop. The other direction (new plug-in, old seamm) is handled by
    their ``seamm>=`` pins.
11. A job started with the new version and then rolled back is simply rerun;
    old job directories are unaffected either way.

Survey
------

Who touches tables (workspace grep, 2026-10-03, excluding ``old/``, ``tmp/``
and the cookiecutter):

================================  ==============================================
Code                              Use
================================  ==============================================
``seamm/node.py``                 ``get_table()`` (creates on demand) and
                                  ``store_results()``, which writes cells at
                                  ``current index`` with ``.at``, adds columns
                                  with ``table[col] = default`` and records
                                  ``defaults``. About 25 plug-ins write tables
                                  only through ``store_results``; they need no
                                  change.
``table_step``                    Create, Read (csv/json/xlsx/txt), Save, Save
                                  as, Print, Print the current row, Append a
                                  row, Go to the next row, Add columns, Get
                                  element, Set element.
``loop_step``                     "For rows in table": iterates the index
                                  labels (all rows, or a ``where`` test on one
                                  column using ``dtype.type`` for the value),
                                  sets ``current index`` to the label, sets
                                  ``_row``, ``_loop_index`` and optionally one
                                  variable per column, names ``iter_N``
                                  directories from the label.
``properties_step``               Creates the table if missing, appends rows
                                  by ``concat``, adds columns, saves to a file.
``geometry_analysis_step``        ``get_table()``, adds columns, appends rows
                                  by ``concat``, sets ``current index`` to the
                                  last row, saves with its own copy of the
                                  export code.
================================  ==============================================

The export code (csv/json/xlsx/txt with or without the index) is duplicated in
three places; it becomes ``Table.export()``. The Tk side only handles table
*names*, never data. No ``$variable`` expression reaches into a table.

Quirks of today's behaviour that the conversion must keep or deliberately
change (each one is a fidelity test):

- A new table's ``current index`` is 0, and ``store_results`` creates row 0 by
  ``.at`` enlargement. Without a Loop or a Table "Append a row"/"Go to the
  next row", every ``store_results`` writes the same row.
- Enlargement fills the *other* columns with NaN, not their defaults, so an
  integer or boolean column becomes float or object.
- "Append a row" fills unspecified columns from ``defaults``, then sets
  ``current index`` to the new last row.
- ``current index`` is a label in ``store_results``, the Loop and "Print the
  current row", and a position in "Get/Set element" (``iat``) and "Go to the
  next row" (``+= 1``). The two coincide unless the table has an index column.
- Non-scalar results are stored as compact JSON text.
- Geometry analysis writes formatted strings (``"1.0960"``) into columns
  created with float defaults, which makes them object columns.

Architecture
------------

molsystem (backend):

- User tables are stored under a prefixed SQL name, ``table_<n>``, never the
  display name. ``SystemDB`` keeps atom, bond, system, configuration, ... in
  the same namespace and ``create_table`` raises on an existing name.
- Every identifier is quoted. Users name columns "Energy (kJ/mol)".
- ``_Table`` gains: ``set_cell(rowid, column, value)``, ``get_cell``,
  ``append_rows(list_of_dicts) -> [rowid]``, ordered column listing,
  parameterized ``where`` (it has one in ``rows``/``delete``), and the
  journal hook.
- Found in ``_Table`` while surveying: ``add_attribute`` interpolates a string
  ``DEFAULT`` into the SQL without escaping, so a default containing ``'``
  breaks it (fixed); ``to_dataframe()`` uses the rowid as the index and ignores
  declared types (the user tables have their own). (An ``append()`` "undefined
  ``result``" reported in the first draft was a misreading.)

Two bookkeeping tables in ``seamm.db``:

``_tables`` (the registry), one row per user table:

  ``name`` (display name, unique), ``sql_name``, ``index_column``,
  ``current_row`` (a rowid), ``filename``, ``loop_index`` (bool),
  ``columns`` (JSON: ordered list of ``{name, type, default, null}``, where
  ``type`` is boolean/integer/float/string/json and ``null`` says how a
  missing value materializes: NaN, ``""`` or None).

``_table_changes`` (the journal):

  ``seq``, ``table``, ``rowid``, ``column``, ``op`` (``append``, ``set``,
  ``add_column``). Written by every mutating call. Phase 4 only writes it;
  phase 6 replays it. It also gives the Dashboard "what this step changed".

seamm (API): a ``Table`` class and ``TableHandle``. The flowchart variable for
a table stays a dict-like handle with ``"type"``, so existing
``variable_exists``/``get_variable`` calls keep working; its ``"table"`` key
materializes a fresh DataFrame (with the warning) and the rest of its keys
map onto the registry.

The Table API
-------------

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Call
     - Meaning
   * - ``Node.get_table(name, create=True)``
     - Returns a ``Table`` (not a DataFrame).
   * - ``Table.columns``
     - Ordered column names.
   * - ``Table.add_column(name, type, default)``
     - No-op if it exists with the same type.
   * - ``Table.append_row(**values)`` / ``append_rows([dict, ...])``
     - Unspecified columns get their defaults. Moves the current row to the last appended. Returns row positions.
   * - ``Table.current_row``
     - Get or set; the setter takes a position or an index-column value through ``locate()``.
   * - ``Table.locate(key=..., position=...)``
     - Index-column value or 1-based position to an internal row.
   * - ``Table.next_row()``
     - "Go to the next row".
   * - ``Table.set_cell(column, value, row=None)``
     - ``row=None`` means the current row; creates the row if it does not exist (today's enlargement), filling the other columns with their defaults.
   * - ``Table.get_cell(column, row=None)``
     - Same row rules.
   * - ``Table.rows(where=None)``
     - Iterate rows as dicts, in order; ``where`` is ``(column, op, value)`` with the Loop's operators.
   * - ``Table.n_rows``, ``Table.dtype(column)``
     - For the Loop's messages and value conversion.
   * - ``Table.to_dataframe()``
     - Read-only copy, with the declared types and the index column.
   * - ``Table.export(filename, file_type)``
     - csv, json, xlsx, txt exactly as today.
   * - ``Table.read(filename, file_type, index)``
     - Table step "Read" imports into the database.

Today's idioms to API calls (the conversions are mechanical):

.. list-table::
   :header-rows: 1
   :widths: 55 45

   * - Today
     - API
   * - ``set_variable(name, {"type": "pandas", ...})``
     - ``get_table(name)`` or ``Table.create(...)``
   * - ``handle["table"] = pandas.concat([t, rows])`` and ``handle["current index"] = shape[0] - 1``
     - ``table.append_rows(rows)``
   * - ``table[col] = default`` and ``handle["defaults"][col] = default``
     - ``table.add_column(col, type, default)``
   * - ``table.at[handle["current index"], col] = v``
     - ``table.set_cell(col, v)``
   * - ``handle["current index"] += 1``
     - ``table.next_row()``
   * - ``handle["current index"] = label`` (Loop)
     - ``table.current_row = table.locate(key=label)``
   * - ``table.iat[row, column]`` (Get/Set element)
     - ``get_cell``/``set_cell`` with ``locate()``
   * - ``{k: table.at[i, k] for k in table}`` (Loop)
     - the row dict from ``rows()``
   * - ``table.dtypes[column].type(value)`` (Loop)
     - ``table.dtype(column)``
   * - ``to_csv``/``to_json``/``to_excel``/``to_string``
     - ``table.export(...)``
   * - ``table.to_string(...)`` for Print
     - ``to_dataframe().to_string(...)``
   * - ``table.columns``, ``table.shape[0]``
     - ``table.columns``, ``table.n_rows``

Row identity
------------

One internal rowid per row, held only by the backend; the registry's
``current_row`` is a rowid. Users and flowcharts see:

- the index-column value, when the table has one (the Loop iterates these, and
  ``store_results`` writes at the matching row);
- otherwise the position, ordered by rowid. ``iter_N`` directory names,
  ``_loop_index`` and ``_row`` stay exactly as today (index-column value, or
  0-based position with ``iter_{position + 1}``).

Fidelity
--------

The comparison harness compares exported files and printed tables, so:

- ``to_dataframe()`` is built from the registry's declared types, not from
  SQLite's dynamic typing; NULL in an integer column must not turn ``1`` into
  ``1.0`` (a row created by a write gets the defaults; Paul's answer below).
- Empty string, NULL and NaN stay distinct (the property-units round-trip bug
  of 2026-09-20 was this).
- JSON cells stay TEXT.
- Column order is insertion order (``ALTER TABLE ADD COLUMN`` appends).
- Index column in exports exactly as today (``index=True`` only with one).

Commits and the rest of the campaign
------------------------------------

- **Commits:** the evaluator loop in ``seamm_exec/exec_flowchart.py`` commits
  after each ``node.run()``. Phase 5 writes ``checkpoint.json`` only after
  that commit.
- **Phase 5:** the current row lives in the registry, per table, so a
  checkpoint needs nothing extra for tables.
- **Phase 6:** a child's ``seamm.db`` is the selected configurations plus the
  tables (rows and registry) with an empty journal. Merge = replay the child
  journals in iteration order, renumbering appended rows.
- **Read-only and in-memory databases:** ``exec_flowchart`` can open a
  ``:memory:`` or read-only (``?mode=ro``) structure database. Tables work in
  ``:memory:`` (not persistent). Writing a table to a read-only database stops
  the job with a clear message (Paul's decision, above).
- **Mixed versions:** an old ``table_step`` with the new seamm would put a
  plain pandas dict into the variables. The start-up check (point 10) refuses
  the job before it runs; as a backstop ``store_results`` and ``get_table``
  refuse such a handle with a message naming the plug-ins to update, rather
  than silently writing to a DataFrame that is never saved.

Validation and release
----------------------

- Unit tests for the API and backend; every quirk listed in the survey has a
  test, with today's pandas output as the expected value.
- SEAMM_DEV A/B with the comparison harness (released venv vs the phase 4
  venv) over the Testing flowcharts and the local and Dropbox flowchart
  corpus, then a soak in SEAMM_DEV, then release. Rollback after release is
  ``seamm-manager environment rollback``.
- Release order: molsystem, then seamm (pinned ``molsystem>=``), then
  table_step, loop_step, properties_step and geometry_analysis_step (each
  pinned ``seamm>=``).

Paul's answers (2026-10-03)
---------------------------

- No switch: cut over in one release. A read-only database cannot hold
  tables.
- A row created by a write gets its columns' **defaults**, keeping the
  declared types (today: NaN, turning integer and boolean columns into float or
  object). Listed in HISTORY as an intended change; the harness diffs it shows
  are expected.
- Dashboard view of tables: deferred (phase 7, with the task view).
- Go ahead and code.

2026-10-03 -- implementation
----------------------------

Committed locally on each ``dev`` (not pushed):

======================================  ========  =============================================
Package                                 Commit    Change
======================================  ========  =============================================
``molsystem``                           0ad3f75   ``user_tables.py``: ``SystemDB.user_tables``,
                                                  ``UserTable``, registry and journal; quoted
                                                  string ``DEFAULT`` in ``add_attribute``.
``seamm``                               131646a   ``table.py``: ``seamm.Table``,
                                                  ``check_table_plugins``; ``get_table`` and
                                                  ``store_results`` converted.
``seamm_exec``                          e487896   Commit after each step; the start-up check.
``table_step``                          e8a84e1   All methods on ``seamm.Table``.
``loop_step``                           6ba6156   "For rows in table" on ``seamm.Table``;
                                                  commit after each body step.
``properties_step``                     14359c8   Export on ``seamm.Table``.
``geometry_analysis_step``              b9d04d0   Tables on ``seamm.Table``.
======================================  ========  =============================================

Choices made while coding:

- **Columns have no SQL type.** SQLite then stores exactly what was written
  (Geometry Analysis's text ``"1.0960"`` stays text, so its CSVs are
  unchanged); the declared type in the registry decides how values are read
  back and how ``to_dataframe()`` types the column.
- **The current row is a rowid or NULL**, NULL meaning "the row after the last":
  the next write appends it. A new or empty table starts there, so the first
  ``store_results`` creates row 0 as before. ``next_row()`` past the last row
  goes there, and does nothing if already there, so "Go to the next row" works
  at either end of a loop body (today's ``+= 1`` twice would have left a gap in
  the labels).
- **Assigning a DataFrame to** ``handle["table"]`` replaces the table's
  contents, keeping its name, index column and file, so unconverted code that
  does ``concat`` and replace still saves its rows. Reading it gives a copy,
  with a warning.
- ``get_table`` adopts a table that is in the database but not yet a variable
  (a database given with ``--database``; later, checkpoint restore).
- The plug-in check treats a development checkout (a version with a local part,
  ``2026.9.30+3.g1234abc``) as current. The minimum versions in
  ``seamm.table.table_plugins`` are placeholders (2026.10.4) until the release.

**Correction to the design (both sessions had it wrong):** the Loop does *not*
return its body's steps to the evaluator's loop; it runs them in its own
``run()``. The per-step commit is therefore in both ``exec_flowchart`` and the
Loop. Phase 5's checkpoint hook must likewise be called from both, or moved to
a shared helper.

Pre-existing bugs fixed on the way (found by the A/B runs, where the released
code failed):

- The Loop's row selection converted the second value even when only
  ``between`` uses it, so any test on a numeric column with an empty second
  value raised ``ValueError``.
- The Table step's Get/Set element by row always did ``int(row)``, so a table
  with a text index column could not be addressed by its index.

Validation (2026-10-03)
~~~~~~~~~~~~~~~~~~~~~~~

- Unit tests: molsystem 322 (23 new), seamm 251 (17 new), seamm_exec 102,
  table_step 17 with the flowchart tests (``--integration``), loop_step 4,
  properties_step 1, geometry_analysis_step 1.
- SEAMM_DEV A/B with ``seamm-manager compare``: ``venvs/phase4-A`` (today's
  releases, plus ``properties-step`` from PyPI, which SEAMM_DEV lacked) against
  ``venvs/phase4-B`` (A plus the seven checkouts, editable), on
  ``Testing/phase4/``: ``p4_rows`` (append, loop over rows, a ``where``
  selection, Set/Get element, Read with an index, csv/json/txt), 
  ``p4_results_only`` (``store_results`` with no Table step, "Go to the next
  row"), ``p4_props_geom`` (Properties, Geometry Analysis single and separate
  tables), ``builder_loop``, and table_step's ``test1`` and
  ``append_text_rows``.

  - Every CSV, the xlsx contents and every printed table are identical, except
    the intended changes: appended rows get their columns' defaults (``x``
    where A left NaN), and "Get element" gives Python values (``4.0``, not
    ``np.float64(4.0)``).
  - In ``p4_rows`` A stopped at each of the two bugs above; B ran through.
  - Otherwise only versions, timings, paths, reference wrapping and
    ``seamm.db`` differ.

Review (2026-10-03)
~~~~~~~~~~~~~~~~~~~

The design session and a subagent read all seven commits, diffed each consumer
against the released code and checked findings with probe scripts. Fixed
(molsystem 5f642e6, seamm 3973343, seamm_exec 0c3b317, loop_step 8327ae6,
properties_step 6cfb0e3):

1. **(must) Read-only databases.** Setting the current row, the loop flag or
   the file wrote the registry, so a Loop over rows or a Save from a
   ``--read-only`` database raised. These three are navigation state; with a
   read-only database ``seamm.Table`` keeps them in the handle.
2. **Text index lookups.** Untyped columns stored the integer 3 written to a
   text column as INTEGER, which ``WHERE col = '3'`` does not match. Values
   written to string and json columns are now stored as text.
3. **Create validated too late.** A wrong index column raised after the
   previous table had been dropped and the new one created. Columns and the
   index column are now checked first. Replacing a table from a DataFrame keeps
   the declared types and defaults of the remaining columns.
4. **Printing missing text.** Missing text cells printed ``None`` where pandas
   printed ``NaN``; text columns now materialize missing values as NaN.
5. **Row tests on missing values** raised ``TypeError``; they now fail every
   test except the negative ones (``!=``, "does not ..."), as NaN did.

Also fixed from the review's notes: SQL column names are internal (``c1``,
``c2``, ...), because SQLite column names ignore case and pandas allowed ``E``
and ``e`` side by side; Properties maps the database's ``int``/``float``/
``str``/``json`` property types (it tested ``integer``, so every int property
became a text column); JSON files written by ``export`` read back
(``orient="table"``); defaults are stored as written values (a list default
works); row ids use ``AUTOINCREMENT`` and are never reused; and a shared
``seamm.step_completed(node)`` (``seamm/checkpoint.py``) does the per-step
commit for both the evaluator and the Loop, so phase 5 has one place to hook.

Pins and versions for the release: seamm requires ``molsystem>=`` the new
molsystem (seamm's tests fail against molsystem 2026.9.25); seamm_exec and the
four steps require ``seamm>=`` the new seamm; ``table_plugins`` gets the real
versions. Release order: molsystem, seamm, seamm_exec, the four steps.

For HISTORY (behaviour changes):

- Tables are stored in the job's ``seamm.db``.
- New rows get their columns' defaults, not NaN (appended rows and rows created
  by a write); integer and boolean columns keep their types.
- "Add columns" uses the evaluated column name and records its default.
- "Append a row" to a table with an index column works (concat used to drop the
  index).
- "Go to the next row" past the last row does nothing more.
- Get/Set element by a text index value works; the Loop's ``where`` on a
  numeric column with an empty second value works.
- Loop ``_row`` values and "Get element" values are Python, not numpy, scalars.
- Properties: integer properties become integer columns; with nothing to export
  the current row is no longer set to -1.
- ``iter_N`` directory names are unchanged.

Notes for phases 5 and 6 (from the review):

- The journal records the operation, row and column only: no value, no step or
  iteration tag, no registry changes. That is enough to replay from a child's
  database, but ``drop`` and ``create`` must be replayed too. The inherited
  ``_Table.delete``/``clear`` are not journaled; don't use them on user tables.
- The Loop's selected rows and its ``self.table`` live in memory; a checkpoint
  resume inside a Loop over rows must recompute them.
- molsystem commits per operation inside a step, so a crash mid-step leaves its
  earlier writes committed. ``append_row`` and ``set_cell`` on a NULL current
  row are not idempotent on re-entry: part of phase 5's audit.
- ``check_table_plugins`` runs once, at job start, in ``exec_flowchart``.

Corpus (2026-10-03)
~~~~~~~~~~~~~~~~~~~

85 of the local and Dropbox flowcharts use tables (``~/SEAMM/flowcharts`` and
Dropbox ``Science/Flowcharts``, ``Science/Thermochemistry/flowcharts``,
``GM/flowcharts``, ``GM/TrainingData``). Most run LAMMPS, VASP, ORCA, Gaussian
or DFTB+ for minutes to hours or need input files, so the A/B ran the cheap
MOPAC ones, copied into ``Testing/phase4`` as ``dbx_*.flow``:

- ``MOPAC SMILES table`` (``C CC O --hamiltonians PM6 PM7``: append in a loop,
  a nested loop storing into a column named by ``$H``, Save inside the loop,
  Print the current row) and ``MOPAC SMILES table new`` (Read a CSV, loop over
  rows with ``$_row["SMILES"]``, Save as): **identical** in A and B, tables,
  printed rows and every other output.
- ``geometry`` fails identically in A and B before any table (Read Structure:
  ``'PosixPath' object has no attribute 'format'`` when the system name is the
  ``file`` parameter). Not phase 4; Geometry Analysis tables are covered by
  ``p4_props_geom``.

Both SMILES flowcharts first failed identically in A and B, also not phase 4:
their MOPAC Optimization has ``structure handling: be put in a new
configuration``, an old spelling that ``structure_handling_description`` no
longer accepts, so every iteration raised. The copies were changed to "Create a
new configuration" to exercise the tables; the Dropbox originals are untouched.
Both pre-existing problems are filed, not fixed in phase 4: `seamm#220
<https://github.com/molssi-seamm/seamm/issues/220>`_ (the ``Path`` system name)
and `seamm#221 <https://github.com/molssi-seamm/seamm/issues/221>`_ (the legacy
structure-handling spellings).

The remaining table flowcharts are for the soak in SEAMM_DEV.

Soak (2026-10-03)
~~~~~~~~~~~~~~~~~

Paul's decision: soak in ``~/SEAMM_DEV`` before the PRs. At his request it was
switched to ``venvs/phase4-B`` (after adding the ``seamm_bsse``,
``seamm_packaging`` and ``xnn_step`` editables it lacked), and the JobServer and
web UI were restarted from it at 12:49. The soak set, its README (switch and
rollback, recipes, a results table) and the runs are in
``~/Work/SEAMM/Testing/phase4/soak/``. Results so far:

- **Timing** (300 iterations of a loop over rows, each a MOPAC energy storing
  four columns and saving the table): A 357.6 s (1.19 s/iteration), B 343.7 s
  (1.15 s/iteration). Tables in the database and the commit after each step
  cost nothing measurable; the CSVs are byte-identical.
- **Kill test:** ``kill -9`` of the evaluator during iteration 41 left exactly
  40 complete rows, the current row NULL (the next write appends iteration 41)
  and a consistent journal.
- **Read-only:** reading, looping and exporting work; creating a table stops
  with the ``PermissionError``.
- Loops over rows with a string index column, Read CSV/xlsx, Add columns,
  Get/Set element by index value, row number and column number, nested loops
  writing two tables, Properties and Geometry Analysis: all as expected;
  Geometry Analysis and summary CSVs byte-identical to A.
- **Found and fixed:** "Set element" of the current row past the end of a table
  raised; it now appends the row, as ``store_results`` does (table_step
  585dd9b).
- **Found, not phase 4:** the Properties step exports configuration property
  values as ``{sid, cid, value}`` dicts (A writes their Python repr, B JSON).
- Corpus copies: "MOPAC from SMILES" and "MOPAC over database" work; the three
  SMILES-table flowcharts fail every iteration on seamm#221, as released.

Still to do: soak in SEAMM_DEV,
then the release (molsystem, seamm, seamm_exec, the four steps; minimum
versions in ``table_plugins`` and the pins set then).
