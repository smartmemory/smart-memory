"""Incremental transcript capture: per-transcript byte offsets and bounded tail slices.

A Claude Code transcript is append-only, so each capture only needs the bytes written
since the last one. Nothing here loads a whole transcript: slices are streamed to a
temporary file of at most ``CAPTURE_MAX_MB`` and ended on a line boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DEFAULT_MAX_MB = 50.0
DEFAULT_MAX_RSS_MB = 1024.0
_SKIP_BLOCK = 1024 * 1024
_rss_unreadable_logged = False


@dataclass(frozen=True)
class Slice:
    start: int
    end: int
    path: Path


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        log.warning("%s=%r is not a positive number, using %s", name, raw, default)
        return default
    return value


def max_bytes() -> int:
    return max(1, int(_env_number("CAPTURE_MAX_MB", DEFAULT_MAX_MB) * 1024 * 1024))


def max_rss_mb() -> float:
    return _env_number("CAPTURE_MAX_RSS_MB", DEFAULT_MAX_RSS_MB)


def rss_mb(pid: int | None = None) -> float | None:
    """Resident size in MB from ``ps`` (psutil is not a declared dependency)."""
    global _rss_unreadable_logged
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid or os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return int(out.stdout.strip()) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        if not _rss_unreadable_logged:
            _rss_unreadable_logged = True
            log.warning("Capture cannot read its own RSS, the memory guard is off")
        return None


def _offset_file(root: Path, transcript: str) -> Path:
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    return root / "offsets" / f"{digest}.json"


def read_offset(root: Path, transcript: str) -> int | None:
    try:
        record = json.loads(_offset_file(root, transcript).read_text())
        return int(record["offset"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "Capture offset for %s is unreadable, treating as none: %s", transcript, exc
        )
        return None


def write_offset(root: Path, transcript: str, offset: int) -> None:
    """Atomic: write a temp file, then rename over the old offset."""
    target = _offset_file(root, transcript)
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"path": transcript, "offset": offset}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _skip_rest_of_line(src) -> None:
    while True:
        block = src.readline(_SKIP_BLOCK)
        if not block or block.endswith(b"\n"):
            return


def iter_slices(
    transcript: str, start: int, limit: int, cap: int, root: Path
) -> Iterator[Slice]:
    """Yield byte ranges of ``transcript[start:limit]``, each at most ``cap`` bytes.

    Each slice is copied to a temp file laid out like the original (same parent and file
    name, so the parser derives the same title) and deleted when the consumer moves on.
    A single record longer than ``cap`` is skipped with one WARNING.
    """
    source = Path(transcript)
    with source.open("rb") as src:
        src.seek(start)
        position = start
        while position < limit:
            folder = root / "slices" / uuid.uuid4().hex / source.parent.name
            folder.mkdir(parents=True)
            target = folder / source.name
            written = 0
            try:
                with target.open("wb") as out:
                    while position < limit and written < cap:
                        room = min(cap - written, limit - position)
                        line = src.readline(room)
                        if not line:  # The file shrank under us: stop at its real end.
                            limit = position = src.tell()
                            break
                        complete = line.endswith(b"\n") or position + len(line) >= limit
                        if not complete and len(line) == room:
                            if written:  # Leave it for the next slice.
                                src.seek(position)
                                break
                            log.warning(
                                "Transcript %s has a record over CAPTURE_MAX_MB at byte %d, skipped",
                                transcript,
                                position,
                            )
                            _skip_rest_of_line(src)
                            position = min(src.tell(), limit)
                            continue
                        position += len(line)
                        out.write(line)
                        written += len(line)
                yield Slice(start, position, target)
            finally:
                shutil.rmtree(folder.parent, ignore_errors=True)
            start = position
