"""Durable SessionEnd acknowledgements; immutable, atomically published events."""

from __future__ import annotations

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

    A bare rename: no bytes are written, so a full disk cannot fail the claim and the file's
    mtime survives (``hook_jobs`` derives ``queued_ns`` from it, and ``fail_hook_job`` persists
    it on the first requeue). Any other ``OSError`` (permissions) propagates: the consumer
    hands it to ``hook_io_failure`` and backs off, the record stays in queue/.
    """
    source = Path(job["_path"])
    target = root / "running" / source.name
    try:
        source.rename(target)
    except FileNotFoundError:
        return None
    return {**_strip(job), "_path": str(target)}


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


def requeue_dead_hooks(
    root: str | Path | None = None,
    names: list[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """Move hook ``dead/`` records back to ``queue/`` with a fresh attempt count.

    Returns (action, file name, detail). ``requeued`` keeps ``queued_ns`` so the job takes its
    old place in its session's order. ``skipped`` covers a raw ``spill-*`` file (a malformed spill
    envelope, not a job; an adopted spill job requeues like any other), a record with no known
    phase, a payload that is gone, and a name that is already in queue/ or running/. ``unknown`` is a requested name not in dead/. ``names``
    limits the move (file names, ``.json`` optional).
    """
    root = hook_queue_dir(root)
    dead = {p.name: p for p in (root / "dead").glob("*.json")}
    wanted = (
        [n if n.endswith(".json") else f"{n}.json" for n in dict.fromkeys(names)]
        if names
        else sorted(dead)
    )
    out: list[tuple[str, str, str]] = []
    for name in wanted:
        path = dead.get(name)
        if path is None:
            out.append(("unknown", name, "not in dead/"))
            continue
        is_spill = name.startswith("spill-")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            detail = (
                "malformed spill file, not a job record"
                if is_spill
                else f"unreadable record: {exc}"
            )
            out.append(("skipped", name, detail))
            continue
        # ``spill-`` is also the name of an adopted spill job (``_adopt_one``), a real job record
        # that carries ``spill`` and ``payload_path``; only a raw envelope dead-lettered by
        # ``_dead_letter_spill`` lacks them.
        if is_spill and not (
            isinstance(record, dict)
            and isinstance(record.get("spill"), str)
            and record.get("payload_path")
        ):
            out.append(("skipped", name, "malformed spill file, not a job record"))
            continue
        if not isinstance(record, dict) or record.get("phase") not in HOOK_PHASES:
            out.append(("skipped", name, "record has no known phase"))
            continue
        _contain_payloads(root, record)
        missing = [
            k
            for k in ("payload_path", "state_snapshot")
            if record.get(k) and not Path(record[k]).exists()
        ]
        if missing:
            out.append(("skipped", name, f"{', '.join(missing)} is gone"))
            continue
        if any((root / sub / name).exists() for sub in ("queue", "running")):
            out.append(("skipped", name, "already in queue/ or running/"))
            continue
        if not dry_run:
            for key in ("attempts", "last_error", "failed_at"):
                record.pop(key, None)
            record.setdefault("queued_ns", path.stat().st_mtime_ns)
            _write_json_atomic(root / "queue" / name, record)
            path.unlink(missing_ok=True)
        out.append(("requeued", name, str(record.get("session_id") or "")))
    return out


def run_requeue_dead_hooks(
    names: list[str] | None, dry_run: bool, root: str | Path | None = None
) -> int:
    """CLI body of ``sm lifecycle requeue-hooks``. One line per record, exit 1 on an unknown name."""
    done = requeue_dead_hooks(root, names, dry_run)
    for action, name, detail in done:
        print(f"{'would ' if dry_run else ''}{action} {name}: {detail}")
    tally = {
        a: sum(d[0] == a for d in done) for a in ("requeued", "skipped", "unknown")
    }
    print(
        f"{len(done)} record(s): {tally['requeued']} requeued, {tally['skipped']} "
        f"skipped, {tally['unknown']} unknown."
    )
    return 1 if tally["unknown"] else 0


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
