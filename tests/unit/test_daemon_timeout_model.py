"""Lifecycle budget, escalation and diagnostic-path regression tests."""

import json
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from smartmemory_app import daemon
from smartmemory_app.cli import cli
from smartmemory_app.diagnostics import redact_credentials
from tests.unit import test_daemon_lifecycle
from tests.unit.test_daemon_deadlines import SECRETS

launchd = test_daemon_lifecycle.launchd
sysconfig.get_config_vars()


@pytest.mark.parametrize("command", ["stop", "restart"])
def test_stalled_health_still_retires_launchd_job(launchd, monkeypatch, command):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if len(calls) == 1:
                time.sleep(2.3)
            body = json.dumps({"service": "other"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(server.server_port))
    launchd.loaded = True
    daemon._pid_file().write_text("424242")
    monkeypatch.setattr(
        "smartmemory_app.cli._start_with_progress",
        lambda **kwargs: {"service": "smartmemory", "status": "ok"},
    )
    started = time.monotonic()
    try:
        with daemon.lifecycle_budget(4):
            result = CliRunner().invoke(cli, [command])
        print(
            "stalled health",
            command,
            "elapsed",
            time.monotonic() - started,
            "events",
            launchd.events,
            "output",
            repr(result.output),
        )
        assert result.exit_code == 0, result.output
        assert "bootout" in launchd.events
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_uncooperative_worker_reaches_core_escalation(launchd, monkeypatch, platform):
    from smartmemory.pipeline.work_graph import spawn

    monkeypatch.setattr(daemon.sys, "platform", platform)
    (daemon._data_dir() / ".worker.pid").write_text("434343")
    monkeypatch.setattr(spawn, "worker_is_running", lambda data: True)
    real_kill = daemon.os.kill
    signals = []

    def kill(pid, sig):
        if pid == 434343:
            signals.append(sig)
        else:
            return real_kill(pid, sig)

    def run(cmd, **kwargs):
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(
                cmd, 0, "python -m smartmemory_app.worker_entry", ""
            )
        return launchd.run(cmd, **kwargs)

    monkeypatch.setattr(daemon.os, "kill", kill)
    monkeypatch.setattr(subprocess, "run", run)
    started = time.monotonic()
    try:
        with daemon.lifecycle_budget(0.25):
            daemon._stop_workers()
    except TimeoutError as exc:
        print(
            "worker expiry",
            platform,
            "elapsed",
            time.monotonic() - started,
            "signals",
            signals,
            "exception",
            str(exc),
        )
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals, "Deadline-aware sleep prevents core escalation"


@pytest.mark.parametrize(
    "path",
    [
        "/Users/monkey Smith/.smartmemory/daemon.log",
        "/private/tmp/sk-project/data/daemon.log",
        "/private/tmp/secret folder/data/daemon.log",
    ],
)
def test_diagnostic_file_paths_are_preserved(path):
    output = redact_credentials("Failure at " + path)
    print("path", repr(path), "output", repr(output))
    assert output == "Failure at " + path


@pytest.mark.parametrize(
    "message,secret", SECRETS + [("SECRET=fixture-secret", "fixture-secret")]
)
def test_precise_credentials_preserve_adjacent_paths(message, secret):
    paths = '/private/tmp/sk-project/data/daemon.log and "/Users/monkey Smith/.smartmemory/daemon.log"'
    output = redact_credentials(message + " at " + paths)
    assert secret not in output
    assert paths in output
    assert redact_credentials(output) == output


@pytest.mark.parametrize(
    "component", ["sk-" + "A" * 48, "gsk_" + "B" * 52, "TOKEN=folder", "secret folder"]
)
def test_key_shaped_path_components_are_preserved(component):
    message = f"Failure at /private/tmp/{component}/daemon.log"
    assert redact_credentials(message) == message


def test_nested_lifecycle_calls_share_one_deadline():
    with daemon.lifecycle_budget(20):
        deadline = daemon._deadline.get()
        with daemon.lifecycle_budget(10):
            assert daemon._deadline.get() == deadline
        with daemon.lifecycle_budget(75):
            assert daemon._deadline.get() == deadline
    assert daemon._deadline.get() is None


@pytest.mark.parametrize("slow_identity", [False, True])
def test_real_sigterm_resistant_worker_exits_before_stop_returns(
    tmp_path, monkeypatch, slow_identity
):
    from smartmemory.pipeline.work_graph import spawn

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    # Exercise unmanaged shutdown on macOS without consulting real launchd jobs.
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    code = """from smartmemory.pipeline.work_graph.worker import run_worker
from filelock import FileLock
import os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
d = pathlib.Path(sys.argv[1])
with FileLock(str(d / '.worker.lock')):
    (d / '.worker.pid').write_text(str(os.getpid()))
    time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    real_run = subprocess.run
    inspections = []

    def run(command, **kwargs):
        assert command[0] == "ps"
        inspections.append(kwargs["timeout"])
        if slow_identity:
            time.sleep(min(0.03, kwargs["timeout"] / 2))
        return real_run(command, **kwargs)

    try:
        until = time.monotonic() + 15
        while not (tmp_path / ".worker.pid").exists() and time.monotonic() < until:
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.05)
        assert (tmp_path / ".worker.pid").exists()
        monkeypatch.setattr(subprocess, "run", run)
        started = time.monotonic()
        with daemon.lifecycle_budget(0.8):
            daemon._stop_workers()
        assert time.monotonic() - started < 0.8
        assert not spawn.worker_is_running(tmp_path), (
            "Stop returned before the worker released its lock"
        )
        assert process.wait(timeout=0.1) == -signal.SIGKILL
        assert len(inspections) >= 3
        assert all(0 < cap <= 0.08 for cap in inspections)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stderr.close()
