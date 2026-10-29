"""CLI-SETUP-PORT-IN-USE-FALSE-POSITIVE-1: our own slow-starting daemon is never "another program"."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from smartmemory_app import daemon, daemon_diagnosis as dd
from smartmemory_app import port_probe

OWN, FOREIGN_PID = 4242, 777

# holder pid, marker pid (None = no daemon.pid), marker alive, holder command line is the
# daemon's, daemon.pid age in seconds, health reply -> (klass, word in detail, word NOT in detail)
ROWS = [
    ("holder is own pid, young", OWN, OWN, True, False, 68, None, "starting", "still starting (68s)", "another program"),
    ("holder is own pid, past deadline", OWN, OWN, True, False, 400, None, "unresponsive", "not ready yet", "another program"),
    ("holder unnamed, own daemon alive", None, OWN, True, False, 30, None, "starting", "still starting", "another program"),
    ("holder foreign pid", FOREIGN_PID, OWN, True, False, 30, None, "port_in_use", "another program (process 777)", "still starting"),
    ("holder foreign, no pidfile", FOREIGN_PID, None, False, False, None, None, "port_in_use", "another program", "still starting"),
    ("holder is daemon by cmdline, no pidfile", FOREIGN_PID, None, False, True, None, None, "starting", "still starting", "another program"),
    ("no holder, nothing listening", None, None, False, False, None, None, "not_running", "not running", "another program"),
    ("own daemon ready before deadline", OWN, OWN, True, False, 68, {"status": "ok"}, "ok", "daemon running", "another program"),
]


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_port_holder_rows(row, tmp_path, monkeypatch):
    _, holder, marker_pid, alive, cmd_is_daemon, age, health, klass, word, absent = row
    pidfile = tmp_path / "daemon.pid"
    if marker_pid is not None:
        pidfile.write_text(str(marker_pid))
        stamp = time.time() - age
        os.utime(pidfile, (stamp, stamp))
    listening = holder is not None or marker_pid is not None or cmd_is_daemon
    monkeypatch.setattr(daemon, "_port", lambda: 9099)
    monkeypatch.setattr(daemon, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon, "_pid_file", lambda: pidfile)
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: alive)
    monkeypatch.setattr(daemon, "get_status", lambda: health)
    monkeypatch.setattr(dd.socket.socket, "connect_ex", lambda self, addr: 0 if listening else 1)
    monkeypatch.setattr(port_probe, "port_holder", lambda port: (holder, "python3"))
    monkeypatch.setattr(port_probe, "_is_smartmemory_process", lambda pid: cmd_is_daemon and pid == holder)
    monkeypatch.setattr(
        "smartmemory.utils.process.process_cmdline",
        lambda pid: ["python", "-m", "smartmemory_app.viewer_server"] if pid == marker_pid else None,
    )
    monkeypatch.setattr(dd, "_plist_python", lambda: None)

    got = dd.diagnose(dd.collect_facts())

    assert got.klass == klass
    assert word in got.detail
    assert absent not in got.detail


# method -> (word that must be in the timeout message, word that must not be)
TIMEOUT_ROWS = [
    ("POST", "may still have been saved", "not responding"),
    ("GET", "not responding", "may still have been saved"),
]


@pytest.mark.parametrize("row", TIMEOUT_ROWS, ids=[r[0] for r in TIMEOUT_ROWS])
def test_client_timeout_message_rows(row, monkeypatch):
    import click
    import httpx

    from smartmemory_app import cli

    method, word, absent = row

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(httpx, "Client", lambda **kw: Client())
    monkeypatch.setattr(cli._scope_header, "print_scope_header", lambda **kw: None)
    with pytest.raises(click.ClickException) as raised:
        cli._daemon_request(method, "/memory/ingest")
    assert word in raised.value.message
    assert absent not in raised.value.message
