"""Watched local folder as memory (CONN-LOCAL-FS-1): the local runner that drives ``smartmemory-connectors``' ``local_fs``.

Everything runs on this machine against the local daemon. The connector and the runner contract (checkpoint per record,
content-hash dedupe, deletion after two complete snapshots) live in ``smartmemory_connectors``; this module only supplies the
sink (the daemon's ``/ingest`` and ``DELETE /{id}``), the PDF text extractor and the state-file location.

``smartmemory-connectors`` is imported lazily: the wrapper does not depend on it yet (optional extra ``folders``).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ORIGIN = "import:live:local_fs"
Request = Callable[..., Any]


class FolderSyncError(Exception):
    """A fixed-text failure: the message never carries a path, a note's text or a vendor string."""


def _missing_connectors() -> FolderSyncError:
    return FolderSyncError(
        "Folder sync needs the smartmemory-connectors package: pip install smartmemory-connectors"
    )


def pdf_extractor() -> Callable[[bytes], str] | None:
    """A ``.pdf`` text extractor when ``pypdf`` is installed, else ``None`` (PDFs are then reported, not listed)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    def extract(raw: bytes) -> str:
        return "\n\n".join(
            (page.extract_text() or "") for page in PdfReader(io.BytesIO(raw)).pages
        )

    return extract


def state_path(root: str, data_dir: str | None = None) -> Path:
    base = Path(
        os.path.expanduser(
            data_dir or os.environ.get("SMARTMEMORY_DATA_DIR") or "~/.smartmemory"
        )
    )
    tag = hashlib.sha256(
        os.path.realpath(os.path.expanduser(root)).encode()
    ).hexdigest()[:12]
    return base / "folders" / f"{tag}.json"


class DaemonSink:
    """``RecordSink`` over the local daemon. ``request(method, path, **kw)`` is the CLI's ``_daemon_request`` (None = down)."""

    def __init__(self, request: Request) -> None:
        self._request = request

    def write(self, record: Any, prior_item_id: str | None) -> Any:
        from smartmemory_connectors.local_runner import SinkResult

        if prior_item_id:  # the daemon has no update-by-key: replace = delete the old item, then ingest (DIST-OBSIDIAN-LITE-1)
            self._delete(prior_item_id)
        props = {
            "source_path": record.source_path,
            "record_key": record.record_key,
            "source_version": record.version,
            **{
                k: v
                for k, v in record.metadata.items()
                if isinstance(v, (str, int, float, bool))
            },
        }
        out = self._request(
            "POST",
            "/ingest",
            json={
                "content": record.text,
                "memory_type": "episodic",
                "context": {"origin": ORIGIN},
                "properties": props,
            },
        )
        if not isinstance(out, dict) or not out.get("item_id"):
            raise FolderSyncError("The daemon did not accept the record.")
        return SinkResult(
            str(out["item_id"]), "replaced" if prior_item_id else "created"
        )

    def archive(self, item_id: str, reason: str) -> None:
        # Core's archive() only sets metadata and recall does not filter on it (design-v2 Q4, unproven), so the local sink
        # removes the item: a deleted note must stop appearing in answers.
        self._delete(item_id)

    def _delete(self, item_id: str) -> None:
        import click

        try:
            out = self._request("DELETE", f"/{item_id}")
        except click.ClickException as exc:
            if "not found" in str(exc.message).lower():
                return
            raise FolderSyncError("The daemon could not remove the item.") from None
        if out is None:
            raise FolderSyncError("The daemon is not running.")


def sync_folder(
    root: str,
    *,
    preset: str,
    request: Request,
    reconcile_every: float = 3600,
    state_file: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    extractors: dict[str, Callable[[bytes], str]] | None = None,
) -> Any:
    """One runner pass over ``root``. Returns the ``RunReport``."""
    try:
        from smartmemory_connectors.connectors.local_fs import (
            LocalFsConnector,
            preset_config,
        )
        from smartmemory_connectors.local_runner import JsonCheckpointStore, LocalRunner
    except ImportError:
        raise _missing_connectors() from None
    root = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise FolderSyncError("The folder does not exist or is not a directory.")
    try:
        cfg = preset_config(preset, root)
    except ValueError:
        raise FolderSyncError(
            "Unknown preset: use markdown, logseq or apple_notes."
        ) from None
    exts = {".pdf": pdf_extractor()} if extractors is None else extractors
    conn = LocalFsConnector(
        cfg, log_paths=False, extractors={k: v for k, v in exts.items() if v}
    )
    store = JsonCheckpointStore(state_file or state_path(root))
    runner = LocalRunner(
        conn,
        DaemonSink(request),
        store,
        resource="files",
        reconcile_every=reconcile_every,
    )
    return runner.run_once(now or datetime.now(timezone.utc))
