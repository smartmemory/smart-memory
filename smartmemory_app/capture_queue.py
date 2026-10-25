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

#: HOOK-CAPTURE-NO-LLM-1 (owner 2026-10-20, "captures too"): transcript capture is internal ingestion and runs the
#: costless profile (rules extraction, local enrichers, no LLM, no session lessons). Only ``1`` opts in to the
#: previous Groq-pinned pipeline and LLM session lessons, the explicit per-source opt-in and the rollback.
CAPTURE_LLM_ENV = "SMARTMEMORY_CAPTURE_LLM"


def capture_llm_opted_in() -> bool:
    """Whether transcript capture may call an LLM (extraction and session lessons). Default no."""
    return os.environ.get(CAPTURE_LLM_ENV, "").strip() == "1"


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
    root = capture_dir()
    with FileLock(str(root / "ledger.lock"), timeout=2):
        job = _append(
            root,
            {
                "capture_id": uuid.uuid4().hex,
                "session_id": session_id,
                "transcript_path": str(Path(transcript_path).expanduser().resolve())
                if transcript_path
                else None,
                "workspace_id": workspace_id,
                "cwd": cwd,
                "queued_at": time.time(),
                "status": "queued",
            },
        )
        if not transcript_path:
            message = "SessionEnd capture missing transcript_path"
            log.warning(message)
            job = _append(root, {**job, "status": "running"})
            job = _append(root, {**job, "status": "error", "error": message})
    return job


def spawn_worker() -> None:
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


# ── Hook write queue (HOOK-WRITE-QUEUE-1) ──────────────────────────────────────
# observe/learn/distill hooks enqueue in pure bash (no Python start, no store write)
# and return. Layout under <hook data dir>/hook-queue/, written by the hook scripts:
#   payloads/<id>.<phase>.json  raw hook stdin (and <id>.state.json for distill)
#   queue/<id>.<phase>.json     record, published by an atomic rename
# The worker (capture_worker.run_hooks) claims a record by renaming it into
# running/, deletes it with its payload on success, requeues it with a backoff on
# failure and moves it to dead/ after max attempts. Done jobs leave nothing behind,
# so a scan is O(pending), unlike the SessionEnd event ledger above.

HOOK_PHASES = ("observe", "learn", "distill")
_HOOK_SUBDIRS = ("queue", "payloads", "running", "dead")


class PermanentHookError(Exception):
    """A hook job that can never succeed (unreadable or invalid payload): dead-letter, no retry."""


def hook_queue_dir(root: str | Path | None = None) -> Path:
    """Same rule as the hook scripts: ``$SMARTMEMORY_DATA_DIR`` or ``~/.smartmemory``, then ``hook-queue``."""
    if root is None:
        data = os.environ.get("SMARTMEMORY_DATA_DIR") or str(
            Path.home() / ".smartmemory"
        )
        root = Path(data) / "hook-queue"
    root = Path(root).expanduser()
    for name in _HOOK_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _write_json_atomic(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(path)


def _payload_session(path: str | None) -> str | None:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    value = body.get("session_id") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


def hook_jobs(root: Path, subdir: str = "queue") -> list[dict]:
    """Records in ``subdir`` oldest first. Each carries ``_path`` and an order key ``queued_ns``.

    The bash hook has no clock (bash 3.2), so the first scan stamps ``queued_ns``
    from the record's mtime, and a requeue keeps it: a retried job keeps its place.
    A record without a session id takes the payload's, else ``unknown``.
    """
    records = []
    for path in (root / subdir).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            mtime_ns = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue  # claimed or finished between glob and read
        except (OSError, ValueError) as exc:
            log.warning(
                "Hook queue record %s unreadable, moving it to dead/: %s",
                path.name,
                exc,
            )
            _dead_letter_path(root, path, f"unreadable record: {exc}")
            continue
        if not isinstance(record, dict) or record.get("phase") not in HOOK_PHASES:
            log.warning(
                "Hook queue record %s has no known phase, moving it to dead/", path.name
            )
            _dead_letter_path(root, path, "record has no known phase")
            continue
        record.setdefault("queued_ns", mtime_ns)
        if not record.get("session_id"):
            record["session_id"] = (
                _payload_session(record.get("payload_path")) or "unknown"
            )
        record["_path"] = str(path)
        records.append(record)
    records.sort(key=lambda r: (r["queued_ns"], Path(r["_path"]).name))
    return records


def _strip(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "_path"}


def claim_hook_job(root: Path, job: dict) -> dict | None:
    """Move a queued record into running/; None when it is gone (another claimant)."""
    source = Path(job["_path"])
    target = root / "running" / source.name
    record = {**_strip(job), "claimed_at": time.time()}
    try:
        _write_json_atomic(source, record)  # persist queued_ns before the move
        source.rename(target)
    except FileNotFoundError:
        return None
    return {**record, "_path": str(target)}


def _remove_payloads(job: dict) -> None:
    for key in ("payload_path", "state_snapshot"):
        if job.get(key):
            try:
                Path(job[key]).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Hook queue payload %s not removed: %s", job[key], exc)


def complete_hook_job(job: dict) -> None:
    Path(job["_path"]).unlink(missing_ok=True)
    _remove_payloads(job)


def _dead_letter_path(root: Path, path: Path, error: str) -> None:
    try:
        path.rename(root / "dead" / path.name)
    except OSError as exc:
        log.warning(
            "Hook queue record %s not moved to dead/ (%s): %s", path.name, exc, error
        )


def fail_hook_job(
    root: Path, job: dict, error: str, *, max_attempts: int, permanent: bool = False
) -> str:
    """Requeue with a backoff, or dead-letter after ``max_attempts``. Returns ``requeued`` or ``dead``."""
    attempts = int(job.get("attempts", 0)) + 1
    record = {
        **_strip(job),
        "attempts": attempts,
        "last_error": error,
        "failed_at": time.time(),
    }
    source = Path(job["_path"])
    if permanent or attempts >= max_attempts:
        _write_json_atomic(root / "dead" / source.name, record)
        source.unlink(missing_ok=True)
        log.warning(
            "Hook %s job %s (session %s) dead-lettered after %d attempt(s), its write is lost until requeued by hand: %s",
            job["phase"],
            source.name,
            job.get("session_id"),
            attempts,
            error,
        )
        return "dead"
    record["not_before"] = time.time() + min(60.0, 2.0 ** (attempts - 1))
    _write_json_atomic(root / "queue" / source.name, record)
    source.unlink(missing_ok=True)
    log.warning(
        "Hook %s job %s failed (attempt %d of %d), requeued: %s",
        job["phase"],
        source.name,
        attempts,
        max_attempts,
        error,
    )
    return "requeued"


def recover_running(root: Path, *, max_attempts: int) -> int:
    """Records left in running/ by a dead worker count one attempt and go back to the queue."""
    recovered = 0
    for job in hook_jobs(root, "running"):
        fail_hook_job(
            root,
            job,
            "worker exited while the job was running",
            max_attempts=max_attempts,
        )
        recovered += 1
    return recovered


def hook_counts(root: str | Path | None = None) -> dict[str, int]:
    root = hook_queue_dir(root)
    return {
        name: sum(1 for _ in (root / name).glob("*.json"))
        for name in ("queue", "running", "dead")
    }
