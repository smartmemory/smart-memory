"""LITE-LOOPBACK-HOST-GATE-1: refuse requests a web page could aim at the local daemon.

The daemon binds 127.0.0.1 and has no auth, so any page the user visits can reach it:

- DNS rebinding: ``attacker.example`` re-resolves to 127.0.0.1, the page becomes
  same-origin with ``http://attacker.example:<port>`` and can read and delete every
  memory. Its requests still carry ``Host: attacker.example``, so only loopback Host
  names are served.
- Cross-site "simple" requests (a form POST, a body-less ``fetch``) need neither CORS
  nor rebinding. Browsers always send ``Origin`` on them, so a mutating request whose
  Origin is not the daemon's own page is refused. Non-browser callers (the CLI, the
  hooks, the TUI, the Obsidian plugin's ``requestUrl``) send no Origin and pass.
- Cross-site GETs (an ``<img>`` or a navigation) send no Origin but do reach handlers
  with side effects (``GET /memory/recall`` appends to the hook trace). Browsers that
  send fetch metadata label them ``Sec-Fetch-Site: cross-site``/``same-site``, and
  those are refused on every method; a present foreign or ``null`` Origin is refused
  on every method too. Safari before 16.4 sends neither header on such a GET.

Pure ASGI (not ``BaseHTTPMiddleware``) so it adds nothing to streaming responses, and
``_build_app`` registers it last so it is the outermost user middleware.
"""

from __future__ import annotations

import logging
import threading
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
# Obsidian's own renderer origin. A web page cannot send it (browsers set Origin),
# and code running inside Obsidian can already reach the daemon without a browser.
FIRST_PARTY_ORIGINS = frozenset({"app://obsidian.md"})

HOST_REFUSED = {"detail": "Refused: this server only answers to localhost."}
ORIGIN_REFUSED = {"detail": "Refused: cross-origin request to the local daemon."}
FETCH_SITE_REFUSED = {"detail": "Refused: cross-site request to the local daemon."}
CROSS_SITE_VALUES = frozenset({"cross-site", "same-site"})
REFUSED_STATUS = 403

_LOG_WINDOW_S = 10.0
_LOG_VALUE_MAX = 100
_PORT_MAX_DIGITS = 5
_PORT_MAX = 65535


def split_host(value: str) -> tuple[str, int | None] | None:
    """Split a Host header into (lowercased name, port); None when malformed."""
    value = value.strip().lower()
    if value.startswith("["):
        name, bracket, rest = value.partition("]")
        if not bracket:
            return None
        name += bracket
    else:
        name, colon, rest = value.partition(":")
        rest = colon + rest
    if not rest:
        return name, None
    digits = rest[1:]
    if not rest.startswith(":") or not (digits.isascii() and digits.isdigit()):
        return None
    if len(digits) > _PORT_MAX_DIGITS or int(digits) > _PORT_MAX:
        return None
    return name, int(digits)


def own_origins(port: int | None) -> frozenset[str]:
    """The Origin values the daemon's own viewer page sends when served on ``port``."""
    suffixes = {f":{port}"} if port is not None else set()
    if port in (None, 80):
        suffixes.add("")
    return frozenset(f"http://{h}{s}" for h in LOOPBACK_HOSTS for s in suffixes)


def _headers(scope: Scope, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers") or () if k == name]


def refusal(scope: Scope) -> tuple[str, str] | None:
    """Return (what was refused, the refused value) or None when the request may pass."""
    hosts = _headers(scope, b"host")
    if len(hosts) != 1:
        return "host", " | ".join(hosts) or "<missing>"
    parsed = split_host(hosts[0])
    if parsed is None or parsed[0] not in LOOPBACK_HOSTS:
        return "host", hosts[0]
    server = scope.get("server")
    # The listening socket's port (uvicorn fills it); the Host port must name it.
    port = server[1] if server and server[1] is not None else parsed[1]
    if parsed[1] is not None and parsed[1] != port:
        return "host", hosts[0]
    # Every method: browsers send Origin on mutations and WebSocket handshakes (CORS
    # never guards the latter), and a foreign or ``null`` Origin on a GET is no caller of ours.
    allowed = own_origins(port) | FIRST_PARTY_ORIGINS
    origins = _headers(scope, b"origin")
    for origin in origins:
        if origin.strip().lower() not in allowed:
            return "origin", origin
    if scope["type"] != "http":
        return None
    fetch_sites = _headers(scope, b"sec-fetch-site")
    if len(fetch_sites) > 1:
        return "fetch-site", " | ".join(fetch_sites)
    # Every Origin present is allowed here, so a present one is Obsidian's or our own
    # page's: a web page cannot send ``app://obsidian.md``.
    if (
        fetch_sites
        and fetch_sites[0].strip().lower() in CROSS_SITE_VALUES
        and not origins
    ):
        return "fetch-site", fetch_sites[0]
    return None


class _RefusalLog:
    """One WARNING per refusal, at most one per window; the next line counts the rest."""

    def __init__(self, window_s: float = _LOG_WINDOW_S) -> None:
        self._window_s = window_s
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._suppressed = 0

    def warn(self, kind: str, method: str, path: str, value: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now < self._next_at:
                self._suppressed += 1
                return
            suppressed, self._suppressed = self._suppressed, 0
            self._next_at = now + self._window_s
        log.warning(
            "Refused %s %r: %s %r is not the local daemon's "
            "(%d more refusals in the last %.0fs not logged)",
            method,
            path[:_LOG_VALUE_MAX],
            kind,
            value[:_LOG_VALUE_MAX],
            suppressed,
            self._window_s,
        )


class LoopbackGate:
    """Root ASGI middleware: loopback Host, own Origin and no cross-site fetch metadata."""

    def __init__(self, app: ASGIApp, refusal_log: _RefusalLog | None = None) -> None:
        self.app = app
        self._log = refusal_log or _RefusalLog()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refused = refusal(scope)
        if refused is None:
            await self.app(scope, receive, send)
            return
        kind, value = refused
        method = scope.get("method", "WEBSOCKET")
        self._log.warn(kind, method, scope.get("path", ""), value)
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        body = {
            "host": HOST_REFUSED,
            "origin": ORIGIN_REFUSED,
            "fetch-site": FETCH_SITE_REFUSED,
        }[kind]
        await JSONResponse(body, status_code=REFUSED_STATUS)(scope, receive, send)
