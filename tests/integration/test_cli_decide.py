"""Exercise the decision CLI against an offline, disposable Lite daemon."""

# === 1. IMPORTS ===
import json
import os
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app.cli import cli
from tests.real_use.lite_week import child_env


# === 2. TESTS ===
def test_decide_then_why(lite_daemon):
    """Read the stored reason and both alternatives through the actual CLI transport."""
    runner = CliRunner()
    text = "Weekly liver tests for all sites"
    reason = "two grade-3 ALT elevations"
    first, second = "wait for IRB: too slow", "monthly tests"
    created = runner.invoke(
        cli,
        [
            "decide",
            text,
            "--why",
            reason,
            "--ruled-out",
            first,
            "--ruled-out",
            second,
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    decision_id = json.loads(created.stdout)["decision_id"]
    assert isinstance(decision_id, str) and decision_id

    read = runner.invoke(cli, ["why", "liver tests"])
    assert read.exit_code == 0, read.output
    assert f"[decision] {text} ({decision_id[:8]})" in read.stdout
    assert f"rationale: {reason}" in read.stdout
    assert f"ruled out: {first}\n  ruled out: {second}" in read.stdout

    other = runner.invoke(cli, ["decide", "Keep SQLite", "--why", "Offline use"])
    assert other.exit_code == 0, other.output
    assert other.stdout.startswith("Recorded decision ")
    assert other.stdout.removeprefix("Recorded decision ").strip() not in (
        "",
        decision_id,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["", "--why", "reason"],
        ["   ", "--why", "reason"],
        ["choice", "--why", " "],
        ["choice", "--why", "reason", "--ruled-out", " "],
        ["choice"],
    ],
)
def test_decide_refuses_incomplete_input(args):
    """Click reports an invalid invocation without needing a backend."""
    result = CliRunner().invoke(cli, ["decide", *args])
    assert result.exit_code == 2
    assert (
        "must contain text" in result.output
        or "Missing option '--why'" in result.output
    )


def test_decide_help():
    """Help documents repeatability and the scope of a write."""
    result = CliRunner().invoke(cli, ["decide", "--help"])
    assert result.exit_code == 0
    help_text = " ".join(result.stdout.split())
    assert "Repeat for each option" in help_text
    assert "Replacing a decision is outside this command's scope" in help_text


# === 3. FIXTURES ===
@pytest.fixture
def lite_daemon(cli_test_port, tmp_path, monkeypatch):
    """Run the mounted daemon app on an isolated port and remove its store on exit."""
    port = os.environ.get("SM_DECIDE_TEST_PORT", str(cli_test_port))
    if "SM_DECIDE_TEST_PORT" in os.environ:
        assert 19000 <= int(port) <= 19999, "use a reserved test port"
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", port)
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    with TemporaryDirectory(prefix="cli-decide-", dir=tmp_path) as directory:
        work = Path(directory)
        guard_log = work / "network.log"
        env = child_env(work, work / "store", guard_log)
        env.update(
            SMARTMEMORY_DAEMON_PORT=port,
            SMARTMEMORY_MODE="local",
            SMARTMEMORY_NO_UPDATE_CHECK="1",
            SMARTMEMORY_DISABLE_LAUNCH_METRICS="1",
        )
        argv = [
            sys.executable,
            "-m",
            "tests.real_use._net_guard",
            str(guard_log),
            "uvicorn",
            "smartmemory_app.viewer_server:_build_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            port,
        ]
        with subprocess.Popen(argv, cwd=work, env=env) as server:
            try:
                _wait_for_daemon(server, port)
                yield
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)


def _wait_for_daemon(server, port):
    """Wait for HTTP readiness and surface premature process exits."""
    deadline = time.monotonic() + 45
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            assert server.poll() is None, "Lite daemon exited before startup"
            try:
                response = client.get(f"http://127.0.0.1:{port}/openapi.json")
                response.raise_for_status()
                return
            except httpx.ConnectError:
                time.sleep(0.1)
    pytest.fail("Lite daemon did not start in 45 seconds")
