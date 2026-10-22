"""`Daemon port` doctor row when the OS refuses system-wide socket listing (CLI-CLEAR-DAEMON-1).

macOS lets only root list other processes' sockets, so ``psutil.net_connections``
raised AccessDenied for the tester and every row read "owner unavailable
(AccessDenied)" while the healthy daemon held the port. Real loopback listeners,
real HTTP /health answers. Only the OS refusal itself is forced.
"""

import json
import logging
import socket
import threading
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil
import pytest

from smartmemory_app.store_diagnostics import daemon_port_rows


def _server(body: dict | None):
    """A real loopback HTTP listener. body=None answers 404 like a foreign service."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(body or {"detail": "nope"}).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
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


def _deny(monkeypatch, error):
    def listing(kind="tcp"):
        raise error

    monkeypatch.setattr(psutil, "net_connections", listing)


Conn = namedtuple("Conn", "laddr status pid")
Addr = namedtuple("Addr", "ip port")

# One row per class member: how the OS answers the listing x what holds the port.
# Rows 1-3 are the tester's case (AccessDenied); 4 is any other listing failure.
DENIED_ROWS = [
    ("denied, port free", psutil.AccessDenied(), "free", "OK ({port}, free)"),
    (
        "denied, SmartMemory daemon on the port",
        psutil.AccessDenied(),
        {"service": "smartmemory", "status": "ok"},
        "OK ({port}, SmartMemory listener)",
    ),
    (
        "denied, foreign service on the port",
        psutil.AccessDenied(),
        None,
        "warning: {port} is in use by a process that does not answer as SmartMemory",
    ),
    (
        "listing fails with OSError",
        OSError("sysctl failed"),
        {"service": "smartmemory", "status": "ok"},
        "warning: owner unavailable (OSError)",
    ),
]


@pytest.mark.parametrize(
    "error, holder, expected",
    [row[1:] for row in DENIED_ROWS],
    ids=[row[0] for row in DENIED_ROWS],
)
def test_port_row_when_socket_listing_is_refused(
    monkeypatch, caplog, error, holder, expected
):
    httpd = None
    if holder == "free":
        port = _free_port()
    else:
        httpd = _server(holder)
        port = httpd.server_address[1]
    monkeypatch.setattr("smartmemory_app.daemon._port", lambda: port)
    _deny(monkeypatch, error)
    try:
        with caplog.at_level(
            logging.WARNING, logger="smartmemory_app.store_diagnostics"
        ):
            rows = list(daemon_port_rows(port))
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
    assert len(rows) == 1
    assert rows[0].startswith("Daemon port: " + expected.format(port=port)), rows
    # no-silent-degradation: the fallback names what was lost
    assert any(
        f"Daemon port {port}" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    "listeners, expected",
    [
        ([], "OK ({port}, free)"),
        (
            [Conn(Addr("127.0.0.1", 0), psutil.CONN_LISTEN, None)],
            "warning: {port} held by a non-SmartMemory or unverified process",
        ),
    ],
    ids=["listing works, free", "listing works, foreign holder"],
)
def test_port_row_unchanged_when_listing_works(monkeypatch, listeners, expected):
    port = _free_port()
    fixed = [c._replace(laddr=Addr("127.0.0.1", port)) for c in listeners]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": fixed)
    assert list(daemon_port_rows(port)) == [
        "Daemon port: "
        + expected.format(port=port)
        + (
            ". Fix: choose a free SMARTMEMORY_DAEMON_PORT, then smartmemory start"
            if listeners
            else ""
        )
    ]


def test_real_listing_never_reports_owner_unavailable_for_a_free_port():
    """The tester's exact case on this machine: on macOS without root the real psutil call is denied."""
    port = _free_port()
    assert list(daemon_port_rows(port)) == [f"Daemon port: OK ({port}, free)"]
