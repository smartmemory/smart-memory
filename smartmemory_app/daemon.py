"""DIST-DAEMON-1: Daemon lifecycle helpers — start/stop the viewer server as a background process.

Not a server itself — just functions for managing the daemon process.
The daemon IS viewer_server.main() running in a detached subprocess.
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


def _data_dir() -> Path:
    """Resolve data dir from config (respects data_dir setting and SMARTMEMORY_DATA_DIR env)."""
    from smartmemory_app.storage import _resolve_data_dir

    return _resolve_data_dir()


def _pid_file() -> Path:
    return _data_dir() / "daemon.pid"


def _port() -> int:
    from smartmemory_app.config import load_config

    return load_config().daemon_port


# ── launchd integration (macOS) ───────────────────────────────────────────────
# `smartmemory setup` on macOS installs launchd plists with KeepAlive=true, so
# launchd — not this module's subprocess logic — owns the daemon. A plain os.kill
# is respawned instantly, which made `sm stop` a no-op and blocked `sm start`
# /`sm restart` from picking up a config change (e.g. a local→remote mode switch).
# These helpers make stop bootout the launchd job and start bootstrap it. All are
# no-ops off macOS / when no plist is installed, so the subprocess path is intact.
_LAUNCHD_DAEMON_LABEL = "ai.smartmemory.daemon"
_LAUNCHD_WORKER_LABEL = "ai.smartmemory.worker"


def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_loaded(label: str) -> bool:
    """True if a launchd job with this label is currently loaded (macOS only)."""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def _launchd_bootout(label: str) -> bool:
    """Unload a launchd job so KeepAlive stops respawning it. Idempotent; macOS only."""
    if sys.platform != "darwin":
        return False
    uid = os.getuid()
    # Modern API (bootout) first, then legacy unload as a fallback.
    for cmd in (
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        ["launchctl", "unload", str(_launchd_plist_path(label))],
    ):
        try:
            if subprocess.run(cmd, capture_output=True, text=True).returncode == 0:
                return True
        except Exception:
            continue
    return False


def _launchd_bootstrap(label: str, errors: Optional[list[str]] = None) -> bool:
    """Load a launchd job from its plist (RunAtLoad starts it). Idempotent; macOS only."""
    if sys.platform != "darwin":
        return False
    plist = _launchd_plist_path(label)
    if not plist.exists():
        return False
    uid = os.getuid()
    for cmd in (
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        ["launchctl", "load", str(plist)],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return True
            if errors is not None:
                detail = (result.stderr or result.stdout or "").strip()
                errors.append(
                    f"{' '.join(cmd)}: {detail or f'exit code {result.returncode}'}"
                )
        except Exception as exc:
            if errors is not None:
                errors.append(f"{' '.join(cmd)}: {exc}")
            continue
    return False


def _launchd_manages_daemon() -> bool:
    """True if the daemon plist is installed — launchd, not a subprocess, owns it."""
    return (
        sys.platform == "darwin" and _launchd_plist_path(_LAUNCHD_DAEMON_LABEL).exists()
    )


def is_running(require_healthy: bool = True) -> bool:
    """Check daemon is running AND is SmartMemory (not a random process on the port).

    require_healthy=True: also checks backend loaded ("ok" status).
    require_healthy=False: any SmartMemory response counts (for stop/status).

    Uses trust_env=False so proxy env vars never route the local health check
    through a proxy and falsely report the daemon as down (L4).
    """
    try:
        import httpx

        with httpx.Client(trust_env=False) as client:
            r = client.get(f"http://127.0.0.1:{_port()}/health", timeout=2)
        data = r.json()
        if data.get("service") != "smartmemory":
            return False
        if require_healthy and data.get("status") != "ok":
            return False
        return True
    except Exception:
        return False


def _stream_new_log_lines(
    log_path: Path, last_pos: int, emit: Callable[[str], None]
) -> int:
    """Emit complete new lines from log_path past last_pos; return the new position.

    Used by start_daemon to surface the daemon's own startup progress (which it
    already prints — "Loading backend...", "Backend ready (Xs)", etc.) to the
    terminal while `sm start` blocks. Best-effort: any error returns last_pos
    unchanged so streaming can never break startup. A trailing partial line is
    left buffered (not emitted) until its newline arrives on the next poll.
    """
    try:
        with open(log_path, "rb") as fh:
            fh.seek(last_pos)
            data = fh.read()
        if not data:
            return last_pos
        text = data.decode("utf-8", errors="replace")
        nl = text.rfind("\n")
        if nl == -1:
            return last_pos  # no complete line yet
        for line in text[:nl].split("\n"):
            if line.strip():
                emit(line)
        return last_pos + len(text[: nl + 1].encode("utf-8"))
    except Exception:
        return last_pos


def _tail_log_lines(log_path: Path, limit: int = 20) -> list[str]:
    """Read a bounded daemon-log tail for startup error reporting."""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace").splitlines()[
            -limit:
        ]
    except OSError:
        return []


def _launchd_job_summary(label: str) -> list[str]:
    """Return only non-secret launchd state lines for diagnostics."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return [f"launchctl print failed: {exc}"]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return [f"launchctl print: {detail or f'exit code {result.returncode}'}"]
    safe_prefixes = ("state =", "pid =", "runs =", "last exit code =")
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(safe_prefixes)
    ]


def _launchd_failure_message(
    summary: str,
    log_path: Path,
    *,
    command_errors: Optional[list[str]] = None,
    include_job_state: bool = False,
) -> str:
    """Build an actionable startup failure without exposing launchd environment values."""
    details = [summary]
    if command_errors:
        details.append("launchctl errors:\n  " + "\n  ".join(command_errors))
    if include_job_state:
        state = _launchd_job_summary(_LAUNCHD_DAEMON_LABEL)
        if state:
            details.append("launchd state:\n  " + "\n  ".join(state))
    tail = _tail_log_lines(log_path)
    if tail:
        details.append(f"Last {len(tail)} lines of {log_path}:\n  " + "\n  ".join(tail))
    else:
        details.append(f"No daemon log was written at {log_path}")
    return "\n".join(details)


def start_daemon(
    num_workers: int = 1, on_log: Optional[Callable[[str], None]] = None
) -> None:
    """Start the daemon and enrichment workers.

    Blocks until daemon is ready (health check passes) or timeout.
    Then starts num_workers background enrichment worker processes.

    Warmup takes ~22s cold (first run), ~2s warm (model cached). When `on_log` is
    given, the daemon's own startup progress lines (written to daemon.log) are
    streamed to it during the wait, so `sm start` shows progress instead of a
    silent hang. Idempotent — returns immediately if already running.
    """
    if is_running():
        return

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    log_path = data / "daemon.log"
    log_path.touch(exist_ok=True)
    # Stream only NEW startup lines — skip whatever was already in daemon.log.
    _log_pos = log_path.stat().st_size if log_path.exists() else 0

    def _pump() -> None:
        nonlocal _log_pos
        if on_log is not None:
            _log_pos = _stream_new_log_lines(log_path, _log_pos, on_log)

    # launchd-managed install (macOS): let launchd own the process via the plist
    # (RunAtLoad/KeepAlive). bootstrap re-loads it after a `sm stop` bootout and
    # starts a fresh process that re-reads config — which is how a local→remote
    # mode switch actually takes effect. Falls through to the subprocess path on
    # non-macOS or when no plist is installed (dev/CI).
    if _launchd_manages_daemon():
        bootstrap_errors: list[str] = []
        for label in (_LAUNCHD_DAEMON_LABEL, _LAUNCHD_WORKER_LABEL):
            if _launchd_plist_path(label).exists() and not _launchd_loaded(label):
                label_errors: list[str] = []
                if not _launchd_bootstrap(label, label_errors):
                    bootstrap_errors.extend(label_errors)
                    raise RuntimeError(
                        _launchd_failure_message(
                            f"Failed to bootstrap launchd job {label}.",
                            log_path,
                            command_errors=bootstrap_errors,
                            include_job_state=True,
                        )
                    )
        for _ in range(120):  # up to 60s for launchd to bring it healthy
            _pump()
            if is_running(require_healthy=False):
                _pump()
                return
            time.sleep(0.5)
        raise TimeoutError(
            _launchd_failure_message(
                "launchd daemon did not become healthy within 60s.",
                log_path,
                command_errors=bootstrap_errors,
                include_job_state=True,
            )
        )

    port = _port()

    # Launch viewer_server.main() directly — NOT the CLI command
    # (avoids recursion since CLI `viewer` calls start_daemon + open browser).
    # Inherit PYTHONPATH so editable installs work in dev.
    env = os.environ.copy()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from smartmemory_app.viewer_server import main; main(port={port}, open_browser=False)",
        ],
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )

    # Phase 1: Wait for port to open (fast socket check, no httpx timeout)
    import socket

    for _ in range(120):  # 60s max
        if proc.poll() is not None:
            _pump()  # surface whatever the daemon logged before it died
            raise RuntimeError(
                f"Daemon exited during startup (code {proc.returncode}). Check {log_path}"
            )
        _pump()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break  # port is open
        finally:
            s.close()
        time.sleep(0.5)
    else:
        proc.terminate()
        raise TimeoutError(f"Daemon failed to bind port within 60s. Check {log_path}")

    # Phase 2: Verify it's actually SmartMemory responding
    if not is_running(require_healthy=False):
        proc.terminate()
        raise RuntimeError(
            f"Port {port} is in use (possibly another SmartMemory daemon or process); "
            f"check `sm status` after fixing. (See {log_path})"
        )

    # Phase 3: Start enrichment worker(s)
    _start_workers(num_workers)
    _pump()  # final drain of any trailing startup lines


def _start_workers(num_workers: int = 1) -> None:
    """Start enrichment worker(s) as detached background processes.

    Each worker polls the SQLite enrichment_queue and runs Tier 2 LLM extraction.
    Multiple workers process jobs in parallel (SQLite row-level locking prevents
    double-processing via the 'processing' status).

    Args:
        num_workers: Number of worker processes to start (default 1).
    """
    from smartmemory_app.config import LLM_KEY_ENV_VARS, llm_key_present

    if not llm_key_present():
        # no-silent-degradation: this fallback disables Tier-2 entirely, so say so.
        log.warning(
            "No LLM API key found (checked %s) — Tier-2 entity extraction and "
            "enrichment workers will NOT start. Memories are still stored with "
            "Tier-1 (spaCy) extraction only. Add a key with `smartmemory setup`.",
            ", ".join(LLM_KEY_ENV_VARS),
        )
        return

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    worker_log = data / "worker.log"
    env = os.environ.copy()

    for i in range(num_workers):
        pid_file = data / f"worker.{i}.pid"

        # Check if this slot is already running
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                continue  # already running
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)

        proc = subprocess.Popen(
            [sys.executable, "-m", "smartmemory_app.enrichment_worker", "--loop"],
            stdout=open(worker_log, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        pid_file.write_text(str(proc.pid))


def _stop_workers() -> None:
    """Stop all enrichment workers. Idempotent."""
    data = _data_dir()
    for pid_file in data.glob("worker.*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)


def stop_daemon() -> None:
    """Stop the daemon and all workers. Idempotent — no-op if not running.

    If launchd manages the daemon (KeepAlive=true), bootout the job FIRST —
    otherwise the os.kill below is respawned instantly and `sm stop` is a no-op
    (and a following mode switch never takes effect). Falls through to the
    subprocess kill path when launchd isn't managing it.
    """
    _stop_workers()

    if sys.platform == "darwin":
        booted = False
        for label in (_LAUNCHD_WORKER_LABEL, _LAUNCHD_DAEMON_LABEL):
            if _launchd_loaded(label):
                _launchd_bootout(label)
                booted = True
        if booted:
            for _ in range(40):  # up to 10s for launchd to tear it down
                if not is_running(require_healthy=False):
                    _pid_file().unlink(missing_ok=True)
                    return
                time.sleep(0.25)

    import httpx

    # Prefer health-check-based stop — confirms we're killing SmartMemory, not a reused PID
    if is_running(require_healthy=False):
        try:
            with httpx.Client(trust_env=False) as _hc:
                r = _hc.get(f"http://127.0.0.1:{_port()}/health", timeout=2)
            pid = r.json().get("pid")
            if pid:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not is_running(require_healthy=False):
                        _pid_file().unlink(missing_ok=True)
                        return
                    time.sleep(0.25)
                # Still running after 5s — force kill
                os.kill(pid, signal.SIGKILL)
                _pid_file().unlink(missing_ok=True)
                return
        except Exception:
            pass

    # Fallback: PID file (only if health unreachable but file exists)
    pf = _pid_file()
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            # Verify it's actually a smartmemory process before killing
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
            )
            if "smartmemory" in result.stdout:
                os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        pf.unlink(missing_ok=True)


def get_status() -> dict | None:
    """Get daemon status. Returns health dict or None if not running."""
    if not is_running(require_healthy=False):
        return None
    try:
        import httpx

        with httpx.Client(trust_env=False) as client:
            r = client.get(f"http://127.0.0.1:{_port()}/health", timeout=3)
        return r.json()
    except Exception:
        return {"service": "smartmemory", "status": "unreachable"}
