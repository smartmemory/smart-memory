"""Launchd lifecycle regressions at the subprocess boundary, with real HTTP."""

import json
import platform
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from smartmemory_app import daemon
from smartmemory_app.cli import cli

_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    platform.platform()  # Cache stdlib platform probing before mocking subprocess.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    monkeypatch.setattr(daemon, "_LAUNCHD_DAEMON_LABEL", "ai.smartmemory.test.daemon")
    monkeypatch.setattr(daemon, "_LAUNCHD_WORKER_LABEL", "ai.smartmemory.test.worker")
    data = daemon._data_dir()
    data.mkdir(parents=True)
    plist = daemon._launchd_plist_path(daemon._LAUNCHD_DAEMON_LABEL)
    plist.parent.mkdir(parents=True)
    plist.write_text("test plist; consumed only by the subprocess boundary")

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"service": "smartmemory", "status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Launchd:
        loaded = False
        pending = 0
        stuck = False
        fail_bootout = False
        fail_bootstrap = False
        server = None
        port = 0

        def __init__(self):
            self.events = []
            server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
            self.port = server.server_port
            server.server_close()

        def start(self):
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Health)
            self.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.thread.start()
            self.loaded = True
            daemon._pid_file().write_text("424242")

        def close(self):
            if self.server:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join()
                self.server = None

        def run(self, command, **kwargs):
            assert command[0] == "launchctl", command
            action = command[1]
            self.events.append(action)
            rc, out, err = 0, "", ""
            if action == "print":
                if command[-1].endswith(daemon._LAUNCHD_WORKER_LABEL):
                    rc, err = 113, "Could not find service"
                else:
                    if self.pending and not self.stuck:
                        self.pending -= 1
                        if not self.pending:
                            self.loaded = False
                            self.events.append("removed")
                    if self.loaded:
                        out = "state = SIGTERMed" if self.pending else "state = running"
                    else:
                        rc, err = 113, "Could not find service"
            elif action in ("bootout", "unload"):
                if self.fail_bootout:
                    rc, err = 5, "bootout denied"
                else:
                    self.close()  # HTTP disappears BEFORE launchd removes the job.
                    self.pending = 3
            elif action in ("bootstrap", "load"):
                assert not self.loaded, "bootstrap raced the terminating job"
                if self.fail_bootstrap:
                    rc, err = 5, "test startup failure"
                else:
                    self.start()
            else:
                pytest.fail(f"Unexpected launchctl action: {command}")
            return subprocess.CompletedProcess(command, rc, out, err)

    state = Launchd()
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(state.port))
    monkeypatch.setattr(subprocess, "run", state.run)

    def no_popen(*args, **kwargs):
        pytest.fail("Direct subprocess spawn must never compete with launchd")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    yield state
    state.close()


@pytest.mark.parametrize(
    "plist,loaded,pid,expected",
    [
        (True, False, False, False),  # deliberately stopped
        (True, True, False, True),  # crashed / KeepAlive throttle gap
        (False, False, False, False),
        (False, False, True, True),  # unmanaged crash marker
    ],
)
def test_expected_running(launchd, plist, loaded, pid, expected):
    if not plist:
        daemon._launchd_plist_path(daemon._LAUNCHD_DAEMON_LABEL).unlink()
    launchd.loaded = loaded
    if pid:
        daemon._pid_file().write_text("424242")
    assert daemon.should_be_running() is expected


@pytest.mark.parametrize("command", ["restart", "start-after-stop"])
def test_restart_waits_for_job_removal_before_bootstrap(launchd, command):
    launchd.start()
    if command == "restart":
        result = CliRunner().invoke(cli, ["restart"])
    else:
        result = CliRunner().invoke(cli, ["stop"])
        assert result.exit_code == 0, result.output
        assert not daemon.should_be_running()
        result = CliRunner().invoke(cli, ["start", "--wait"])
    assert result.exit_code == 0, result.output
    assert "SmartMemory is ready" in result.output
    assert launchd.events.index("removed") < launchd.events.index("bootstrap")
    assert launchd.events.count("bootstrap") == 1
    assert daemon.is_running()


def test_stop_unresponsive_loaded_job(launchd):
    launchd.loaded = True  # no HTTP, e.g. crashed job awaiting KeepAlive
    result = CliRunner().invoke(cli, ["stop"])
    assert result.exit_code == 0, result.output
    assert "bootout" in launchd.events
    assert not launchd.loaded
    assert not daemon.should_be_running()


def test_restart_unresponsive_loaded_job(launchd):
    launchd.loaded = True
    result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code == 0, result.output
    assert launchd.events.index("removed") < launchd.events.index("bootstrap")


def test_stop_failure_does_not_claim_success(launchd):
    launchd.start()
    launchd.fail_bootout = True
    result = CliRunner().invoke(cli, ["stop"])
    assert result.exit_code != 0
    assert "Daemon stopped" not in result.output
    assert "bootout" in result.output
    assert daemon._pid_file().exists()


@pytest.mark.parametrize("command", ["start", "restart"])
def test_startup_error_reaches_cli_with_log_path(launchd, command):
    launchd.fail_bootstrap = True
    log_path = daemon._data_dir() / "daemon.log"
    log_path.write_text("RuntimeError: underlying startup exception\n")
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code != 0
    assert "test startup failure" in result.output
    assert "RuntimeError: underlying startup exception" in result.output
    assert str(log_path) in result.output


def test_unmanaged_stop_is_idempotent(launchd):
    daemon._launchd_plist_path(daemon._LAUNCHD_DAEMON_LABEL).unlink()
    daemon.stop_daemon()
    assert not daemon.should_be_running()
    assert "bootout" not in launchd.events


def test_stop_timeout_does_not_clear_pid_or_start_replacement(launchd):
    launchd.start()
    launchd.stuck = True
    result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code != 0
    assert "shutdown did not complete" in result.output
    assert daemon._pid_file().exists()
    assert "bootstrap" not in launchd.events


@pytest.mark.parametrize("error", [None, OSError("launchctl unavailable")])
def test_inspection_failure_is_not_treated_as_job_removal(launchd, monkeypatch, error):
    def failed(*args, **kwargs):
        if error:
            raise error
        return subprocess.CompletedProcess(args[0], 5, "", "inspection denied")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises((OSError, RuntimeError), match="unavailable|inspection denied"):
        daemon._launchd_loaded(daemon._LAUNCHD_DAEMON_LABEL)


@pytest.mark.parametrize("command", ["start", "restart"])
def test_failure_redacts_credentials_preserving_path(launchd, command):
    launchd.fail_bootstrap = True
    path = daemon._data_dir() / "daemon.log"
    path.write_text(
        "RuntimeError: Authorization: Bearer fixture-bearer\n"
        "api_key=fixture-key\n"
        "https://alice:fixture-password@example.com/api?token=fixture-token&x=ok\n"
        '{"api_key": "fixture-json-key"}\n'
        "https://fixture-userinfo@example.com/api\n"
    )
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code != 0
    for secret in (
        "fixture-bearer",
        "fixture-key",
        "fixture-password",
        "fixture-token",
        "fixture-json-key",
        "fixture-userinfo",
    ):
        assert secret not in result.output
    assert "RuntimeError" in result.output
    assert str(path) in result.output


@pytest.mark.parametrize(
    "helper",
    [
        "_launchd_loaded",
        "_launchd_bootout",
        "_launchd_job_summary",
        "_launchd_bootstrap",
    ],
)
def test_launchctl_boundaries_have_timeouts(launchd, monkeypatch, helper):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "state = running", "")

    monkeypatch.setattr(subprocess, "run", run)
    getattr(daemon, helper)(daemon._LAUNCHD_DAEMON_LABEL)
    assert calls and all(0 < call.get("timeout", 0) <= 5 for call in calls)


def test_worker_disappears_after_second_inspection(launchd, monkeypatch):
    daemon._launchd_plist_path(daemon._LAUNCHD_WORKER_LABEL).write_text(
        "smartmemory_app.worker_entry"
    )
    launchd.start()
    bootouts = 0

    def run(command, **kwargs):
        nonlocal bootouts
        if daemon._LAUNCHD_WORKER_LABEL in command[-1]:
            if command[1] == "print":
                rc = 0 if bootouts < 2 else 113
                return subprocess.CompletedProcess(
                    command,
                    rc,
                    "state = SIGTERMed",
                    "Could not find service" if rc else "",
                )
            if command[1] == "bootout":
                bootouts += 1
                if bootouts == 1:
                    return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] in ("bootstrap", "load"):
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(
                command, 113, "", "Could not find service"
            )
        return launchd.run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code == 0, result.output
    assert "bootstrap" in launchd.events
    assert bootouts == 2


@pytest.mark.parametrize("command", ["stop", "restart"])
def test_unmanaged_missing_gui_domain_uses_pid_path(
    launchd, monkeypatch, caplog, command
):
    daemon._launchd_plist_path(daemon._LAUNCHD_DAEMON_LABEL).unlink()
    daemon._pid_file().write_text("424242")
    signals = []

    def run(command, **kwargs):
        if command[0] == "launchctl":
            return subprocess.CompletedProcess(
                command, 112, "", "Could not find domain for user gui: 501"
            )
        assert command[0] == "ps"
        return subprocess.CompletedProcess(
            command, 0, "smartmemory_app.viewer_server", ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    if command == "restart":

        class Child:
            def poll(self):
                return None

        def popen(*args, **kwargs):
            assert signals
            launchd.start()
            return Child()

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(daemon, "_start_workers", lambda *args: None)
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 0, result.output
    assert signals
    assert "WARNING" in caplog.text and "Skipping launchd" in caplog.text
    assert "gui" in caplog.text.lower()


def test_slow_health_shares_shutdown_deadline(launchd, monkeypatch):
    import time
    import httpx
    from smartmemory_app.daemon import lifecycle_budget

    launchd.start()
    # Job disappears immediately, but health remains responsive and slow.
    launchd.close()

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 113, "", "Could not find service")

    async def get(self, url, **kwargs):
        time.sleep(min(0.06, kwargs["timeout"]))
        return httpx.Response(200, json={"service": "smartmemory", "pid": 424242})

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(daemon.os, "kill", lambda *args: None)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline|shutdown"):
        with lifecycle_budget(0.2):
            daemon.stop_daemon()
    assert time.monotonic() - started < 0.6
    assert daemon._pid_file().exists()


@pytest.mark.parametrize("action", ["print", "bootout"])
def test_blocked_launchctl_is_killed_by_shared_deadline(launchd, monkeypatch, action):
    import sys
    import time
    from smartmemory_app.daemon import lifecycle_budget

    launchd.loaded = True
    daemon._pid_file().write_text("424242")
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)

    def run(command, **kwargs):
        if command[1] == action:
            return _REAL_RUN(
                [sys.executable, "-c", "import time; time.sleep(5)"], **kwargs
            )
        return launchd.run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    started = time.monotonic()
    with lifecycle_budget(0.2):
        result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code != 0
    assert "timed out" in result.output or "deadline expired" in result.output
    assert time.monotonic() - started < 0.8
    assert daemon._pid_file().exists()
    assert "bootstrap" not in launchd.events


@pytest.mark.parametrize("command", ["start", "restart"])
def test_cli_redacts_direct_exception(launchd, monkeypatch, command):
    from importlib import import_module

    def fail(**kwargs):
        raise RuntimeError(
            'API key="fixture spaced secret" https://example.com/?X-Signature=fixture-signature Authorization: Basic fixture-basic'
        )

    monkeypatch.setattr(
        import_module("smartmemory_app.cli"), "_start_with_progress", fail
    )
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code != 0
    assert "fixture" not in result.output
    assert "RuntimeError" in result.output
    assert str(daemon._data_dir() / "daemon.log") in result.output


def test_restart_deadline_includes_startup_health_waits(launchd, monkeypatch):
    import httpx

    launchd.start()
    (daemon._data_dir() / "daemon.log").write_text(
        "RuntimeError: startup deadline probe\n"
    )
    now = [0.0]
    original_get = httpx.AsyncClient.get

    async def get(client, url, **kwargs):
        response = await original_get(client, url, **kwargs)
        now[0] += 0.4
        if "bootstrap" in launchd.events:
            return httpx.Response(
                200, json={"service": "smartmemory", "status": "warming"}
            )
        return response

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        daemon.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    result = CliRunner().invoke(cli, ["restart"])
    assert result.exit_code != 0
    assert "deadline expired" in result.output
    assert "RuntimeError: startup deadline probe" in result.output
    assert now[0] <= 75.4
    assert "bootstrap" in launchd.events
    assert "SmartMemory is ready" not in result.output
