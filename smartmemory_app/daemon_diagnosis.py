"""LOCAL-DAEMON-1: say why the local daemon is not healthy, with a one-line fix.

``collect_facts`` gathers what is observable (health reply, port, ``daemon.pid``,
the launchd plist's Python). ``diagnose`` is a pure function of those facts, so
every failure class is testable by injecting state. Nothing here kills a process
or deletes a file.
"""

from __future__ import annotations

import os
import plistlib
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


#: What a user may do while the daemon reports "warming". Adds and searches are not
#: refused: they queue behind the model load (storage._init_lock), so the status is
#: "not fully loaded", never "unusable" (CLI-HYGIENE-K-1).
WARMING_NOTE = (
    "You can keep using sm add and sm search: requests wait for the load, "
    "so the first ones can be slow."
)


class Diagnosis(NamedTuple):
    klass: str
    ok: bool
    detail: str
    fix: str | None


@dataclass(frozen=True)
class DaemonFacts:
    port: int
    log_path: Path
    config_path: Path
    health: dict | None = None  # parsed /health of a SmartMemory daemon, else None
    port_taken: bool = False  # another program accepts connections on the port
    port_holder_pid: int | None = None
    marker_pid: int | None = None  # daemon.pid content
    marker_garbled: bool = False  # daemon.pid exists but holds no valid pid
    marker_alive: bool = False
    marker_is_daemon: bool | None = None  # None when the command line is unreadable
    plist_python: str | None = None  # Python baked into the launchd plist
    plist_python_ok: bool = True
    log_error: str | None = None  # last error-looking line of daemon.log


_MISSING_MODEL = re.compile(
    r"MissingModelError|model .{0,160}(is required|unavailable)", re.I
)
_SPACY_MODEL = re.compile(r"spaCy model '([^']+)'")
_ERROR_LINE = re.compile(r"(Traceback|Error|ERROR|Exception|CRITICAL)")


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _missing_models_fix(reason: str) -> str:
    steps = []
    if "spacy" in reason.lower():
        model = (_SPACY_MODEL.search(reason) or [None, "en_core_web_sm"])[1]
        steps.append(f"{sys.executable} -m spacy download {model}")
    if "embedding" in reason.lower():
        steps.append("sm warm (needs the network once)")
    if not steps:
        steps.append("sm setup")
    return f"Install the missing models: {' and '.join(steps)}. Then run: sm restart"


def diagnose(f: DaemonFacts) -> Diagnosis:
    addr = f"http://127.0.0.1:{f.port}"
    log = f"Log: {f.log_path}"
    health = f.health
    if health is not None:
        status = health.get("status")
        if status == "ok":
            return Diagnosis("ok", True, f"daemon running at {addr}", None)
        if status == "warming":
            return Diagnosis(
                "warming",
                False,
                f"daemon at {addr} is still warming up (models loading)",
                f"{WARMING_NOTE} Check again in a minute: sm status",
            )
        reason = str(health.get("degraded_reason") or "")
        if _MISSING_MODEL.search(reason):
            return Diagnosis(
                "missing_models",
                False,
                "daemon is up but required model files are missing",
                _missing_models_fix(reason),
            )
        return Diagnosis(
            "degraded",
            False,
            f"daemon at {addr} is up but degraded: {_short(reason) or 'no reason given'}",
            f"Run: sm restart. {log}",
        )
    if f.port_taken:
        holder = f" (process {f.port_holder_pid})" if f.port_holder_pid else ""
        return Diagnosis(
            "port_in_use",
            False,
            f"port {f.port} is used by another program{holder}",
            f"Stop that program, or set daemon_port in {f.config_path} to a free port. Then run: sm start",
        )
    if f.plist_python and not f.plist_python_ok:
        return Diagnosis(
            "wrong_python",
            False,
            f"auto-start launches {f.plist_python}, which no longer exists",
            "Run: sm setup (it rewrites the auto-start entry for this Python)",
        )
    if f.marker_garbled:
        return Diagnosis(
            "stale_marker",
            False,
            "daemon.pid is unreadable and no daemon is answering",
            "Run: sm start (the leftover file is replaced)",
        )
    if f.marker_pid is not None:
        if not f.marker_alive:
            tail = f" Last log error: {_short(f.log_error)}" if f.log_error else ""
            return Diagnosis(
                "crashed",
                False,
                f"daemon exited (process {f.marker_pid} is gone) and left daemon.pid behind.{tail}",
                f"Run: sm start. If it exits again, read {f.log_path}",
            )
        if f.marker_is_daemon is False:
            return Diagnosis(
                "stale_marker",
                False,
                f"daemon.pid names process {f.marker_pid}, which is not SmartMemory",
                "Run: sm start (the leftover file is replaced)",
            )
        return Diagnosis(
            "unresponsive",
            False,
            f"daemon process {f.marker_pid} is running but not answering on port {f.port}",
            "Run: sm restart",
        )
    tail = f" Last log error: {_short(f.log_error)}" if f.log_error else ""
    return Diagnosis(
        "not_running", False, f"daemon is not running.{tail}", "Run: sm start"
    )


def _port_holder_pid(port: int) -> int | None:
    """Best effort: name the listener so the owner knows what to stop."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^p(\d+)$", out, re.MULTILINE)
    return int(match[1]) if match else None


def _last_log_error(log_path: Path) -> str | None:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[
            -40:
        ]
    except OSError:
        return None
    for line in reversed(lines):
        if _ERROR_LINE.search(line):
            return line.strip()
    return None


def _plist_python() -> str | None:
    from smartmemory_app import daemon

    if sys.platform != "darwin":
        return None
    plist = daemon._launchd_plist_path(daemon._LAUNCHD_DAEMON_LABEL)
    try:
        arguments = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return str(arguments[0]) if arguments else None


def collect_facts(status: dict | None = None) -> DaemonFacts:
    """Observe the daemon. ``status`` is a health reply the caller already has."""
    from smartmemory_app import daemon
    from smartmemory_app.config import config_path
    from smartmemory.utils.process import process_cmdline

    port = daemon._port()
    health = status
    if health is None:
        try:
            health = daemon.get_status()
        except Exception:
            health = None
    port_taken = False
    if health is None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            port_taken = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()
    marker_pid, garbled, alive, is_daemon = None, False, False, None
    marker = daemon._pid_file()
    if health is None and marker.exists():
        try:
            marker_pid = int(marker.read_text(encoding="utf-8").strip())
            if marker_pid <= 0:
                raise ValueError("not a pid")
        except (OSError, ValueError):
            marker_pid, garbled = None, True
        if marker_pid is not None:
            alive = daemon._pid_alive(marker_pid)
            if alive:
                arguments = process_cmdline(marker_pid)
                if arguments:
                    is_daemon = any(
                        "smartmemory_app.viewer_server" in a for a in arguments
                    )
    plist_python = _plist_python()
    return DaemonFacts(
        port=port,
        log_path=daemon._data_dir() / "daemon.log",
        config_path=config_path(),
        health=health,
        port_taken=port_taken,
        port_holder_pid=_port_holder_pid(port) if port_taken else None,
        marker_pid=marker_pid,
        marker_garbled=garbled,
        marker_alive=alive,
        marker_is_daemon=is_daemon,
        plist_python=plist_python,
        plist_python_ok=plist_python is None
        or (Path(plist_python).exists() and os.access(plist_python, os.X_OK)),
        log_error=_last_log_error(daemon._data_dir() / "daemon.log")
        if health is None
        else None,
    )
