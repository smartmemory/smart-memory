"""Tests for the explicit ``sm update`` command."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from click.testing import CliRunner

import smartmemory_app
from smartmemory_app import cli as cli_module
from smartmemory_app import daemon, update_check


@pytest.fixture(autouse=True)
def isolated_update_check(tmp_path, monkeypatch):
    """Keep update checks off the network and out of the real data directory."""
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(update_check.NO_UPDATE_CHECK_ENV, raising=False)
    monkeypatch.setattr(cli_module, "_configure_cli_logging", lambda: None)
    monkeypatch.setattr(update_check, "trailing_notices", lambda *_args, **_kwargs: [])


def _result(returncode: int, stdout: str = "") -> SimpleNamespace:
    """Build the subprocess result fields used by the command."""
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _set_versions(monkeypatch, *, current: str, latest: str | None) -> None:
    """Set the current version and stub the existing PyPI HTTP boundary."""
    monkeypatch.setattr(smartmemory_app, "__version__", current)
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: latest)


def test_update_already_latest_does_not_run_pip(tmp_path, monkeypatch):
    """An up-to-date install exits successfully without invoking pip."""
    _set_versions(monkeypatch, current="1.4.110", latest="1.4.110")
    run = Mock(side_effect=AssertionError("subprocess should not run"))
    monkeypatch.setattr(cli_module.subprocess, "run", run)

    result = CliRunner().invoke(cli_module.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory 1.4.110 is already the latest version." in result.output
    run.assert_not_called()
    assert (
        json.loads((tmp_path / update_check.UPDATE_CACHE_FILENAME).read_text())[
            "latest"
        ]
        == "1.4.110"
    )


def test_update_available_runs_pip_and_restarts_running_daemon(tmp_path, monkeypatch):
    """A fresh PyPI result updates and restarts a daemon that was running."""
    cache = tmp_path / update_check.UPDATE_CACHE_FILENAME
    cache.write_text(json.dumps({"checked_at": 10**12, "latest": "1.4.110"}))
    _set_versions(monkeypatch, current="1.4.110", latest="1.4.111")

    run = Mock(side_effect=[_result(0), _result(0, "1.4.111\n")])
    monkeypatch.setattr(cli_module.subprocess, "run", run)
    monkeypatch.setattr(daemon, "get_status", Mock(return_value={"status": "ok"}))
    stop = Mock()
    monkeypatch.setattr(daemon, "stop_daemon", stop)
    start = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(cli_module, "_start_with_progress", start)

    result = CliRunner().invoke(cli_module.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory updated: 1.4.110 → 1.4.111." in result.output
    assert "SmartMemory is ready." in result.output
    assert run.call_args_list == [
        call(
            [sys.executable, "-m", "pip", "install", "-U", "smartmemory"],
            check=False,
        ),
        call(
            [
                sys.executable,
                "-c",
                "from importlib.metadata import version; print(version('smartmemory'))",
            ],
            capture_output=True,
            text=True,
            check=False,
        ),
    ]
    stop.assert_called_once_with()
    start.assert_called_once_with(
        num_workers=1,
        message="Starting SmartMemory",
        wait_until_ready=True,
    )


def test_update_pip_failure_exits_nonzero_without_restart(monkeypatch):
    """A failed pip process is visible and leaves the daemon untouched."""
    _set_versions(monkeypatch, current="1.4.110", latest="1.4.111")
    run = Mock(return_value=_result(7))
    monkeypatch.setattr(cli_module.subprocess, "run", run)
    monkeypatch.setattr(daemon, "get_status", Mock(return_value={"status": "ok"}))
    stop = Mock()
    monkeypatch.setattr(daemon, "stop_daemon", stop)
    start = Mock()
    monkeypatch.setattr(cli_module, "_start_with_progress", start)

    result = CliRunner().invoke(cli_module.cli, ["update"])

    assert result.exit_code != 0
    assert "SmartMemory update failed (pip exited with status 7)." in result.output
    assert "daemon was not restarted" in result.output
    run.assert_called_once_with(
        [sys.executable, "-m", "pip", "install", "-U", "smartmemory"],
        check=False,
    )
    stop.assert_not_called()
    start.assert_not_called()


def test_update_does_not_start_daemon_that_was_not_running(monkeypatch):
    """A successful update does not turn a stopped daemon into a running one."""
    _set_versions(monkeypatch, current="1.4.110", latest="1.4.111")
    run = Mock(side_effect=[_result(0), _result(0, "1.4.111\n")])
    monkeypatch.setattr(cli_module.subprocess, "run", run)
    monkeypatch.setattr(daemon, "get_status", Mock(return_value=None))
    stop = Mock()
    monkeypatch.setattr(daemon, "stop_daemon", stop)
    start = Mock()
    monkeypatch.setattr(cli_module, "_start_with_progress", start)

    result = CliRunner().invoke(cli_module.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory updated: 1.4.110 → 1.4.111." in result.output
    assert "Daemon was not running; it was not started." in result.output
    stop.assert_not_called()
    start.assert_not_called()


def test_update_check_failure_makes_no_changes(monkeypatch):
    """An unavailable latest version exits successfully without invoking pip."""
    _set_versions(monkeypatch, current="1.4.110", latest=None)
    run = Mock(side_effect=AssertionError("subprocess should not run"))
    monkeypatch.setattr(cli_module.subprocess, "run", run)

    result = CliRunner().invoke(cli_module.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "Could not check for SmartMemory updates" in result.output
    assert "No changes made." in result.output
    run.assert_not_called()
