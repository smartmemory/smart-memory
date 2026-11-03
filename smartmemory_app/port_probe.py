"""CLI-PORT-CONFLICT-1: find, name and avoid daemon port conflicts.

One place answers "who is on this port?" for setup, start, status and doctor.
Identity is a handshake, not a command-line guess: a SmartMemory daemon's
``/health`` carries ``install_id_present`` and ``version`` (never the id value),
and a listener that does not answer it is not treated as ours.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from typing import NamedTuple

log = logging.getLogger(__name__)

DEFAULT_PORT = 9014
#: Fallback range setup may pick from when the default port is taken.
FALLBACK_PORTS = tuple(range(DEFAULT_PORT, DEFAULT_PORT + 6))

#: How long the ``/health`` handshake waits before the listener counts as not answering.
HANDSHAKE_TIMEOUT = 2.0

FREE, LIVE, STALE, FOREIGN, UNKNOWN = "free", "live", "stale", "foreign", "unknown"


class PortState(NamedTuple):
    port: int
    state: str  # free | live | stale | foreign | unknown
    pid: int | None = None
    program: str | None = None
    version: str | None = None

    @property
    def holder(self) -> str:
        """``process 123 (python3)``, or ``another program`` when it cannot be named."""
        if self.pid and self.program:
            return f"process {self.pid} ({self.program})"
        if self.pid:
            return f"process {self.pid}"
        return "another program"


def identity_fields() -> dict:
    """What ``/health`` adds so a listener can prove it is a SmartMemory daemon.

    Presence only: the install id value is never exposed or logged.
    """
    from smartmemory_app import __version__
    from smartmemory_app.storage import _resolve_data_dir

    try:
        present = (_resolve_data_dir() / ".install-id").is_file()
    except OSError:
        present = False
    return {"install_id_present": present, "version": __version__}


def _listening(port: int, timeout: float = 0.5) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


def _lsof_holder(port: int) -> tuple[int | None, str | None]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except Exception:
        return None, None
    pid = re.search(r"^p(\d+)$", out, re.MULTILINE)
    name = re.search(r"^c(.+)$", out, re.MULTILINE)
    return (int(pid[1]) if pid else None), (name[1] if name else None)


def _psutil_holder(port: int) -> tuple[int | None, str | None]:
    """Cross-platform holder lookup for machines without ``lsof`` (every Windows install).

    The system-wide socket list needs root on macOS, so on AccessDenied it falls back
    to the caller's own processes, which is where a hung SmartMemory daemon lives.
    """
    import psutil

    def listening(connections) -> bool:
        return any(
            c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN
            for c in connections
        )

    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                if c.pid is None:
                    return None, None
                return c.pid, psutil.Process(c.pid).name()
        return None, None
    except psutil.AccessDenied:
        pass
    except (psutil.Error, OSError):
        return None, None
    for process in psutil.process_iter(["pid", "name"]):
        # psutil < 6 (core allows >=5.9) names this ``connections``.
        read = getattr(process, "net_connections", None) or process.connections
        try:
            if listening(read(kind="tcp")):
                return process.info["pid"], process.info["name"]
        except (psutil.Error, OSError, AttributeError):
            continue
    return None, None


def port_holder(port: int) -> tuple[int | None, str | None]:
    """Best effort pid and program name of the listener: ``lsof``, then psutil."""
    pid, name = _lsof_holder(port)
    if pid is None:
        pid, name = _psutil_holder(port)
    return pid, name


def _handshake(port: int) -> dict | None:
    import httpx

    try:
        with httpx.Client(trust_env=False) as client:
            reply = client.get(
                f"http://127.0.0.1:{port}/health", timeout=HANDSHAKE_TIMEOUT
            )
        data = reply.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def probe_port(port: int) -> PortState:
    """Classify a port: free, a live SmartMemory daemon, a stale one, or foreign.

    live: answers ``/health`` as SmartMemory with the identity fields.
    stale: answers as SmartMemory without them (an older daemon), or does not
    answer but its command line is SmartMemory's (hung).
    foreign: answers as something else, or does not answer and its process is not ours.
    unknown: does not answer and no tool could name the holder, so SmartMemory is not
    ruled out. Callers must not move the port or call it "another program".
    """
    if not _listening(port):
        return PortState(port, FREE)
    data = _handshake(port)
    if data and data.get("service") == "smartmemory":
        if "install_id_present" in data and "version" in data:
            # A live daemon names itself: no process listing needed.
            pid = data.get("pid")
            return PortState(
                port,
                LIVE,
                pid if isinstance(pid, int) else None,
                "smartmemory",
                str(data["version"]),
            )
    pid, program = port_holder(port)
    if data and data.get("service") == "smartmemory":
        return PortState(port, STALE, pid, program)
    if data is not None:
        return PortState(port, FOREIGN, pid, program)
    if pid is None:
        return PortState(port, UNKNOWN)
    if _is_smartmemory_process(pid):
        return PortState(port, STALE, pid, program)
    return PortState(port, FOREIGN, pid, program)


def _is_smartmemory_process(pid: int) -> bool:
    from smartmemory.utils.process import process_cmdline

    try:
        arguments = process_cmdline(pid) or []
    except Exception:
        return False
    return any("smartmemory_app.viewer_server" in a for a in arguments)


def conflict_message(state: PortState, config_path) -> str:
    """The one holder line: who has the port and what to do about it."""
    port = state.port
    if state.state == LIVE:
        return (
            f"Port {port} is held by a SmartMemory daemon"
            f"{f' ({state.holder})' if state.pid else ''} that is running. "
            f"Run: sm status. To restart it: sm stop, then sm start."
        )
    if state.state == UNKNOWN:
        return (
            f"Port {port} is held by a process that is not answering and could not be "
            f"identified, so it may be a hung SmartMemory daemon. Run: sm stop, then "
            f"sm start. If the port is still held, set daemon_port in {config_path} "
            f"(or SMARTMEMORY_DAEMON_PORT) to a free port."
        )
    if state.state == STALE:
        stop = f"kill {state.pid}" if state.pid else "stop that process"
        return (
            f"Port {port} is held by a SmartMemory daemon that is not answering "
            f"normally ({state.holder}). Run: sm stop (or {stop}), then sm start."
        )
    return (
        f"Port {port} is in use by {state.holder}, which is not SmartMemory. "
        f"Stop it, or set daemon_port in {config_path} (or SMARTMEMORY_DAEMON_PORT) "
        f"to a free port, then run: sm start."
    )


def pick_free_port(skip: int | None = None) -> int | None:
    for port in FALLBACK_PORTS:
        if port != skip and not _listening(port):
            return port
    return None


def ensure_daemon_port_free(on_log=None) -> int:
    """Setup step: if the configured port is held by a foreign program, move to a free one.

    Only a port taken from the config file or the default is changed, and the new
    port is persisted in the config file (never a shell export). A port the user
    pinned with ``SMARTMEMORY_DAEMON_PORT`` is left alone: start then names the holder.
    """
    import os

    from smartmemory_app.config import load_config, save_config

    cfg = load_config()
    port = cfg.daemon_port
    state = probe_port(port)
    if state.state in (FREE, LIVE):
        return port
    if os.environ.get("SMARTMEMORY_DAEMON_PORT"):
        return port
    if state.state != FOREIGN:
        return port
    new = pick_free_port(skip=port)
    if new is None:
        log.warning(
            "Daemon port %s is held by %s and no port in %s-%s is free, so it is left as is",
            port,
            state.holder,
            FALLBACK_PORTS[0],
            FALLBACK_PORTS[-1],
        )
        return port
    cfg.daemon_port = new
    save_config(cfg)
    log.warning(
        "Daemon port %s was held by %s, so port %s is used instead",
        port,
        state.holder,
        new,
    )
    (on_log or print)(
        f"Port {port} is in use by {state.holder}. SmartMemory will use port {new} instead "
        f"(saved in your SmartMemory config)."
    )
    return new


def port_report_rows(configured: int) -> list[str]:
    """``sm doctor --ports``: every port SmartMemory uses and who holds it."""
    rows = []
    for port in dict.fromkeys((configured, *FALLBACK_PORTS)):
        role = "daemon (configured)" if port == configured else "daemon fallback"
        state = probe_port(port)
        if state.state == FREE:
            holder = "free"
        elif state.state == LIVE:
            holder = f"SmartMemory daemon {state.version} ({state.holder})"
        elif state.state == STALE:
            holder = f"SmartMemory daemon not answering normally ({state.holder})"
        elif state.state == UNKNOWN:
            holder = "not answering, holder not identified (may be a hung SmartMemory daemon)"
        else:
            holder = f"NOT SmartMemory: {state.holder}"
        rows.append(f"Port {port} [{role}]: {holder}")
    return rows
