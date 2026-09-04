"""Cross-process generation marker for the replaceable lite SQLite store."""

from __future__ import annotations

import uuid
from pathlib import Path

_MARKER_NAME = ".store-generation"


def _marker_path() -> Path:
    from smartmemory_app.storage import _resolve_data_dir

    return _resolve_data_dir() / _MARKER_NAME


def read_store_generation() -> str | None:
    """Return the current store generation, or ``None`` on an older install."""
    try:
        value = _marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def bump_store_generation() -> str:
    """Atomically publish a new store generation after destructive replacement."""
    marker = _marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    temporary = marker.with_name(f"{marker.name}.{generation}.tmp")
    temporary.write_text(generation, encoding="utf-8")
    temporary.replace(marker)
    return generation
