"""Shared file reset that reports retained storage instead of claiming success."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def remove_store_files(data_path: Path) -> int:
    """Delete storage files once each, keeping OS lock files in place."""
    files = {
        path
        for pattern in (
            "*.db",
            "*.db-shm",
            "*.db-wal",
            "*.db-journal",
            "*.usearch",
            "*.json",
            "*.jsonl",
        )
        for path in data_path.glob(pattern)
    }
    failed = []
    removed = 0
    for path in sorted(files):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Clear retained %s: %s", path.name, exc)
            failed.append(path.name)
    if failed:
        raise RuntimeError(
            f"Clear incomplete: removed {removed} files, retained {', '.join(failed)}. "
            "Stop SmartMemory workers and disconnect MCP clients before retrying."
        )
    return removed
