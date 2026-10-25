"""Durable SessionEnd acknowledgements; immutable, atomically published events."""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
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


ERROR_TEXT_MAX = 200


def requeue_errors(
    ids: list[str] | None = None, dry_run: bool = False
) -> list[tuple[str, str, str, str, str | None]]:
    """Move each ``error`` job back to ``queued`` once. Returns (action, id, old, new, error).

    Actions: ``requeued`` (written once, new status ``queued``), ``skipped`` (a matched job
    that is not ``error``: ``done``, ``queued``, ``running``, ``superseded`` or parked
    ``held``, left alone, new status = old status) and ``unknown`` (an ``--id`` that
    matches nothing). ``ids`` limits the move to those capture ids. Without ``ids`` only
    error jobs are returned, since every other job is simply not in scope. ``error`` is the
    previous error text truncated to ``ERROR_TEXT_MAX``. The ledger lock is held from the
    read to the write, so a second call (or a racing worker) finds the job already
    ``queued`` and skips it. A dry run writes nothing. Spawns no worker: follow with
    ``sm lifecycle drain``.
    """
    root = capture_dir()
    wanted = list(dict.fromkeys(ids)) if ids else None
    out: list[tuple[str, str, str, str, str | None]] = []
    with FileLock(str(root / "ledger.lock"), timeout=2):
        current = {j["capture_id"]: j for j in jobs(root)}
        parked = {e["capture_id"]: e for _, e in hold.parked(root, hold.HELD)}
        for cid in wanted if wanted is not None else list(current):
            job = current.get(cid)
            if job is None:
                if cid in parked:
                    out.append(("skipped", cid, "held", "held", None))
                elif wanted is not None:
                    out.append(("unknown", cid, "unknown", "unknown", None))
                continue
            status = job["status"]
            if status != "error":
                if wanted is not None:
                    out.append(("skipped", cid, status, status, None))
                continue
            text = (job.get("error") or "")[:ERROR_TEXT_MAX]
            if not dry_run:
                body = {
                    k: v for k, v in job.items() if k not in ("error", "recorded_at")
                }
                _append(
                    root,
                    {
                        **body,
                        "status": "queued",
                        "requeued_from": "error",
                        "previous_error": text,
                    },
                )
            out.append(("requeued", cid, "error", "queued", text))
    return out


def run_requeue_errors(ids: list[str] | None, dry_run: bool) -> int:
    """CLI body of ``sm lifecycle requeue --errors``. One line per job, exit 1 on an unknown id."""
    try:
        done = requeue_errors(ids, dry_run)
    except Timeout as exc:
        print(f"refused: the capture ledger is locked: {exc}", file=sys.stderr)
        return 1
    for action, cid, old, new, text in done:
        line = f"{'would ' if dry_run else ''}{action} {cid} {old} -> {new}"
        print(f"{line}: {text}" if text else line)
    tally = {
        a: sum(d[0] == a for d in done) for a in ("requeued", "skipped", "unknown")
    }
    summary = (
        f"{len(done)} job(s): {tally['requeued']} requeued, {tally['skipped']} "
        f"skipped, {tally['unknown']} unknown."
    )
    if tally["requeued"] and not dry_run:
        summary += " Run `sm lifecycle drain` to start a worker."
    print(summary)
    return 1 if tally["unknown"] else 0


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


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_json_atomic(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        _discard(temporary)  # a full disk leaves no half temp file behind
        raise


def _payload_session(path: str | None) -> str | None:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    value = body.get("session_id") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


_foreign_payload_roots: set[str] = set()


def _contain_payloads(root: Path, record: dict) -> None:
    """Point a record's payload files into this queue's own ``payloads/``.

    The hooks write absolute paths. A queue copied to another data dir still names the
    original's files, and reading and then unlinking them would delete another queue's data.
    Every producer writes the files next to the record (``<root>/payloads/<name>``), so
    resolve them by name.
    """
    own = root / "payloads"
    for key in ("payload_path", "state_snapshot"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            continue
        local = own / Path(value).name
        if os.path.abspath(value) == os.path.abspath(local):
            continue
        if str(root) not in _foreign_payload_roots:
            _foreign_payload_roots.add(str(root))
            log.warning(
                "Hook queue %s: a record names %s outside this queue; reading payloads from %s "
                "instead (one warning per queue)",
                root,
                value,
                own,
            )
        record[key] = str(local)


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
        _contain_payloads(root, record)
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
    """Move a queued record into running/; None when it is gone (another claimant).

    Any other ``OSError`` (disk full, permissions) propagates: the consumer hands it to
    ``hook_io_failure`` and backs off, the record stays in queue/.
    """
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


# Per-job count of failed queue file operations (claim, requeue, dead-letter, complete).
# A full disk or a permission error must not kill the one consumer: the operation is
# retried after a backoff and, past ``max_attempts``, the record moves to dead/ by a
# rename (which needs no new bytes on a full disk).
_io_failures: dict[str, int] = {}


def hook_io_failure(
    root: Path,
    job: dict,
    action: str,
    error: BaseException,
    *,
    max_attempts: int,
    dead_letter: bool = True,
) -> str:
    """Count a failed queue file operation on ``job``. Returns ``retry``, ``dead`` or ``dropped``.

    WARNING on the first failure of a job, and once more when it gives up. ``dead_letter=False``
    (the job already wrote, only its record could not be removed) gives up by leaving the record
    where it is: the next daemon start writes it again, a duplicate but never a loss.
    """
    path = Path(job["_path"])
    key = str(path)
    count = _io_failures.get(key, 0) + 1
    _io_failures[key] = count
    if count == 1:
        log.warning(
            "Hook %s job %s (session %s): %s failed, retried after a backoff: %s: %s",
            job.get("phase"),
            path.name,
            job.get("session_id"),
            action,
            type(error).__name__,
            error,
        )
    if count < max_attempts:
        return "retry"
    _io_failures.pop(key, None)
    if not dead_letter:
        log.warning(
            "Hook %s job %s (session %s) was written but its record could not be removed after %d attempts; "
            "it stays in %s and the next daemon start writes it a second time: %s",
            job.get("phase"),
            path.name,
            job.get("session_id"),
            count,
            path.parent.name,
            error,
        )
        return "dropped"
    try:
        path.rename(root / "dead" / path.name)
    except FileNotFoundError:
        return "dropped"
    except OSError as exc:
        _io_failures[key] = (
            max_attempts - 1
        )  # try the rename again after the next backoff
        log.warning(
            "Hook %s job %s (session %s) not moved to dead/ either (%s); it stays in %s and is retried",
            job.get("phase"),
            path.name,
            job.get("session_id"),
            exc,
            path.parent.name,
        )
        return "retry"
    log.warning(
        "Hook %s job %s (session %s) dead-lettered after %d failed %s attempts, its write is lost until requeued by hand: %s: %s",
        job.get("phase"),
        path.name,
        job.get("session_id"),
        count,
        action,
        type(error).__name__,
        error,
    )
    return "dead"


def hook_io_ok(job: dict) -> None:
    """A queue file operation on ``job`` succeeded: forget its failure count."""
    _io_failures.pop(job.get("_path", ""), None)


def recover_running(root: Path, *, max_attempts: int) -> int:
    """Records left in running/ by a dead worker count one attempt and go back to the queue.

    A record whose requeue cannot be written (full disk) is renamed back to queue/ as it is
    (attempt not counted); one that cannot even be renamed stays in running/ with a WARNING.
    """
    recovered = 0
    for job in hook_jobs(root, "running"):
        try:
            fail_hook_job(
                root,
                job,
                "worker exited while the job was running",
                max_attempts=max_attempts,
            )
        except OSError as exc:
            source = Path(job["_path"])
            try:
                source.rename(root / "queue" / source.name)
            except OSError as again:
                log.warning(
                    "Hook %s job %s (session %s) left in running/ by a dead worker could not be requeued "
                    "(%s, then %s); it waits there until the next start",
                    job.get("phase"),
                    source.name,
                    job.get("session_id"),
                    exc,
                    again,
                )
                continue
            log.warning(
                "Hook %s job %s (session %s) requeued from running/ without its attempt count: %s",
                job.get("phase"),
                source.name,
                job.get("session_id"),
                exc,
            )
        recovered += 1
    return recovered


def hook_counts(root: str | Path | None = None) -> dict[str, int]:
    root = hook_queue_dir(root)
    return {
        name: sum(1 for _ in (root / name).glob("*.json"))
        for name in ("queue", "running", "dead")
    }


# HOOK-SPILL-INTERIM-1 left the pre-queue hooks spilling over-cap payloads to
# ``<data dir>/hook-spill/<UTC ts>-<pid>-<phase>.json`` (envelope
# ``{"v":1,"phase","ts","pid","payload"}``, published by rename, ``.tmp-*`` while being
# written). The consumer adopts them into this queue before each scan, so they get the
# same per-session order, retry and dead-letter policy as hook jobs.

_SPILL_NAME = re.compile(r"^(\d{8}T\d{12}Z)-(\d+)-(observe|learn|distill)\.json$")


# L1: a persistent spill failure (full disk, a file that cannot be renamed) warns once per
# file and kind, and a failed write or listing backs the whole pass off (1 s doubling to
# 60 s per spill folder), so a 0.1 s consumer loop does not flood an unrotated daemon.log.
_spill_warned: set[tuple[str, str]] = set()
_spill_backoff: dict[str, tuple[float, float]] = {}  # spill dir -> (retry at, delay)
_SPILL_BACKOFF_MAX = 60.0


def _spill_warn_once(path: Path, kind: str, message: str, *args) -> None:
    key = (str(path), kind)
    if key in _spill_warned:
        log.debug(message, *args)
        return
    if len(_spill_warned) > 10000:
        _spill_warned.clear()
    _spill_warned.add(key)
    log.warning(message, *args)


def _spill_back_off(spill_dir: Path) -> None:
    _, delay = _spill_backoff.get(str(spill_dir), (0.0, 0.0))
    delay = min(_SPILL_BACKOFF_MAX, max(1.0, delay * 2))
    _spill_backoff[str(spill_dir)] = (time.monotonic() + delay, delay)


def hook_spill_dir(root: Path) -> Path:
    """The spill folder sits next to ``hook-queue`` in the data dir (the spill hooks' rule)."""
    return Path(root).parent / "hook-spill"


def _write_durable(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        _discard(temporary)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _spill_ns(stamp: str) -> int:
    moment = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ")
    return calendar.timegm(moment.timetuple()) * 10**9 + moment.microsecond * 1000


def _dead_letter_spill(root: Path, path: Path, error: str) -> None:
    _spill_warn_once(
        path,
        "malformed",
        "Hook spill file %s is malformed, moving it to %s/dead/spill-%s; its write is lost until requeued by hand: %s",
        path.name,
        root,
        path.name,
        error,
    )
    try:
        path.rename(root / "dead" / f"spill-{path.name}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        _spill_warn_once(
            path,
            "dead-rename",
            "Hook spill file %s not moved to dead/ (%s), it is skipped on every pass until it can be: %s",
            path.name,
            exc,
            error,
        )


def adopt_spill(root: Path, spill_dir: Path | None = None) -> int:
    """Move spill files into the queue in name (arrival) order. Returns files adopted.

    A spill file is deleted only after its payload and queue record are fsynced, and
    the job is named after the spill file, so a crash between the two leaves a record
    that the next pass recognises: the spill file is then deleted, never queued twice.
    No file can stop the consumer: an I/O error keeps the file for a later pass (after
    a backoff), any other error moves it to dead/ with a WARNING.
    """
    spill_dir = Path(spill_dir) if spill_dir else hook_spill_dir(root)
    retry_at, _ = _spill_backoff.get(str(spill_dir), (0.0, 0.0))
    if time.monotonic() < retry_at:
        return 0
    try:
        names = sorted(
            n
            for n in os.listdir(spill_dir)
            if n.endswith(".json") and not n.startswith(".")
        )
    except FileNotFoundError:
        return 0
    except OSError as exc:
        _spill_warn_once(
            spill_dir,
            "list",
            "Hook spill folder %s unreadable, spilled payloads wait: %s",
            spill_dir,
            exc,
        )
        _spill_back_off(spill_dir)
        return 0
    adopted = 0
    for name in names:
        path = spill_dir / name
        try:
            outcome = _adopt_one(root, spill_dir, path)
        except OSError as exc:
            _spill_warn_once(
                path,
                "io",
                "Hook spill file %s not adopted, it stays in %s for a later pass: %s",
                name,
                spill_dir,
                exc,
            )
            _spill_back_off(spill_dir)
            break
        except Exception as exc:  # noqa: BLE001 - a poison file must never stop the consumer
            _dead_letter_spill(root, path, f"{type(exc).__name__}: {exc}")
            continue
        if outcome:
            adopted += 1
    else:
        _spill_backoff.pop(str(spill_dir), None)
    if adopted:
        log.info(
            "Adopted %d hook spill file(s) from %s into the hook queue",
            adopted,
            spill_dir,
        )
    return adopted


def _adopt_one(root: Path, spill_dir: Path, path: Path) -> bool:
    """Queue one spill file; True when it was adopted. Raises ``OSError`` to keep it for later."""
    name = path.name
    match = _SPILL_NAME.match(name)
    if match is None:
        _dead_letter_spill(
            root, path, "spill file name does not match <ts>-<pid>-<phase>.json"
        )
        return False
    stamp, pid, phase = match.groups()
    try:
        queued_ns = _spill_ns(stamp)
    except ValueError as exc:
        _dead_letter_spill(root, path, f"spill file name has no valid UTC time: {exc}")
        return False
    job_name = f"spill-{stamp}-{pid}.{phase}.json"
    if any((root / sub / job_name).exists() for sub in ("queue", "running", "dead")):
        try:
            path.unlink(missing_ok=True)  # adopted before a crash; its job is queued
        except OSError as exc:
            _spill_warn_once(
                path,
                "guard-unlink",
                "Hook spill file %s is already queued as %s but cannot be deleted, it is skipped: %s",
                name,
                job_name,
                exc,
            )
        return False
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _dead_letter_spill(root, path, f"unreadable spill file: {exc}")
        return False
    if (
        not isinstance(envelope, dict)
        or envelope.get("v") != 1
        or envelope.get("phase") != phase
        or "payload" not in envelope
    ):
        _dead_letter_spill(
            root, path, "spill envelope is not v1 with this phase and a payload"
        )
        return False
    payload = envelope["payload"]
    body = payload if isinstance(payload, dict) else {}
    session = body.get("session_id")
    cwd = body.get("cwd")
    payload_path = root / "payloads" / job_name
    record = {
        "v": 1,
        "phase": phase,
        "session_id": session if isinstance(session, str) and session else None,
        "cwd": cwd if isinstance(cwd, str) else None,
        "workspace_id": None,
        "payload_path": str(payload_path),
        "state_snapshot": None,
        "queued_ns": queued_ns,
        "spill": name,
    }
    payload_text = json.dumps(
        payload
    )  # a non-serialisable payload dead-letters before any write
    _write_durable(
        payload_path, payload_text
    )  # OSError: the file stays for a later pass
    _write_durable(root / "queue" / job_name, json.dumps(record) + "\n")
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _spill_warn_once(
            path,
            "unlink",
            "Hook spill file %s is queued as %s but was not deleted; if its job finishes first, "
            "the next pass queues it a second time: %s",
            name,
            job_name,
            exc,
        )
    if phase == "distill":
        log.warning(
            "Spilled distill %s adopted without a prompt snapshot (the spilling hook took none); "
            "its response is stored unpaired with its prompt",
            name,
        )
    return True


if __name__ == "__main__":
    raise SystemExit(main())
