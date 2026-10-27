"""CLI-PORT-CONFLICT-1: a held daemon port is found, named and avoided.

Real loopback listeners on OS-assigned ports (never 9014), real /health answers.
One row table: port free | foreign process | stale smartmemory | live smartmemory |
env/config override. Each row drives the probe, setup's port step and start's
pre-spawn refusal, and checks the doctor --ports line.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from click.testing import CliRunner

from smartmemory_app import daemon, port_probe
from smartmemory_app.cli import cli
from smartmemory_app.config import config_path, load_config

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


# kind of listener | probe state | setup moves port | start refuses | doctor --ports says
ROWS = [
    ("free", None, "free", False, False, "free"),
    ("foreign", None, "foreign", True, True, "NOT SmartMemory"),
    ("stale", STALE_BODY, "stale", False, True, "not answering normally"),
    ("live", LIVE_BODY, "live", False, False, "SmartMemory daemon 9.9.9"),
    ("env override on a foreign port", None, "foreign", False, True, "NOT SmartMemory"),
]


@pytest.mark.parametrize(
    "name, body, state, moves, refuses, doctor_says",
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_port_conflict_table(
    monkeypatch, tmp_path, name, body, state, moves, refuses, doctor_says
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SMARTMEMORY_DAEMON_PORT", raising=False)
    httpd = None
    if name == "free":
        port = _free_port()
    else:
        httpd = _server(body)
        port = httpd.server_address[1]
    fallback = _free_port()
    monkeypatch.setattr(port_probe, "FALLBACK_PORTS", (port, fallback))
    config_path().parent.mkdir(parents=True)
    config_path().write_text(f"[local]\ndaemon_port = {port}\n")
    if name.startswith("env override"):
        monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(port))
    notices = []
    try:
        assert port_probe.probe_port(port).state == state
        # setup: a foreign holder moves the port (persisted in the config file)
        # and never when the env pins it or the holder is SmartMemory.
        chosen = port_probe.ensure_daemon_port_free(notices.append)
        if moves:
            assert chosen == fallback
            assert f"port {fallback}" in notices[0] and str(port) in notices[0]
            assert load_config(apply_env=False).daemon_port == fallback
        else:
            assert chosen == port and notices == []
            assert load_config(apply_env=False).daemon_port == port
        # start: foreign or stale holders are named before anything is spawned.
        monkeypatch.setattr(daemon, "_port", lambda: port)
        if refuses:
            with pytest.raises(RuntimeError) as err:
                daemon._refuse_held_port()
            assert f"Port {port} is" in str(err.value)
            assert "process" in str(err.value) or "another program" in str(err.value)
        else:
            daemon._refuse_held_port()
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
        assert doctor_says in line, line
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()


def test_health_identity_never_exposes_the_install_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / ".install-id").write_text("12345678-1234-5678-1234-567812345678")
    fields = port_probe.identity_fields()
    assert fields["install_id_present"] is True and fields["version"]
    assert "12345678" not in json.dumps(fields)
