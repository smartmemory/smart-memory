"""Pytest configuration for smartmemory tests."""

import os

import pytest

# Never send automatic reports during tests, including import/collection failures.
os.environ["SMARTMEMORY_CRASH_REPORTS"] = "0"


def pytest_collection_modifyitems(items):
    """Auto-mark tests in tests/integration/ with the integration marker."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def isolated_support_diagnostics(monkeypatch, tmp_path):
    """Never write user logs or make doctor probes against a live network."""
    from smartmemory_app import bug_report, support_diagnostics

    monkeypatch.setattr(
        bug_report, "debug_log_path", lambda: tmp_path / "cli-debug.log"
    )
    monkeypatch.setattr(
        support_diagnostics, "_probe", lambda host: "reachable (1 ms, HTTP 200)"
    )
    monkeypatch.setattr(
        support_diagnostics, "_daemon_status", lambda: "Daemon: stopped"
    )

    # Detached-entry tests also install global handlers/hooks. Restore them per test.
    import logging
    import sys
    import threading

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    states = [(handler, handler.level, handler.formatter) for handler in handlers]
    process_hook, thread_hook = sys.excepthook, threading.excepthook
    stdout, stderr = sys.stdout, sys.stderr
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for handler, handler_level, formatter in states:
        handler.setLevel(handler_level)
        handler.setFormatter(formatter)
    sys.excepthook, threading.excepthook = process_hook, thread_hook
    sys.stdout, sys.stderr = stdout, stderr
