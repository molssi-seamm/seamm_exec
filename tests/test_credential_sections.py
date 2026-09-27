# -*- coding: utf-8 -*-
"""Datastore credentials are looked up by installation, then as before."""

import platform

from seamm_exec.exec_flowchart import credential_sections


def test_default_installation():
    assert credential_sections("~/SEAMM") == ["SEAMM", "localhost", platform.node()]


def test_development_installation_keeps_legacy_dev_section():
    assert credential_sections("~/SEAMM_DEV") == ["SEAMM_DEV", "dev", platform.node()]


def test_other_installation():
    assert credential_sections("/data/SEAMM_NEW")[:2] == ["SEAMM_NEW", "localhost"]
