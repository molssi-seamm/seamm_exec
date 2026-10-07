# -*- coding: utf-8 -*-
"""Shared test fixtures."""

import gc

import pytest
import seamm


@pytest.fixture(autouse=True)
def _close_system_databases():
    """Close the SystemDB a flowchart run left behind, in the main thread.

    Each ExecFlowchart replaces seamm.flowchart_variables, orphaning the previous
    run's database. Freed later by the cyclic garbage collector in whatever thread
    happened to trigger it -- a TaskSet's, say -- its __del__ could not close the
    SQLite connection ("SQLite objects created in a thread can only be used in
    that same thread"), which pytest reported as an unraisable exception.
    """
    yield
    variables = seamm.flowchart_variables
    if variables is not None and variables.exists("_system_db"):
        db = variables.get_variable("_system_db")
        variables.delete("_system_db")
        try:
            db.close()
        except Exception:
            pass
    gc.collect()
