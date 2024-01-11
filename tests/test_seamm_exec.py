"""
Unit and regression test for the seamm_exec package.
"""

# Import package, test suite, and other packages as needed
import sys

import pytest  # noqa: F401

import seamm_exec  # noqa: F401


def test_seamm_exec_imported():
    """Sample test, will always pass so long as import statement worked."""
    assert "seamm_exec" in sys.modules
