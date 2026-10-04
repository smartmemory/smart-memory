"""DIST-DAEMON-1: Daemon lifecycle helpers — start/stop the viewer server as a background process.

Not a server itself — just functions for managing the daemon process.
The daemon IS viewer_server.main() running in a detached subprocess.
"""

import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

from smartmemory.utils.process import (
    spawn_detached,
    pid_alive,
    process_cmdline,
    terminate_process,
)

from smartmemory_app.diagnostics import redact_credentials

log = logging.getLogger(__name__)
_deadline: ContextVar[float | None] = ContextVar("daemon_deadline", default=None)
STOP_TIMEOUT = 30
START_TIMEOUT = 75
RESTART_TIMEOUT = STOP_TIMEOUT + START_TIMEOUT


@contextmanager
def lifecycle_budget(seconds: float):
    """Set one absolute deadline at entry; nested lifecycle calls inherit it."""
    if _deadline.get() is not None:
        yield
        return
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def bounded_lifecycle(seconds: float):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with lifecycle_budget(seconds):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _remaining(limit: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            "Lifecycle deadline expired; shutdown did not complete or startup remains unverified."
        )
    return min(limit, remaining)


def _pause(seconds: float) -> None:
    time.sleep(_remaining(seconds))
    _remaining(seconds)


def _run_command(command, *, limit: float = 5, **kwargs):
    try:
        result = subprocess.run(command, timeout=_remaining(limit), **kwargs)
        _remaining(limit)
        return result
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            f"{command[0]} {command[1]} timed out; lifecycle operation unverified"
        ) from None


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
    r = _run_command(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return True
    if r.returncode == 113 and "Could not find service" in r.stderr:
        return False
    if r.returncode == 112 and "Could not find domain for user gui:" in r.stderr:
        log.warning(
            "Skipping launchd inspection/bootout for %s: GUI domain absent; using unmanaged PID shutdown",
            label,
        )
        return False
    raise RuntimeError(
        f"Cannot inspect launchd job {label}: "
        f"{r.stderr.strip() or r.stdout.strip() or f'exit code {r.returncode}'}"
    )


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
            result = _run_command(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return True
            log.warning(
                "launchd bootout via %s failed; restart prevention unverified: %s",
                cmd[1],
                redact_credentials(result.stderr.strip()),
            )
        except OSError as exc:
            log.warning(
                "launchd bootout via %s unavailable; restart prevention unverified: %s",
                cmd[1],
                redact_credentials(str(exc)),
            )
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
            result = _run_command(cmd, capture_output=True, text=True)
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


def should_be_running() -> bool:
    """Whether local lifecycle markers say a daemon is expected to exist."""
    # An installed plist survives a deliberate stop. A loaded job, including a
    # crashed KeepAlive job awaiting respawn, still represents running intent.
    return _pid_file().exists() or (
        _launchd_manages_daemon() and _launchd_loaded(_LAUNCHD_DAEMON_LABEL)
    )


def _health_response(limit: float):
    """Cancel the entire response at the lifecycle deadline, including its body."""
    import httpx

    url = f"http://127.0.0.1:{_port()}/health"
    if _deadline.get() is None:
        with httpx.Client(trust_env=False) as client:
            return client.get(url, timeout=limit)

    async def request():
        # Cancellation exits the client context and closes the socket. A read
        # inactivity timeout alone cannot bound a continuously trickling body.
        async with httpx.AsyncClient(trust_env=False) as client:
            return await client.get(url, timeout=_remaining(limit))

    async def bounded_request():
        return await asyncio.wait_for(request(), timeout=_remaining(limit))

    try:
        response = asyncio.run(bounded_request())
    except TimeoutError:
        # A cancelled probe is unavailable health, not failed shutdown. Only the
        # absolute lifecycle deadline may abort the caller's remaining work.
        _remaining(limit)
        raise httpx.ReadTimeout("Health probe timed out") from None
    _remaining(limit)
    return response


def is_running(require_healthy: bool = True) -> bool:
    """Check daemon is running AND is SmartMemory (not a random process on the port).

    require_healthy=True: also checks backend loaded ("ok" status).
    require_healthy=False: any SmartMemory response counts (for stop/status).

    Uses trust_env=False so proxy env vars never route the local health check
    through a proxy and falsely report the daemon as down (L4).
    """
    try:
        r = _health_response(2)
        _remaining(2)
        data = r.json()
        if data.get("service") != "smartmemory":
            return False
        if require_healthy and data.get("status") != "ok":
            return False
        return True
    except TimeoutError:
        raise
    except Exception:
        _remaining(2)
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
                emit(redact_credentials(line))
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
        result = _run_command(
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


def _startup_failure_message(
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
    output_path = log_path.with_name("daemon-output.log")
    output_tail = _tail_log_lines(output_path)
    if output_tail:
        details.append(
            f"Last {len(output_tail)} lines of {output_path}:\n  "
            + "\n  ".join(output_tail)
        )
    return redact_credentials("\n".join(details))


def _prepare_output_log(data: Path) -> None:
    """Rotate oversized inherited output before opening the child's handle."""
    limit = int(os.environ.get("SMARTMEMORY_DAEMON_OUTPUT_MAX_BYTES", 5 * 1024 * 1024))
    if not 0 < limit <= 100 * 1024 * 1024:
        raise ValueError(
            "SMARTMEMORY_DAEMON_OUTPUT_MAX_BYTES must be between 1 and 104857600."
        )
    output = data / "daemon-output.log"
    if output.exists() and output.stat().st_size > limit:
        backup = data / "daemon-output.log.1"
        backup.unlink(missing_ok=True)
        output.replace(backup)
        log.info("Rotated detached daemon output before launch (limit=%s bytes)", limit)


@bounded_lifecycle(START_TIMEOUT)
def start_daemon(
    num_workers: int = 1,
    on_log: Optional[Callable[[str], None]] = None,
    *,
    wait_until_ready: bool = True,
    allow_warming_on_timeout: bool = False,
) -> dict | None:
    """Start the daemon and enrichment workers.

    By default, blocks until the daemon returns a terminal health payload or the
    startup times out. With ``wait_until_ready=False``, an observed ``warming``
    payload is returnable so interactive startup can finish as soon as HTTP is up.
    The caller decides how to render healthy, warming, or degraded states.
    Then starts num_workers background enrichment worker processes. Setup alone
    may accept a fresh warming response at the deadline with
    ``allow_warming_on_timeout=True``; that return defers worker startup.

    Warmup takes ~22s cold (first run), ~2s warm (model cached). When `on_log` is
    given, startup lines from daemon.log and daemon-output.log are
    streamed to it during the wait, so `sm start` shows progress instead of a
    silent hang. Idempotent — returns immediately if already running.
    """
    _upgrade_worker_agent()
    legacy_retired = _retire_legacy_workers()
    existing = get_status()
    if existing is not None and legacy_retired:
        _start_workers(num_workers)
    if existing is not None and (
        not wait_until_ready or existing.get("status") != "warming"
    ):
        return existing

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    log_path = data / "daemon.log"
    log_path.touch(exist_ok=True)
    # Stream only NEW startup lines — skip whatever was already in daemon.log.
    _log_pos = log_path.stat().st_size if log_path.exists() else 0
    output_path = data / "daemon-output.log"
    _output_pos = output_path.stat().st_size if output_path.exists() else 0

    def _pump() -> None:
        nonlocal _log_pos, _output_pos
        if on_log is not None:
            _log_pos = _stream_new_log_lines(log_path, _log_pos, on_log)
            _output_pos = _stream_new_log_lines(output_path, _output_pos, on_log)

    def _returnable(status: dict | None) -> bool:
        return status is not None and (
            not wait_until_ready or status.get("status") != "warming"
        )

    def _setup_timeout_status() -> dict | None:
        # Do not change strict start's probes or timeout behavior. Setup may keep
        # a daemon only when a fresh health response verifies it is still alive.
        if allow_warming_on_timeout:
            status = get_status()
            if status is not None:
                _pump()
                return status
        return None

    # Another caller may already have launched this daemon. Blocking callers keep
    # observing that process; they must not fall through and launch a duplicate.
    if existing is not None:
        for _ in range(120):
            _pump()
            status = get_status()
            if _returnable(status):
                _pump()
                return status
            _pause(0.5)
        status = _setup_timeout_status()
        if status is not None:
            return status
        raise TimeoutError(
            _startup_failure_message(
                "SmartMemory did not finish warming within 60 seconds.",
                log_path,
            )
        )

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
                        _startup_failure_message(
                            f"Failed to bootstrap launchd job {label}.",
                            log_path,
                            command_errors=bootstrap_errors,
                            include_job_state=True,
                        )
                    )
        # Keep polling for the full launch window. A KeepAlive job that crashed is
        # genuinely absent during launchd's 10-second throttle interval; one missed
        # probe is not a verdict. Return only a health payload obtained now.
        for _ in range(120):  # up to 60s for launchd to answer /health
            _pump()
            status = get_status()
            if _returnable(status):
                _pump()
                return status
            _pause(0.5)
        status = _setup_timeout_status()
        if status is not None:
            return status
        raise TimeoutError(
            _startup_failure_message(
                "SmartMemory did not respond within 60 seconds.",
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
    _prepare_output_log(data)
    with (data / "daemon-output.log").open("a", encoding="utf-8") as output:
        proc = spawn_detached(
            [
                sys.executable,
                "-c",
                f"from smartmemory_app.viewer_server import main; main(port={port}, open_browser=False)",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )

    # Phase 1: Wait for port to open (fast socket check, no httpx timeout)
    import socket

    for _ in range(120):  # 60s max
        if proc.poll() is not None:
            _pump()  # surface whatever the daemon logged before it died
            raise RuntimeError(
                _startup_failure_message(
                    f"SmartMemory stopped during startup (code {proc.returncode}).",
                    log_path,
                )
            )
        _pump()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(_remaining(2))
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break  # port is open
        finally:
            s.close()
        _pause(0.5)
    else:
        proc.terminate()
        raise TimeoutError(
            _startup_failure_message(
                "SmartMemory did not open its port within 60s.",
                log_path,
            )
        )

    # Phase 2: Verify it's actually SmartMemory responding
    status = get_status()
    if status is None:
        proc.terminate()
        raise RuntimeError(
            _startup_failure_message(
                f"SmartMemory could not start because port {port} is in use. "
                "Stop the other process, then run `sm status`.",
                log_path,
            )
        )

    if not _returnable(status):
        for _ in range(120):
            if proc.poll() is not None:
                _pump()
                raise RuntimeError(
                    _startup_failure_message(
                        f"SmartMemory stopped during warmup (code {proc.returncode}).",
                        log_path,
                    )
                )
            _pump()
            status = get_status()
            if _returnable(status):
                break
            _pause(0.5)
        else:
            status = _setup_timeout_status()
            if status is None:
                proc.terminate()
                raise TimeoutError(
                    _startup_failure_message(
                        "SmartMemory did not finish warming within 60 seconds.",
                        log_path,
                    )
                )
            if status.get("status") == "warming":
                # Setup accepts a responding daemon, without starting workers
                # before readiness or treating warmup as ready.
                return status

    # Phase 3: Start enrichment worker(s)
    _start_workers(num_workers)
    _pump()  # final drain of any trailing startup lines
    return status


def _pid_alive(pid: int) -> bool:
    """Inspect existence without sending a signal on any platform."""
    return pid_alive(pid)


def _wait_legacy_exit(pid: int) -> None:
    deadline = time.monotonic() + 10
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Legacy enrichment worker {pid} did not stop")
        _pause(0.05)


def _legacy_agent_state() -> str | None:
    result = _run_command(
        ["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_WORKER_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout
    if result.returncode == 113 and "Could not find service" in result.stderr:
        return None
    raise RuntimeError(f"Cannot inspect legacy launchd worker: {result.stderr.strip()}")


def _retire_legacy_agent() -> bool:
    # Historical launchd -c enrichment_worker.main() never wrote a PID file.
    plist = _launchd_plist_path(_LAUNCHD_WORKER_LABEL)
    if (
        sys.platform != "darwin"
        or not plist.exists()
        or "enrichment_worker" not in plist.read_text()
    ):
        return False
    state = _legacy_agent_state()
    if state is None:
        return False
    match = re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
    pid = int(match[1]) if match else None
    if "state = running" in state and pid is None:
        raise RuntimeError(
            "Legacy launchd worker is running without an inspectable PID"
        )
    # Preserve the identity across failed bootout/wait attempts.
    if pid is not None:
        (_data_dir() / "worker.launchd.pid").write_text(str(pid))
    if not _launchd_bootout(_LAUNCHD_WORKER_LABEL) or _legacy_agent_state() is not None:
        raise RuntimeError("Legacy launchd worker could not be positively booted out")
    if pid is not None:
        _wait_legacy_exit(pid)
    return True


def _processing_count() -> int:
    database = _data_dir() / "memory.db"
    if not database.exists():
        return 0
    with sqlite3.connect(database) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_queue'"
        ).fetchone():
            return 0
        return conn.execute(
            "SELECT count(*) FROM enrichment_queue WHERE status='processing'"
        ).fetchone()[0]


def _retire_legacy_workers(*, recover_queue: bool = True) -> bool:
    """Prove legacy quiescence before recovering in-flight rows or starting core."""
    try:
        retired = _retire_legacy_agent()
        for pid_file in _data_dir().glob("worker.*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                log.warning(
                    "Legacy PID identity lost in %s; retirement cannot be verified",
                    pid_file,
                )
                raise RuntimeError(f"Invalid legacy PID file: {pid_file}") from None
            if pid <= 0:
                raise RuntimeError(f"Invalid legacy PID in {pid_file}: {pid}")
            if _pid_alive(pid):
                arguments = process_cmdline(pid)
                if not arguments:
                    raise RuntimeError(
                        f"Cannot inspect live legacy PID {pid}: command line unavailable"
                    )
                if any("smartmemory_app.enrichment_worker" in arg for arg in arguments):
                    log.warning(
                        "Legacy worker %s has no cooperative stop protocol; terminating, final flush unverified",
                        pid,
                    )
                    if not terminate_process(pid, timeout=_remaining(5)):
                        raise RuntimeError(
                            f"Legacy enrichment worker {pid} exit unverified"
                        )
                    _wait_legacy_exit(pid)
                    retired = True
                else:
                    log.warning(
                        "Legacy PID %s was reused; stale worker identity discarded without signalling",
                        pid,
                    )
            else:
                log.warning("Legacy worker %s is gone; removing stale PID file", pid)
            pid_file.unlink(missing_ok=True)
    except (OSError, RuntimeError) as exc:
        if recover_queue:
            try:
                count = str(_processing_count())
            except sqlite3.Error as count_error:
                count = f"unknown (queue inspection failed: {count_error})"
        else:
            count = "unknown (SQLite queue inspection deferred; shutdown budget reserved for stopping processes)"
        log.warning(
            "Legacy retirement unverified; %s processing rows remain unrecovered; replacement blocked: %s",
            count,
            exc,
        )
        raise
    database = _data_dir() / "memory.db"
    if database.exists():
        if not recover_queue:
            # Core's 30s SQLite waits and unbounded migration cannot safely fit
            # alongside process retirement in the stop budget. Leave rows intact;
            # startup repeats quiescence checks and recovers before replacement.
            log.warning(
                "SQLite legacy queue migration and processing-row recovery deferred "
                "until startup; shutdown budget reserved for stopping processes"
            )
            return retired
        from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph

        recovered = SQLiteWorkGraph(str(database)).migrate_enrichment_queue(
            recover_processing=True
        )
        retired = retired or recovered > 0
    return retired


def _upgrade_worker_agent() -> None:
    """Reuse setup's renderer/reload path for installed pre-core worker agents."""
    plist = _launchd_plist_path(_LAUNCHD_WORKER_LABEL)
    if sys.platform == "darwin" and plist.exists():
        content = plist.read_text()
        if (
            "enrichment_worker" in content
            or "from smartmemory.cli import main" in content
        ):
            _retire_legacy_workers()
            from smartmemory_app.setup import _install_launchd_plist

            if not _install_launchd_plist():
                raise RuntimeError("Could not upgrade the SmartMemory launchd worker")


def _start_workers(num_workers: int = 1) -> None:
    """Start one core worker, including for keyless enrichment.

    ``num_workers`` remains accepted for CLI compatibility; core serializes all
    work for a data directory under its authoritative worker lock.
    """
    from smartmemory.pipeline.work_graph.spawn import worker_is_running

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    _upgrade_worker_agent()
    _retire_legacy_workers()
    if worker_is_running(data):
        return
    with (data / "worker.log").open("a") as output:
        spawn_detached(
            [
                sys.executable,
                "-m",
                "smartmemory_app.worker_entry",
                "--data-dir",
                str(data),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
        )


@bounded_lifecycle(STOP_TIMEOUT)
def _stop_core_worker() -> None:
    """Stop through core's portable cooperative protocol, then verify lock release."""
    from smartmemory.pipeline.work_graph import spawn

    budget = _remaining(STOP_TIMEOUT)
    grace = min(10, budget * 0.4)
    data = _data_dir()
    spawn.stop_worker(data, timeout=grace)
    verify_until = time.monotonic() + budget * 0.2
    while spawn.worker_is_running(data):
        remaining = verify_until - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Core worker exit unverified; replacement blocked")
        _pause(min(0.01, remaining))
    _remaining(STOP_TIMEOUT)


def _stop_workers() -> None:
    """Stop every core launch path and any identified legacy consumer."""

    try:
        _retire_legacy_workers(recover_queue=False)
    except (OSError, RuntimeError) as exc:
        log.warning(
            "Legacy retirement failed during stop; continuing shutdown: %s", exc
        )
    if sys.platform == "darwin" and _launchd_loaded(_LAUNCHD_WORKER_LABEL):
        _launchd_bootout(_LAUNCHD_WORKER_LABEL)
    _stop_core_worker()


@bounded_lifecycle(STOP_TIMEOUT)
def stop_daemon() -> None:
    """Stop the daemon and all workers. Idempotent — no-op if not running.

    If launchd manages the daemon (KeepAlive=true), bootout the job first so it
    cannot respawn the process. Unmanaged daemons receive a cooperative stop
    request followed by portable termination and exit verification if needed.
    """
    _stop_workers()

    if sys.platform == "darwin":
        booted = []
        for label in (_LAUNCHD_WORKER_LABEL, _LAUNCHD_DAEMON_LABEL):
            if _launchd_loaded(label):
                if not _launchd_bootout(label) and _launchd_loaded(label):
                    raise RuntimeError(
                        _startup_failure_message(
                            f"Failed to bootout launchd job {label}; stop unverified.",
                            _data_dir() / "daemon.log",
                            include_job_state=True,
                        )
                    )
                booted.append(label)
        if booted:
            while True:  # Shared deadline includes workers, commands, and HTTP.
                # HTTP closes before asynchronous bootout removes a SIGTERMed
                # job. Returning then makes the next start skip bootstrap.
                if not any(
                    _launchd_loaded(label) for label in booted
                ) and not is_running(require_healthy=False):
                    _pid_file().unlink(missing_ok=True)
                    return
                _pause(0.05)

    # A service health reply identifies the daemon even during backend warmup.
    pid = None
    if is_running(require_healthy=False):
        try:
            response = _health_response(2)
            pid = response.json().get("pid")
        except TimeoutError:
            raise
        except Exception as exc:
            log.warning(
                "Daemon health identity unavailable; inspecting PID marker: %s", exc
            )
            _remaining(2)

    pf = _pid_file()
    if not pid and pf.exists():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
        except ValueError:
            log.warning("Invalid daemon PID marker discarded without signalling")
            pf.unlink(missing_ok=True)
            return
        if not _pid_alive(pid):
            pf.unlink(missing_ok=True)
            return
        arguments = process_cmdline(pid)
        if not arguments:
            raise RuntimeError(
                f"Cannot inspect daemon PID {pid}; stop withheld, marker retained"
            )
        if not any("smartmemory_app.viewer_server" in arg for arg in arguments):
            log.warning(
                "Daemon PID %s was reused; stale marker discarded without signalling",
                pid,
            )
            pf.unlink(missing_ok=True)
            return
    if not pid:
        return
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Invalid daemon health PID; stop withheld")

    request = _data_dir() / f".daemon.stop-{pid}"
    try:
        request.write_text("stop", encoding="utf-8")
        grace_until = time.monotonic() + _remaining(5)
        while _pid_alive(pid) and time.monotonic() < grace_until:
            _pause(0.05)
        if _pid_alive(pid):
            # Revalidate before escalation, including old daemons with no IPC listener.
            arguments = process_cmdline(pid)
            if not arguments or not any(
                "smartmemory_app.viewer_server" in arg for arg in arguments
            ):
                raise RuntimeError(
                    f"Daemon PID {pid} identity unavailable; escalation withheld"
                )
            log.warning(
                "Daemon %s did not stop cooperatively; terminating, final flush unverified",
                pid,
            )
            if not terminate_process(pid, timeout=_remaining(2)):
                raise RuntimeError(f"Daemon {pid} exit unverified; marker retained")
        # Process exit alone does not prove the service has stopped. Keep the
        # marker while health still responds, sharing the same absolute deadline
        # and cancelling even a continuously trickling response body.
        while is_running(require_healthy=False):
            _pause(0.05)
        pf.unlink(missing_ok=True)
    finally:
        request.unlink(missing_ok=True)


def get_status() -> dict | None:
    """Get daemon status. Returns health dict or None if not running."""
    if not is_running(require_healthy=False):
        return None
    try:
        r = _health_response(3)
        _remaining(2)
        data = r.json()
        if data.get("service") != "smartmemory":
            return None
        return data
    except TimeoutError:
        raise
    except Exception:
        _remaining(3)
        return None
