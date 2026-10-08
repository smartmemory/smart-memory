"""Parse a local checkout for the hosted code-index replacement contract."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from smartmemory.code.models import IndexResult

# Service default (service_common.security.production_security). Deployments may
# set a lower MAX_REQUEST_BODY_BYTES, whose HTTP 413 is surfaced by the caller.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024


def prepare_code_index(
    directory: str,
    repo: str,
    commit_hash: str | None,
    exclude_dirs: list[str] | None,
    languages: list[str] | None,
) -> tuple[dict, IndexResult]:
    """Return a diagnostic wire body and IndexResult without opening a local store.

    Traversal and hard parse failures refuse replacement. Recoverable grammar partials
    travel with contracted diagnostics, without satisfying G16 publication.
    Reuse core's parsers and cross-file resolver, with parser-local relation IDs:
    the hosted route remaps those IDs into its authenticated workspace.
    """
    from smartmemory.code.indexer import CodeIndexer
    from smartmemory.code.repo_key import derive_repo_identity
    from smartmemory.code.source_revision import detect_source_revision

    root = Path(directory).resolve()
    if commit_hash is None:
        # CODE-INDEXER-HARDEN-1 F29: HEAD plus the working-tree state, so uncommitted
        # code is stamped "<HEAD>-dirty-<fingerprint>" instead of passing as HEAD.
        revision = detect_source_revision(str(root))
        commit_hash = revision.commit_hash
        if not revision.head:
            log.warning(
                "Hosted code index has no commit hash: git rev-parse HEAD failed"
            )
        if revision.dirty:
            log.warning(
                "Hosted code index of %s includes uncommitted changes; stamped %s",
                root,
                commit_hash,
            )

    # CODE-INGEST-SURFACES-1/code-contract.json: dataclass serialization carries
    # every core model field instead of a surface-specific field whitelist.
    indexer = CodeIndexer(
        graph=None,
        repo=repo,
        repo_root=str(root),
        exclude_dirs=set(exclude_dirs) if exclude_dirs is not None else None,
        commit_hash=commit_hash or "",
    )
    body, result = indexer.prepare_bundle(languages)
    # CODE-INDEXER-HARDEN-1 F28: the service refuses this repo name if it already
    # belongs to a different checkout identity in the workspace.
    body["repo_identity"] = derive_repo_identity(str(root)).identity
    encoded = httpx.Request("POST", "https://unused.invalid", json=body).content
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ValueError(
            f"Hosted code replacement refused: request is {len(encoded)} bytes, "
            f"exceeding MAX_REQUEST_BODY_BYTES={MAX_REQUEST_BODY_BYTES} (service default). "
            "This replacement endpoint cannot safely accept chunks. Prior index was not changed."
        )
    return body, result
