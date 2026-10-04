"""Pytest configuration for smartmemory tests."""

import os
from pathlib import Path

import pytest

# Never send automatic reports during tests, including import/collection failures.
os.environ["SMARTMEMORY_CRASH_REPORTS"] = "0"
# Keep existing model files available without restoring access to the user's HOME.
_REAL_HF_HOME = os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))


def pytest_collection_modifyitems(items):
    """Auto-mark tests in tests/integration/ with the integration marker."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def isolated_support_diagnostics(monkeypatch, tmp_path):
    """Never write user logs or make doctor probes against a live network."""
    if not os.environ.get("HF_HOME"):
        monkeypatch.setenv("HF_HOME", _REAL_HF_HOME)
    if not os.environ.get("HF_HUB_CACHE"):
        monkeypatch.setenv("HF_HUB_CACHE", str(Path(os.environ["HF_HOME"]) / "hub"))
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "appdata"))
    from smartmemory_app import (
        bug_report,
        support_diagnostics,
        install_check,
        diagnostic_process,
        crash_reporter,
        hook_failures,
    )

    monkeypatch.setattr(
        bug_report, "debug_log_path", lambda: tmp_path / "cli-debug.log"
    )
    monkeypatch.setattr(
        crash_reporter, "debug_log_path", lambda: tmp_path / "cli-debug.log"
    )
    monkeypatch.setattr(
        hook_failures, "marker_directory", lambda: tmp_path / "hook-markers"
    )
    monkeypatch.setattr(
        support_diagnostics, "_probe", lambda host: "reachable (1 ms, HTTP 200)"
    )
    monkeypatch.setattr(
        support_diagnostics, "_daemon_status", lambda: "Daemon: stopped"
    )

    # These CLI tests isolate diagnostics unrelated to their command. Dedicated
    # install/store suites and native TVPC drivers exercise the real probes.
    monkeypatch.setattr(
        install_check, "run_native_checks", lambda: ["Native libraries: OK"]
    )
    monkeypatch.setattr(diagnostic_process, "offline_checks", lambda task, **kwargs: [])

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


@pytest.fixture(scope="session", autouse=True)
def real_home_report_leak_gate():
    """Fail if a CLI or shell test adds reporting content to the real profile."""
    root = Path.home() / ".smartmemory"

    def snapshot():
        paths = [root / "hook-failures.tsv"]
        paths.extend((root / "report-outbox").rglob("*"))
        return {
            str(p): p.read_bytes() for p in paths if p.is_file() and p.stat().st_size
        }

    before = snapshot()
    yield
    gained = [p for p, content in snapshot().items() if before.get(p) != content]
    assert not gained, (
        "Tests leaked reporting content into the real home: " + ", ".join(gained)
    )
