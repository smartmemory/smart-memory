"""Parse a local checkout for the hosted code-index replacement contract."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict
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
    """Return a complete wire body and IndexResult without opening a local store.

    Traversal and parse failures refuse replacement of an incomplete checkout.
    Reuse core's parsers and cross-file resolver, with parser-local relation IDs:
    the hosted route remaps those IDs into its authenticated workspace.
    """
    from smartmemory.code.indexer import CodeIndexer
    from smartmemory.code.models import IndexResult
    from smartmemory.code.parser import DEFAULT_EXCLUDE_DIRS as PYTHON_EXCLUDE_DIRS
    from smartmemory.code.parser import parse_file
    from smartmemory.code.ts_parser import DEFAULT_EXCLUDE_DIRS as TS_EXCLUDE_DIRS
    from smartmemory.code.ts_parser import TS_EXTENSIONS

    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(f"Code checkout is not a directory: {root}")
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a non-empty string")
    languages = ["python"] if languages is None else languages
    if not languages or set(languages) - {"python", "typescript"}:
        raise ValueError("Hosted code index supports only python and typescript")
    exclusions = set(exclude_dirs) if exclude_dirs is not None else None
    files = []
    traversal_errors: list[OSError] = []
    # Match core's collection filters, but fail closed for hosted replacement.
    # The local collectors intentionally remain unchanged.
    for language, defaults in (
        ("python", PYTHON_EXCLUDE_DIRS),
        ("typescript", TS_EXCLUDE_DIRS),
    ):
        if language not in languages:
            continue
        excluded = defaults if exclusions is None else exclusions
        language_files = []
        for directory_path, dirs, names in os.walk(
            root, onerror=traversal_errors.append
        ):
            dirs[:] = [
                name
                for name in dirs
                if name not in excluded and not name.endswith(".egg-info")
            ]
            for name in names:
                matches = (
                    name.endswith(".py")
                    if language == "python"
                    else Path(name).suffix.lower() in TS_EXTENSIONS
                )
                if matches:
                    language_files.append(os.path.join(directory_path, name))
        files.extend(sorted(language_files))
    if traversal_errors:
        failures = "; ".join(sorted({str(error) for error in traversal_errors}))
        message = (
            "Hosted code replacement refused: incomplete directory traversal. "
            f"Prior index was not changed. Failed paths: {failures}"
        )
        log.warning("%s", message)
        raise ValueError(message)

    result = IndexResult(repo=repo)
    entities, relations, imports = [], [], []
    for file in files:
        # Refuse symlinked files outside the checkout. No absolute paths or '..'
        # escape paths may become hosted entity identities.
        Path(file).resolve().relative_to(root)
        parsed = parse_file(file, repo=repo, repo_root=str(root))
        result.errors.extend(parsed.errors)
        if parsed.errors and not parsed.entities:
            result.files_skipped += 1
        else:
            result.files_parsed += 1
        entities.extend(parsed.entities)
        relations.extend(parsed.relations)
        imports.extend(parsed.import_symbols)
    if result.errors:
        log.warning(
            "Hosted code replacement refused: %d parse errors", len(result.errors)
        )
        raise ValueError(
            f"Hosted code replacement refused: {len(result.errors)} parse error(s). "
            f"Prior index was not changed. First error: {result.errors[0]}"
        )
    if not entities:
        raise ValueError(
            "Hosted code replacement refused: 0 entities. Prior index was not changed."
        )

    resolver = CodeIndexer(graph=None, repo=repo, repo_root=str(root))
    symbols = resolver._build_symbol_table(entities, imports)
    relations = resolver._resolve_cross_file_calls(relations, symbols)
    entity_ids = {entity.item_id for entity in entities}
    valid_relations = [
        rel
        for rel in relations
        if rel.source_id in entity_ids and rel.target_id in entity_ids
    ]
    if len(valid_relations) != len(relations):
        log.warning(
            "Hosted code index omitted %d unresolved relations with absent endpoints "
            "(matching the local indexer's edge validation)",
            len(relations) - len(valid_relations),
        )

    if commit_hash is None:
        try:
            git = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            commit_hash = git.stdout.strip() if git.returncode == 0 else ""
            if not commit_hash:
                log.warning(
                    "Hosted code index has no commit hash: git rev-parse HEAD failed"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Hosted code index has no commit hash: %s", exc)
            commit_hash = ""

    result.entities = entities
    result.commit_hash = commit_hash
    fields = (
        "name",
        "entity_type",
        "file_path",
        "line_number",
        "docstring",
        "decorators",
        "bases",
        "http_method",
        "http_path",
    )
    body = {
        "repo": repo,
        "entities": [
            {field: getattr(entity, field) for field in fields} for entity in entities
        ],
        "relations": [asdict(rel) for rel in valid_relations],
        "commit_hash": commit_hash,
    }
    # Measure exactly the bytes the existing httpx helper will send.
    encoded = httpx.Request("POST", "https://unused.invalid", json=body).content
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ValueError(
            f"Hosted code replacement refused: request is {len(encoded)} bytes, "
            f"exceeding MAX_REQUEST_BODY_BYTES={MAX_REQUEST_BODY_BYTES} (service default). "
            "This replacement endpoint cannot safely accept chunks. Prior index was not changed."
        )
    return body, result
