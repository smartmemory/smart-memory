"""Parking lots for capture events: ``held-all/`` (the hold) and ``quarantine-large/`` (size).

An event is one JSON file with the ledger record's keys, status ``queued``, so releasing it
is a plain re-queue. Nothing here imports the model stack or reads a transcript: the checks
only ``stat`` a file. One parked event per transcript path, the newer replaces the older.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

HELD = "held-all"
LARGE = "quarantine-large"
HOLD_ENV = "SMARTMEMORY_CAPTURE_HOLD"
OVER_ENV = "SMARTMEMORY_CAPTURE_HOLD_OVER_MB"
DEFAULT_OVER_MB = 50.0
_OFF = {"0", "false", "no", "off"}
_FIELDS = (
    "capture_id",
    "session_id",
    "transcript_path",
    "workspace_id",
    "cwd",
    "queued_at",
)


def hold_on() -> bool:
    """Default ON. Only an explicit 0/false/no/off turns it off, so a typo keeps the hold."""
    raw = os.environ.get(HOLD_ENV, "")
    return not raw.strip() or raw.strip().lower() not in _OFF


def over_mb() -> float:
    """Size limit in MB for a claimed event's transcript. 0 means off."""
    raw = os.environ.get(OVER_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_OVER_MB
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0:
        log.warning("%s=%r is not a number, using %s", OVER_ENV, raw, DEFAULT_OVER_MB)
        return DEFAULT_OVER_MB
    return value


def size_mb(path: str | None) -> float | None:
    """Transcript size by ``stat`` only. None when it cannot be read."""
    try:
        return os.stat(path).st_size / (1024 * 1024)
    except (OSError, TypeError, ValueError):
        return None


def over_limit(path: str | None) -> float | None:
    """The transcript's MB when it is over the size knob, else None."""
    limit = over_mb()
    size = size_mb(path)
    return size if limit > 0 and size is not None and size > limit else None


def parked(root: Path, dirname: str) -> list[tuple[Path, dict]]:
    """Parked events oldest first. An unreadable file is skipped with a WARNING."""
    events = []
    for path in sorted((root / dirname).glob("*.json")):
        try:
            events.append((path, json.loads(path.read_text())))
        except (OSError, ValueError) as exc:
            log.warning("Parked capture %s is unreadable, left in place: %s", path, exc)
    return events


def park(root: Path, record: dict, dirname: str) -> Path:
    """Atomically publish ``record`` in ``dirname``, then drop older events for its path.

    Caller holds ledger.lock, so two parks of one path cannot both delete each other. The
    new file is durable before the old one goes: a crash leaves a duplicate, never a gap.
    """
    folder = root / dirname
    folder.mkdir(exist_ok=True)
    body = {k: record.get(k) for k in _FIELDS}
    body.update(status="queued", recorded_at=time.time())
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
    temporary = folder / f".{name}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(body, handle)
        handle.flush()
        os.fsync(handle.fileno())
    final = folder / f"{name}.json"
    temporary.rename(final)
    # Windows cannot fsync a directory, the rename is durable there.
    if os.name != "nt":
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if body["transcript_path"]:
        for path, old in parked(root, dirname):
            if path != final and old.get("transcript_path") == body["transcript_path"]:
                path.unlink(missing_ok=True)
    return final
