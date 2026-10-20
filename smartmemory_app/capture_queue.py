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

from filelock import FileLock, Timeout
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


FORCED = "release_forced"


def forced(job: dict) -> bool:
    """True for an event released with ``--force``: the worker skips the hold and size knob."""
    return bool(job.get(FORCED))


def enqueue(
    session_id: str,
    transcript_path: str | None,
    workspace_id: str | None,
    cwd: str | None = None,
    force: bool = False,
) -> dict:
    """Queue a capture. At most one queued event per transcript: a newer one replaces it.

    While SMARTMEMORY_CAPTURE_HOLD is on (the default) the event is parked in ``held-all/``
    instead and comes back with status ``held``: callers spawn no worker for it.
    ``force`` (the continuation of a forced release) skips the hold and marks the event
    ``release_forced``. A newer event that replaces a forced one inherits the mark, so a
    SessionEnd on a transcript the operator released never parks it again.
    """
    root = capture_dir()
    resolved = (
        str(Path(transcript_path).expanduser().resolve()) if transcript_path else None
    )
    if resolved and hold.hold_on() and not force:
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
        force = force or any(forced(old) for old in stale)
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
                **({FORCED: True} if force else {}),
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
# Ledger states that mean a released event really reached the queue (a newer SessionEnd
# for the same path may already have superseded it, which carries its work on).
_REACHED = _ACTIVE | {"done", "error", "superseded"}


class ReleaseRefused(Exception):
    """The hold would park every event again."""


def _blocked(tpath: str | None, mb: float | None, force: bool) -> str | None:
    """Why the worker could not capture this event, or None. Checked per transcript path."""
    if not tpath:
        return "the event has no transcript path"
    if mb is None:
        return f"its transcript {tpath} cannot be read"
    limit = hold.over_mb()
    if not force and limit > 0 and mb > limit:
        return (
            f"its transcript is {mb:.0f} MB, over {hold.OVER_ENV}={limit:g}, "
            "the worker would park it again (use --force)"
        )
    return None


def _release_one(root, path, event, keep, newest_id, snapshot, queued_by_path, force):
    """Move one parked event to the ledger under the lock. Raises Timeout/OSError."""
    cid, tpath = event["capture_id"], event.get("transcript_path")
    body = {k: v for k, v in event.items() if k != FORCED}
    with FileLock(str(root / "ledger.lock"), timeout=2):
        current = snapshot.get(cid, {})
        active = current.get("status") in _ACTIVE
        if keep:
            # An event already queued is left alone unless --force must add its mark.
            if not active or (force and not forced(current)):
                for old in queued_by_path.pop(tpath, []):
                    if old["capture_id"] != cid:
                        _append(
                            root, {**old, "status": "superseded", "superseded_by": cid}
                        )
                _append(
                    root,
                    {**body, "status": "queued", **({FORCED: True} if force else {})},
                )
        elif not active:
            _append(root, {**body, "status": "superseded", "superseded_by": newest_id})
        path.unlink(missing_ok=True)


def release_held(
    large: bool = False, dry_run: bool = False, force: bool = False
) -> list[tuple[str, dict, float | None, str | None]]:
    """Re-queue every parked event once. Returns (action, event, transcript MB, why) per file.

    Actions: ``released`` (queued and read back from the ledger), ``superseded`` (an older
    event for a path whose newest one is released), ``left`` (not released, the file stays
    parked, one WARNING with the reason) and ``lost`` (moved, but the ledger read-back does
    not show it queued, one WARNING). Without ``force`` an event whose transcript is over
    the size knob is left: the worker would park it again. With ``force`` the queued event
    is marked ``release_forced`` and the worker captures it past the hold and the knob.
    A missing or unreadable transcript is always left. Spawns no worker: follow with
    ``sm lifecycle drain``. A dry run moves nothing and logs nothing.
    """
    root = capture_dir()
    source = hold.LARGE if large else hold.HELD
    events = hold.parked(root, source)
    sizes = {
        e.get("transcript_path"): hold.size_mb(e.get("transcript_path"))
        for _, e in events
    }
    if not dry_run and not force and hold.hold_on():
        raise ReleaseRefused(f"{hold.HOLD_ENV} is on, the worker would park them again")
    blocked = {tpath: _blocked(tpath, mb, force) for tpath, mb in sizes.items()}
    newest = {e.get("transcript_path"): e["capture_id"] for _, e in events}
    snapshot = {record["capture_id"]: record for record in jobs(root)}
    queued_by_path: dict = {}
    for record in snapshot.values():
        if record["status"] in _ACTIVE:
            queued_by_path.setdefault(record.get("transcript_path"), []).append(record)
    done = []
    for path, event in events:
        cid, tpath = event["capture_id"], event.get("transcript_path")
        why = blocked.get(tpath)
        if why is None:
            keep = newest[tpath] == cid
            action = "released" if keep else "superseded"
            if not dry_run:
                try:
                    _release_one(
                        root,
                        path,
                        event,
                        keep,
                        newest[tpath],
                        snapshot,
                        queued_by_path,
                        force,
                    )
                except (Timeout, OSError) as exc:
                    action, why = "left", f"{type(exc).__name__}: {exc}"
        else:
            action = "left"
        if action == "left" and not dry_run:
            log.warning(
                "Capture %s for session %s not released, left in %s: %s",
                cid,
                event.get("session_id"),
                source,
                why,
            )
        done.append((action, event, sizes.get(tpath), why))
    return done if dry_run else _read_back(root, done, force)


def _read_back(root, done, force):
    """Report ``released`` only for events the ledger now shows queued (or past it)."""
    latest = {record["capture_id"]: record for record in jobs(root)}
    checked = []
    for action, event, mb, why in done:
        if action == "released":
            record = latest.get(event["capture_id"], {})
            status = record.get("status")
            if status not in _REACHED or (
                status in _ACTIVE and force and not forced(record)
            ):
                why = f"the ledger shows {status or 'no record'} after the release"
                log.warning(
                    "Capture %s for session %s was moved but is not queued: %s",
                    event["capture_id"],
                    event.get("session_id"),
                    why,
                )
                action = "lost"
        checked.append((action, event, mb, why))
    return checked


def run_release(large: bool, dry_run: bool, force: bool) -> int:
    """CLI body shared by ``capture_queue release-held`` and ``sm lifecycle release-held``.

    Exit 0 when every event was released or superseded, 1 when the release was refused or
    any event was left or lost (a dry run exits 1 when any event would be left).
    """
    try:
        done = release_held(large, dry_run, force)
    except ReleaseRefused as exc:
        print(f"refused: {exc}. Use --force to release anyway.", file=sys.stderr)
        return 1
    for action, event, mb, why in done:
        size = "missing" if mb is None else f"{mb:.1f} MB"
        line = f"{'would ' if dry_run else ''}{action} {event.get('session_id')} {size}"
        print(f"{line}: {why}" if why else line)
    tally = {
        a: sum(d[0] == a for d in done)
        for a in ("released", "superseded", "left", "lost")
    }
    failed = tally["left"] + tally["lost"]
    summary = (
        f"{len(done)} event(s): {tally['released']} released, {tally['superseded']} "
        f"superseded, {failed} not released."
    )
    if tally["released"] and not dry_run:
        summary += " Run `sm lifecycle drain` to start a worker."
    print(summary)
    return 1 if failed else 0


RELEASE_HELP = (
    "Move parked capture events back to the queue once, one line per event. Default source "
    "is held-all/, --large uses quarantine-large/. Refused while SMARTMEMORY_CAPTURE_HOLD is "
    "on unless --force. Without --force an event over SMARTMEMORY_CAPTURE_HOLD_OVER_MB is "
    "left parked with a warning, since the worker would park it again. --force marks the "
    "events so the worker captures them past the hold and the size knob. An event whose "
    "transcript is missing is always left. Exits 1 if any event is left. --dry-run lists "
    "without moving. Starts no worker: follow with `sm lifecycle drain`."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capture_queue")
    sub = parser.add_subparsers(dest="command", required=True)
    rel = sub.add_parser("release-held", help=RELEASE_HELP, description=RELEASE_HELP)
    rel.add_argument("--large", action="store_true", help="release quarantine-large/")
    rel.add_argument("--dry-run", action="store_true", help="list, move nothing")
    rel.add_argument(
        "--force",
        action="store_true",
        help="release past the hold and the size knob, the worker captures them",
    )
    args = parser.parse_args(argv)
    return run_release(args.large, args.dry_run, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
