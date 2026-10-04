"""Read local status without loading models or constructing a memory store."""

import logging
import sqlite3
from contextlib import closing

from smartmemory_app.storage import _resolve_data_dir

log = logging.getLogger(__name__)


def local_memory_count() -> int | None:
    """Count persisted graph nodes, excluding internal versions as health does.

    A missing store or a work-graph-only database is empty. An unreadable store
    is unknown, never an empty count. Read-only mode avoids creating or migrating
    a store just to report status.
    """
    try:
        path = (_resolve_data_dir() / "memory.db").expanduser().resolve()
        if not path.exists():
            return 0
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        ) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone():
                return 0
            return db.execute(
                "SELECT COUNT(*) FROM nodes WHERE memory_type IS NULL OR memory_type != 'Version'"
            ).fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        log.warning(
            "Local memory count unavailable. Saved memories could not be read: %s", exc
        )
        return None
