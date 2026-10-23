"""Publish stable behavior identifiers in verifier output."""

from __future__ import annotations

import re


REPORTS = []
DOCSTRINGS = {}


def pytest_runtest_setup(item):
    function = getattr(item, "function", None)
    DOCSTRINGS[item.nodeid] = getattr(function, "__doc__", "") or ""


def pytest_runtest_logreport(report):
    if report.when in {"setup", "call", "teardown"} and (
        report.failed or report.when == "call"
    ):
        REPORTS.append(report)


def case_id_from_docstring(docstring: str, fallback: str) -> str:
    match = re.search(r"case_id:\s*([A-Z][A-Z0-9_-]*-[0-9]{2,})", docstring)
    return match.group(1) if match else fallback


def pytest_sessionfinish(session, exitstatus):
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    case_status = {}
    for report in REPORTS:
        docstring = DOCSTRINGS.get(report.nodeid, "")
        fallback = re.sub(r"[^A-Z0-9_]+", "-", report.nodeid.upper()).strip("-")
        case_id = case_id_from_docstring(docstring, fallback)
        passed = report.when == "call" and report.passed
        case_status[case_id] = case_status.get(case_id, True) and passed
    if terminal is not None:
        for case_id in sorted(case_status):
            verdict = "PASSED" if case_status[case_id] else "FAILED"
            terminal.write_line(f"OBSERVED_CASE {case_id} {verdict}")
