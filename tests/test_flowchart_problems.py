#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tests of the check of the whole flowchart before a job runs."""

import seamm.builder

from seamm_exec.exec_flowchart import flowchart_problems


def test_problems_are_returned(monkeypatch):
    monkeypatch.setattr(seamm.builder, "validate", lambda flowchart: ["bad order"])
    assert flowchart_problems(object()) == ["bad order"]


def test_no_problems(monkeypatch):
    monkeypatch.setattr(seamm.builder, "validate", lambda flowchart: [])
    assert flowchart_problems(object()) == []


def test_a_failing_check_does_not_stop_the_job(monkeypatch):
    def broken(flowchart):
        raise ValueError("a bug in the check")

    monkeypatch.setattr(seamm.builder, "validate", broken)
    assert flowchart_problems(object()) == []
