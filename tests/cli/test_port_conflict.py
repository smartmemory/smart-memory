"""CLI-PORT-CONFLICT-1 + CLI-PORT-PROBE-NO-LSOF-1: a held daemon port is found, named and avoided.

Real loopback listeners on OS-assigned ports (never 9014), real /health answers.
One row table: port free | foreign process | stale smartmemory | live smartmemory |
env/config override | hung own daemon without lsof | hung listener nobody can name.
Each row drives the probe, setup's port step, start's pre-spawn refusal, the
post-spawn message, and the doctor lines. Only the holder lookup tools are forced
(the probe's own seams), never the listeners.
"""

import json
import os
import socket
import threading
from collections import namedtuple
from typing import NamedTuple
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil
import pytest
from click.testing import CliRunner

from smartmemory_app import daemon, port_probe
from smartmemory_app.cli import cli
from smartmemory_app.config import config_path, load_config
from smartmemory_app.store_diagnostics import daemon_port_rows

LIVE_BODY = {
    "service": "smartmemory",
    "status": "ok",
    "install_id_present": True,
    "version": "9.9.9",
}
STALE_BODY = {"service": "smartmemory", "status": "ok"}


def _server(body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(body or {"detail": "nope"}).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


Conn = namedtuple("Conn", "laddr status pid")
Addr = namedtuple("Addr", "ip port")


class Row(NamedTuple):
    name: str
    listener: str  # free | http | hung (accepts, never answers)
    body: dict | None
    lookup: str  # real | no lsof | nobody (no tool can name the holder)
    ours: bool  # does the held process's command line say SmartMemory
    state: str  # probe_port
    moves: bool  # setup moves the port
    refuses: bool  # start refuses before spawning
    doctor_says: str  # sm doctor --ports line
    message_says: str  # conflict_message for a non-free port
    row_says: str  # Daemon port doctor row


# the old message that must never describe a live, stale or unidentified holder
BLAME = "which is not SmartMemory"
ROWS = [
    Row("free", "free", None, "real", False, "free", False, False, "free", "", "free"),
    Row(
        "foreign",
        "http",
        None,
        "real",
        False,
        "foreign",
        True,
        True,
        "NOT SmartMemory",
        BLAME,
        "non-SmartMemory",
    ),
    Row(
        "stale",
        "http",
        STALE_BODY,
        "real",
        False,
        "stale",
        False,
        True,
        "not answering normally",
        "not answering normally",
        "older or hung",
    ),
    Row(
        "live",
        "http",
        LIVE_BODY,
        "real",
        False,
        "live",
        False,
        False,
        "SmartMemory daemon 9.9.9",
        "sm status",
        "free",
    ),
    Row(
        "env override on a foreign port",
        "http",
        None,
        "real",
        False,
        "foreign",
        False,
        True,
        "NOT SmartMemory",
        BLAME,
        "non-SmartMemory",
    ),
    Row(
        "foreign answering, no lsof",
        "http",
        None,
        "nobody",
        False,
        "foreign",
        True,
        True,
        "NOT SmartMemory",
        BLAME,
        "non-SmartMemory",
    ),
    Row(
        "hung own daemon, no lsof",
        "hung",
        None,
        "no lsof",
        True,
        "stale",
        False,
        True,
        "not answering normally",
        "sm stop",
        "older or hung",
    ),
    Row(
        "hung, holder unknown",
        "hung",
        None,
        "nobody",
        False,
        "unknown",
        False,
        True,
        "holder not identified",
        "could not be identified",
        "hung SmartMemory daemon",
    ),
]


class _HungListener:
    """Accepts connections and holds them open without ever answering."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                self.held.append(self.sock.accept()[0])
            except OSError:
                return

    def close(self):
        self.sock.close()
        for conn in self.held:
            conn.close()


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_port_conflict_table(monkeypatch, tmp_path, row):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SMARTMEMORY_DAEMON_PORT", raising=False)
    monkeypatch.setattr(port_probe, "HANDSHAKE_TIMEOUT", 0.3)
    holder = None
    if row.listener == "free":
        port = _free_port()
    elif row.listener == "hung":
        holder = _HungListener()
        port = holder.sock.getsockname()[1]
    else:
        holder = _server(row.body)
        port = holder.server_address[1]
    if row.lookup != "real":
        monkeypatch.setattr(port_probe, "_lsof_holder", lambda p: (None, None))
    if row.lookup == "nobody":
        monkeypatch.setattr(port_probe, "_psutil_holder", lambda p: (None, None))
    monkeypatch.setattr(port_probe, "_is_smartmemory_process", lambda pid: row.ours)
    fallback = _free_port()
    monkeypatch.setattr(port_probe, "FALLBACK_PORTS", (port, fallback))
    config_path().parent.mkdir(parents=True)
    config_path().write_text(f"[local]\ndaemon_port = {port}\n")
    if row.name.startswith("env override"):
        monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(port))
    notices = []
    try:
        held = port_probe.probe_port(port)
        assert held.state == row.state
        # setup: a foreign holder moves the port (persisted in the config file)
        # and never when the env pins it, the holder is SmartMemory, or is unknown.
        chosen = port_probe.ensure_daemon_port_free(notices.append)
        if row.moves:
            assert chosen == fallback
            assert f"port {fallback}" in notices[0] and str(port) in notices[0]
            assert load_config(apply_env=False).daemon_port == fallback
        else:
            assert chosen == port and notices == []
            assert load_config(apply_env=False).daemon_port == port
        # start: foreign, stale or unidentified holders are named before anything is spawned.
        monkeypatch.setattr(daemon, "_port", lambda: port)
        if row.refuses:
            with pytest.raises(RuntimeError) as err:
                daemon._refuse_held_port()
            assert f"Port {port} is" in str(err.value)
            if row.state in ("stale", "unknown"):
                assert BLAME not in str(err.value)
                assert "another program" not in str(err.value)
        else:
            daemon._refuse_held_port()
        # the post-spawn message never calls a SmartMemory (or possible one) holder foreign.
        if row.state != "free":
            message = port_probe.conflict_message(held, config_path())
            assert row.message_says in message, message
            if row.state != "foreign":
                assert BLAME not in message and "another program" not in message
        # doctor --ports lists the configured port with its holder.
        monkeypatch.setattr(
            "smartmemory_app.config.load_config",
            lambda *a, **k: type("C", (), {"daemon_port": port})(),
        )
        result = CliRunner().invoke(cli, ["doctor", "--ports"])
        assert result.exit_code == 0, result.output
        line = next(
            ln for ln in result.output.splitlines() if ln.startswith(f"Port {port} ")
        )
        assert row.doctor_says in line, line
        # the Daemon port doctor row: restart for stale or unidentified, never "change ports".
        with monkeypatch.context() as listing:
            listing.setattr(
                psutil,
                "net_connections",
                lambda kind="tcp": []
                if row.listener == "free"
                else [Conn(Addr("127.0.0.1", port), psutil.CONN_LISTEN, os.getpid())],
            )
            doctor_row = next(iter(daemon_port_rows(port)))
        if row.state in ("stale", "unknown"):
            assert row.row_says in doctor_row and "smartmemory stop" in doctor_row
            assert "SMARTMEMORY_DAEMON_PORT" not in doctor_row, doctor_row
        elif row.state == "foreign":
            assert row.row_says in doctor_row, doctor_row
    finally:
        if row.listener == "hung":
            holder.close()
        elif holder:
            holder.shutdown()
            holder.server_close()


def test_health_identity_never_exposes_the_install_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / ".install-id").write_text("12345678-1234-5678-1234-567812345678")
    fields = port_probe.identity_fields()
    assert fields["install_id_present"] is True and fields["version"]
    assert "12345678" not in json.dumps(fields)
