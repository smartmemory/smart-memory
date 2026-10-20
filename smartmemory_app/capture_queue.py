"""Durable SessionEnd acknowledgements; immutable, atomically published events."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from filelock import FileLock
from smartmemory.utils.process import spawn_detached

log = logging.getLogger(__name__)


def capture_dir() -> Path:
    from smartmemory_app.config import load_config

    path = Path(load_config().data_dir).expanduser() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append(root: Path, record: dict) -> dict:
    """Caller holds ledger.lock. A torn temp file is never an acknowledged event."""
    events = root / "events"
    events.mkdir(exist_ok=True)
    record = {**record, "recorded_at": time.time()}
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
    temporary = events / f".{name}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(record, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.rename(events / f"{name}.json")
    # Windows cannot open or fsync a directory; the rename is already durable there.
    for directory in () if os.name == "nt" else (events, root, root.parent):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return record


def jobs(root: Path | None = None) -> list[dict]:
    root = root or capture_dir()
    current = {}
    # Dict insertion order retains FIFO by first queued event, not last transition.
    for path in sorted((root / "events").glob("*.json")):
        record = json.loads(path.read_text())
        current[record["capture_id"]] = record
    return list(current.values())


def transition(job: dict, status: str, **details) -> dict:
    root = capture_dir()
    with FileLock(str(root / "ledger.lock"), timeout=2):
        return _append(root, {**job, **details, "status": status})


def enqueue(
    session_id: str,
    transcript_path: str | None,
    workspace_id: str | None,
    cwd: str | None = None,
) -> dict:
    """Queue a capture. At most one queued event per transcript: a newer one replaces it."""
    root = capture_dir()
    resolved = (
        str(Path(transcript_path).expanduser().resolve()) if transcript_path else None
    )
    with FileLock(str(root / "ledger.lock"), timeout=2):
        # A running item is not touched: the new event captures what it appends.
        stale = (
            [
                j
                for j in jobs(root)
                if j["status"] == "queued" and j.get("transcript_path") == resolved
            ]
            if resolved
            else []
        )
        job = _append(
            root,
            {
                "capture_id": uuid.uuid4().hex,
                "session_id": session_id,
                "transcript_path": resolved,
                "workspace_id": workspace_id,
                "cwd": cwd,
                "queued_at": time.time(),
                "status": "queued",
            },
        )
        # New event first: a crash between the two appends leaves a duplicate, never a gap.
        for old in stale:
            _append(
                root,
                {**old, "status": "superseded", "superseded_by": job["capture_id"]},
            )
        if not transcript_path:
            message = "SessionEnd capture missing transcript_path"
            log.warning(message)
            job = _append(root, {**job, "status": "running"})
            job = _append(root, {**job, "status": "error", "error": message})
    return job


DEFAULT_CAPTURE_MAX = 2
_WORKER_PATTERN = "smartmemory_app.capture_worker"


def _capture_max() -> int:
    raw = os.environ.get("SMARTMEMORY_CAPTURE_MAX", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_CAPTURE_MAX
    except ValueError:
        return DEFAULT_CAPTURE_MAX


def _running_workers() -> int:
    """Live capture workers machine-wide, as the hooks count theirs. 0 if pgrep fails."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", _WORKER_PATTERN],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return len(out.stdout.split())


def spawn_worker() -> None:
    """Start a worker unless SMARTMEMORY_CAPTURE_MAX are already alive (0 = no cap).

    The event is already durable, so a capped spawn loses nothing: a live worker or the
    next SessionEnd drains it. Cap 2, not 1: a SessionEnd landing after the holder's last
    scan must still be able to start one.
    """
    cap = _capture_max()
    if cap > 0:
        running = _running_workers()
        if running >= cap:
            # HOOK-LOAD-CAP-1 mechanism, same line shape as hooks/observe.sh.
            with (capture_dir().parent / "hooks.log").open("a") as handle:
                handle.write(
                    f"WARNING: capture worker spawn skipped: {running} already running "
                    f"(cap {cap}, HOOK-LOAD-CAP-1)\n"
                )
            return
    spawn_successor()


def spawn_successor() -> None:
    """Start a worker with no cap check: a recycling worker replaces itself one for one."""
    root = capture_dir()
    with (root.parent / "hooks.log").open("a") as handle:
        spawn_detached(
            [sys.executable, "-m", "smartmemory_app.capture_worker"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )


def counts() -> dict[str, int]:
    records = jobs()
    return {
        "queued": sum(j["status"] in {"queued", "running"} for j in records),
        "done": sum(j["status"] == "done" for j in records),
        "error": sum(j["status"] == "error" for j in records),
    }


def drain(timeout: float = 60) -> tuple[dict[str, int], int]:
    """Await durable receipts; timeout leaves the detached worker processing."""
    deadline = time.monotonic() + timeout
    result = counts()
    if result["queued"]:
        try:
            spawn_worker()  # Also recovers running jobs abandoned by a dead worker.
        except OSError as exc:
            log.warning("Capture drain could not start worker: %s", exc)
        result = counts()
    while result["queued"]:
        if time.monotonic() >= deadline:
            return result, 2
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        result = counts()
    return result, 1 if result["error"] else 0
