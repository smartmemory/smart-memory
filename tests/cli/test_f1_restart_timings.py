"""Restart reports every phase while retaining strict lifecycle behavior."""

import re

from click.testing import CliRunner
from smartmemory_app import cli, daemon
from tests.unit.test_daemon_lifecycle import launchd

__all__ = ["launchd"]


def test_restart_phase_line_with_real_lifecycle_fixture(launchd):
    result = CliRunner().invoke(cli.cli, ["restart"])
    assert result.exit_code == 0, result.output
    line = next(
        line
        for line in result.output.splitlines()
        if line.startswith("Restart timings:")
    )
    for phase in ("stop worker", "stop daemon", "start/spawn", "ready"):
        assert re.search(re.escape(phase) + r"=\d+\.\d+s", line), line


def test_restart_failure_retains_partial_timings(launchd):
    launchd.fail_bootstrap = True
    result = CliRunner().invoke(cli.cli, ["restart"])
    assert result.exit_code != 0
    assert "Restart timings:" in result.output
    assert "stop daemon=" in result.output


def test_worker_measurement_is_per_restart(monkeypatch):
    monkeypatch.setattr(daemon, "_stop_core_worker", lambda: None)
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    with daemon.record_lifecycle_timings() as timings:
        daemon._stop_workers()
        assert timings["stop worker"] >= 0
    with daemon.record_lifecycle_timings() as next_timings:
        assert next_timings == {}
