"""Durable SessionEnd acknowledgements; immutable, atomically published events."""

from __future__ import annotations

import argparse
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

from smartmemory_app import capture_hold as hold

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
    """Queue a capture. At most one queued event per transcript: a newer one replaces it.

    While SMARTMEMORY_CAPTURE_HOLD is on (the default) the event is parked in ``held-all/``
    instead and comes back with status ``held``: callers spawn no worker for it.
    """
    root = capture_dir()
    resolved = (
        str(Path(transcript_path).expanduser().resolve()) if transcript_path else None
    )
    if resolved and hold.hold_on():
        record = {
            "capture_id": uuid.uuid4().hex,
            "session_id": session_id,
            "transcript_path": resolved,
            "workspace_id": workspace_id,
            "cwd": cwd,
            "queued_at": time.time(),
        }
        with FileLock(str(root / "ledger.lock"), timeout=2):
            hold.park(root, record, hold.HELD)
        log.warning(
            "Capture held for session %s (%s=1), release with `release-held`",
            session_id,
            hold.HOLD_ENV,
        )
        return {**record, "status": "held"}
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


def hold_event(job: dict, dirname: str, why: str) -> None:
    """Move a pending ledger event into a parking dir, one WARNING, status ``held``/``quarantined``."""
    root = capture_dir()
    with FileLock(str(root / "ledger.lock"), timeout=2):
        hold.park(
            root, job, dirname
        )  # Durable first: a crash leaves it pending, not lost.
        _append(
            root,
            {**job, "status": "held" if dirname == hold.HELD else "quarantined"},
        )
    log.warning(
        "Capture %s for session %s parked in %s: %s",
        job["capture_id"],
        job.get("session_id"),
        dirname,
        why,
    )


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


_ACTIVE = {"queued", "running"}


class ReleaseRefused(Exception):
    """The hold or the size knob would park the events again."""


def release_held(
    large: bool = False, dry_run: bool = False, force: bool = False
) -> list[tuple[str, dict, float | None]]:
    """Re-queue every parked event once. Returns (action, event, transcript MB) per file.

    Newest event per transcript path is queued, older ones are recorded ``superseded``.
    Spawns no worker: follow with ``sm lifecycle drain``. A dry run moves nothing.
    """
    root = capture_dir()
    events = hold.parked(root, hold.LARGE if large else hold.HELD)
    sizes = {
        e.get("transcript_path"): hold.size_mb(e.get("transcript_path"))
        for _, e in events
    }
    if not dry_run and not force:
        limit = hold.over_mb()
        if hold.hold_on():
            raise ReleaseRefused(
                f"{hold.HOLD_ENV} is on, the worker would park them again"
            )
        if (
            large
            and limit > 0
            and any(m is not None and m > limit for m in sizes.values())
        ):
            raise ReleaseRefused(f"{hold.OVER_ENV}={limit:g} would park them again")
    newest = {e.get("transcript_path"): e["capture_id"] for _, e in events}
    snapshot = {}
    for record in jobs(root):
        snapshot[record["capture_id"]] = record
    queued_by_path: dict = {}
    for record in snapshot.values():
        if record["status"] in _ACTIVE:
            queued_by_path.setdefault(record.get("transcript_path"), []).append(record)
    done = []
    for path, event in events:
        cid, tpath = event["capture_id"], event.get("transcript_path")
        keep = newest[tpath] == cid or not tpath
        action = "released" if keep else "superseded"
        done.append((action, event, sizes.get(tpath)))
        if dry_run:
            continue
        with FileLock(str(root / "ledger.lock"), timeout=2):
            if snapshot.get(cid, {}).get("status") not in _ACTIVE:
                if keep:
                    for old in queued_by_path.pop(tpath, []):
                        _append(
                            root, {**old, "status": "superseded", "superseded_by": cid}
                        )
                    _append(root, {**event, "status": "queued"})
                else:
                    _append(
                        root,
                        {
                            **event,
                            "status": "superseded",
                            "superseded_by": newest[tpath],
                        },
                    )
            path.unlink(missing_ok=True)
    return done


def run_release(large: bool, dry_run: bool, force: bool) -> int:
    """CLI body shared by ``capture_queue release-held`` and ``sm lifecycle release-held``."""
    try:
        done = release_held(large, dry_run, force)
    except ReleaseRefused as exc:
        print(f"refused: {exc}. Use --force to release anyway.", file=sys.stderr)
        return 1
    for action, event, mb in done:
        size = "missing" if mb is None else f"{mb:.1f} MB"
        print(f"{'would ' if dry_run else ''}{action} {event.get('session_id')} {size}")
    print(
        f"{len(done)} event(s). Run `sm lifecycle drain` to start a worker."
        if done and not dry_run
        else f"{len(done)} event(s)."
    )
    return 0


RELEASE_HELP = (
    "Move parked capture events back to the queue once, one line per event. Default source "
    "is held-all/, --large uses quarantine-large/. Refused while SMARTMEMORY_CAPTURE_HOLD is "
    "on (with --large also while SMARTMEMORY_CAPTURE_HOLD_OVER_MB would park them again) "
    "unless --force. --dry-run lists without moving. Starts no worker: follow with "
    "`sm lifecycle drain`."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capture_queue")
    sub = parser.add_subparsers(dest="command", required=True)
    rel = sub.add_parser("release-held", help=RELEASE_HELP, description=RELEASE_HELP)
    rel.add_argument("--large", action="store_true", help="release quarantine-large/")
    rel.add_argument("--dry-run", action="store_true", help="list, move nothing")
    rel.add_argument(
        "--force", action="store_true", help="release although it would be parked again"
    )
    args = parser.parse_args(argv)
    return run_release(args.large, args.dry_run, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
