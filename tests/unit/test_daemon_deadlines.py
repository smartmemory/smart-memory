"""Round-three diagnostics and absolute deadline regressions."""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module

import pytest
from click.testing import CliRunner
from tests.unit.test_daemon_lifecycle import _REAL_POPEN, _REAL_RUN
from tests.unit import test_daemon_lifecycle

from smartmemory_app import daemon
from smartmemory_app.cli import cli

launchd = test_daemon_lifecycle.launchd


SECRETS = [
    ("Incorrect API key provided: sk-proj-" + "A" * 48, "sk-proj-" + "A" * 48),
    ("Groq authentication failed for gsk_" + "B" * 52, "gsk_" + "B" * 52),
    ("Authentication failed: sk-" + "C" * 48, "sk-" + "C" * 48),
    ("Authentication failed: sk-ant-" + "D" * 48, "sk-ant-" + "D" * 48),
    ("Authorization: Bearer fixture-bearer", "fixture-bearer"),
    ("Authorization: Basic fixture-basic", "fixture-basic"),
    ("KEY=fixture-key", "fixture-key"),
    ("TOKEN=fixture-token", "fixture-token"),
    ('API key="fixture spaced secret"', "fixture spaced secret"),
    ("https://alice:fixture-password@example.com/path", "fixture-password"),
    ("https://fixture-userinfo@example.com/path", "fixture-userinfo"),
    ("https://example.com/?X-Signature=fixture-signature", "fixture-signature"),
]


@pytest.mark.parametrize("message,secret", SECRETS)
@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("boundary", ["tail", "stream", "exception", "health"])
def test_startup_render_boundaries(
    launchd, monkeypatch, message, secret, command, boundary
):
    path = daemon._data_dir() / "daemon.log"
    diagnostic = "AuthenticationError: " + message
    if boundary == "tail":
        launchd.fail_bootstrap = True
        path.write_text(diagnostic + "\n")
    elif boundary == "exception":

        def fail(**kwargs):
            raise RuntimeError(diagnostic)

        monkeypatch.setattr(
            import_module("smartmemory_app.cli"), "_start_with_progress", fail
        )
    elif boundary == "stream":
        original = launchd.start

        def start():
            original()
            with path.open("a") as log:
                log.write(diagnostic + "\n")

        monkeypatch.setattr(launchd, "start", start)
    else:
        original = daemon.get_status

        def status():
            result = original()
            if result is not None:
                result.update(status="degraded", degraded_reason=diagnostic)
            return result

        monkeypatch.setattr(daemon, "get_status", status)
        monkeypatch.setattr("smartmemory_app.work_graph.get_work_status", lambda: {})
    result = CliRunner().invoke(cli, [command])
    assert "AuthenticationError" in result.output, result.output
    assert secret not in result.output
    if boundary in ("tail", "exception"):
        assert result.exit_code != 0
        assert str(path) in result.output
    else:
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", ["stop", "restart", "start"])
@pytest.mark.parametrize("slow_request", [1, 2, 4])
def test_trickling_health_is_cancelled(launchd, monkeypatch, command, slow_request):
    body = json.dumps(
        {"service": "smartmemory", "status": "warming", "pid": 424242}
    ).encode()
    disconnected = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            slow = len(calls) >= slow_request
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for start in range(0, len(body), 2):
                    self.wfile.write(body[start : start + 2])
                    self.wfile.flush()
                    if slow:
                        time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(server.server_port))
    daemon._pid_file().write_text("424242")
    monkeypatch.setattr(daemon.os, "kill", lambda *args: None)
    started = time.monotonic()
    try:
        with daemon.lifecycle_budget(0.3):
            result = CliRunner().invoke(
                cli, [command, "--wait"] if command == "start" else [command]
            )
        elapsed = time.monotonic() - started
        assert result.exit_code != 0, result.output
        assert elapsed < 0.8, (elapsed, result.output)
        assert daemon._pid_file().exists()
        assert "bootstrap" not in launchd.events
        assert len(calls) >= slow_request
        assert disconnected.wait(0.3), "HTTP connection was not closed on cancellation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("command", ["stop", "restart"])
@pytest.mark.parametrize("blocked_inspection", [1, 2])
def test_core_identity_wait_is_bounded(
    launchd, monkeypatch, caplog, command, blocked_inspection
):
    from smartmemory.pipeline.work_graph import spawn

    daemon._pid_file().write_text("424242")
    (daemon._data_dir() / ".worker.pid").write_text("434343")
    monkeypatch.setattr(spawn, "worker_is_running", lambda data: True)
    signals = []
    original_kill = daemon.os.kill

    def kill(pid, sig):
        if pid in (424242, 434343):
            signals.append((pid, sig))
        else:
            original_kill(pid, sig)  # Allow subprocess.run to kill/reap the probe.

    monkeypatch.setattr(daemon.os, "kill", kill)
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    calls = []

    def run(command, **kwargs):
        if command[0] == "ps":
            calls.append(kwargs)
            if len(calls) >= blocked_inspection:
                return _REAL_RUN(
                    [sys.executable, "-c", "import time; time.sleep(1)"], **kwargs
                )
            return subprocess.CompletedProcess(
                command, 0, "python -m smartmemory_app.worker_entry", ""
            )
        return launchd.run(command, **kwargs)

    original_stop = spawn.stop_worker
    original_identity = spawn._is_worker_process
    monkeypatch.setattr(subprocess, "run", run)
    started = time.monotonic()
    with daemon.lifecycle_budget(0.3):
        result = CliRunner().invoke(cli, [command])
    assert time.monotonic() - started < 0.8
    assert result.exit_code != 0
    assert calls and all(0 < call.get("timeout", 0) <= 0.3 for call in calls)
    assert "WARNING" in caplog.text and "identity" in caplog.text.lower()
    assert daemon._pid_file().exists()
    assert "bootstrap" not in launchd.events
    if blocked_inspection == 1:
        assert all(sig == 0 for _, sig in signals)
    assert spawn.stop_worker is original_stop
    assert spawn._is_worker_process is original_identity
    assert subprocess.run is run
