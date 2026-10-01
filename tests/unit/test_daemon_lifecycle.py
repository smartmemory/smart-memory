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
