"""HOOK-RECALL-RELEVANCE-1: Pure formatter, dedup, workspace derivation, JSONL trace.

This module is intentionally I/O-free except for `_trace()` (filesystem append).
All recall-path bug fixes (empty-bucket suppress, dedup, top_k cap) live in
`format_recall_lines()` so both local (`storage.recall`) and remote
(`remote_backend.RemoteMemory.recall`) paths converge on the same formatter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextvars import ContextVar
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

DEFAULT_TRACE_PATH = Path(
    os.environ.get(
        "SMARTMEMORY_HOOK_TRACE",
        str(Path.home() / ".smartmemory" / "hook-recall.jsonl"),
    )
)
TRACE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotate threshold


# --- Item adapter -----------------------------------------------------------


def _item_to_recall_dict(item: Any) -> dict:
    """Adapt a MemoryItem (or dict) to the uniform shape the formatter expects.

    Local path returns MemoryItem objects; remote path returns dicts. Normalize.
    """
    if isinstance(item, dict):
        return {
            "item_id": item.get("item_id") or item.get("id"),
            "memory_type": item.get("memory_type", "?"),
            "content": item.get("content", ""),
            "origin": item.get("origin", ""),
            "confidence": item.get("confidence", 1.0),
            "stale": item.get("stale", False),
            "metadata": item.get("metadata") or {},
        }
    metadata = getattr(item, "metadata", None) or {}
    return {
        "item_id": getattr(item, "item_id", None),
        "memory_type": getattr(item, "memory_type", "?"),
        "content": getattr(item, "content", "") or "",
        "origin": getattr(item, "origin", "") or "",
        "confidence": getattr(item, "confidence", 1.0),
        "stale": getattr(item, "stale", False),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


# --- Formatter --------------------------------------------------------------


def recall_item_label(item: dict) -> str:
    """Identify discarded rows without logging their potentially private content."""
    return str(
        item.get("item_id")
        or "content-sha256:"
        + hashlib.sha256((item.get("content") or "").encode("utf-8")).hexdigest()[:16]
    )


def format_recall_lines(
    items: Iterable[dict], top_k: int, budget: int | None = None
) -> str:
    """Format items as the `## SmartMemory Context` block.

    Bug fixes vs legacy storage.recall():
      - empty/whitespace content suppressed
      - dedup by item_id (then lowercased full content within memory type)
      - top_k cap applied AFTER dedup, not before

    Returns "" if no items survive filtering.
    """
    seen_ids: set[str] = set()
    seen_bodies: set[tuple[str, str]] = set()
    lines: list[str] = []

    for it in items:
        body = (it.get("content") or "").strip()
        if not body:
            log.warning("recall dropped %s: empty content", recall_item_label(it))
            continue

        iid = it.get("item_id")
        if not iid:
            log.warning(
                "recall lost stored item ID; using stable content label %s",
                recall_item_label(it),
            )
        if iid and iid in seen_ids:
            log.warning("recall dropped %s: duplicate item_id", recall_item_label(it))
            continue
        mtype = it.get("memory_type", "?") or "?"
        body_key = (mtype, body.lower())
        if body_key in seen_bodies:
            log.warning(
                "recall dropped %s: duplicate content within memory type %s",
                recall_item_label(it),
                mtype,
            )
            continue
        if iid:
            seen_ids.add(iid)
        seen_bodies.add(body_key)

        if len(lines) >= top_k:
            log.warning(
                "recall dropped %s: top_k cap (%d)", recall_item_label(it), top_k
            )
            continue

        conf = it.get("confidence", 1.0)
        try:
            conf_marker = "~" if float(conf) < 0.5 else ""
        except (TypeError, ValueError):
            log.warning(
                "recall lost confidence marker for %s: invalid confidence",
                recall_item_label(it),
            )
            conf_marker = ""
        stale_marker = "⚠" if it.get("stale") else ""
        mtype = it.get("memory_type", "?") or "?"

        body = body.replace("\n", "\n  ")
        lines.append(
            f"- {stale_marker}{conf_marker}[{mtype}] [mem:{recall_item_label(it)}] {body}"
        )

    if not lines:
        return ""
    block = "## SmartMemory Context\n" + "\n".join(lines)
    return budget_blocks([(block, None)], budget) if budget is not None else block


def payload_ids(payload: str) -> list[str]:
    """IDs on rendered item headers, in injection order."""
    return re.findall(r"(?m)^- [^\n]*?\[mem:([^\]]+)\]", payload)


def budget_blocks(blocks: list[tuple[str, str | None]], budget: int) -> str:
    """Keep complete ranked items, accounting for headers and separators too.

    Formatter continuation lines are indented, so a multiline memory remains
    atomic. Only an item larger than the entire phase allowance can be shortened.
    """
    limit = max(0, budget) * 4
    output = ""
    seen: set[str] = set()
    for block, heading in blocks:
        if not block:
            continue
        chunks = re.split(r"(?m)^(?=- )", block)
        header = heading or chunks[0].strip()
        entries = chunks[1:]
        if not entries:  # Legacy unstructured backend response: still atomic.
            log.warning(
                "recall lost item IDs and item boundaries in legacy unstructured block"
            )
            if len(output + block) <= limit:
                output += block
            else:
                log.info("recall dropped legacy block for budget")
            continue
        section_started = False
        for entry in entries:
            entry = entry.rstrip()
            ids = payload_ids(entry)
            iid = ids[0] if ids else recall_item_label({"content": entry})
            if iid in seen:
                log.warning("recall dropped %s: duplicate injected item", iid)
                continue
            seen.add(iid)
            prefix = ("\n" if output else "") + (
                "" if section_started else header + "\n"
            )
            if len(output + prefix + entry) > limit:
                if len(header + "\n" + entry) > limit:
                    marker = f"…[truncated, mem:{iid}]"
                    available = limit - len(output + prefix) - len(marker)
                    # Preserve the tags, and cut only after a complete sentence.
                    tag_end = entry.find("] ", entry.find("[mem:")) + 2 if ids else 0
                    ends = [m.end() for m in re.finditer(r"[.!?。！？](?=\s|$)", entry)]
                    ends = [end for end in ends if tag_end < end <= available]
                    if ends:
                        entry = entry[: ends[-1]] + marker
                    elif tag_end and tag_end <= available:
                        entry = entry[:tag_end] + marker
                    else:
                        log.info("recall dropped %s for budget", iid)
                        continue
                    log.warning(
                        "recall lost full content for %s: oversized item truncated at sentence boundary",
                        iid,
                    )
                else:
                    log.info("recall dropped %s for budget", iid)
                    continue
            output += prefix + entry
            section_started = True
    return output


# Context-local aggregation keeps storage diagnostics in the one lifecycle trace.
_ACTIVE_TRACE: ContextVar[dict | None] = ContextVar(
    "hook_injection_trace", default=None
)


def record_hook_error(message: str, error: Exception | str) -> None:
    """Surface a fallback and retain its loss in the current injection trace."""
    detail = f"{message}: {error}"
    log.warning("%s", detail)
    active = _ACTIVE_TRACE.get()
    if active is not None:
        active["errors"].append(detail)


def skip_injection(reason: str) -> None:
    active = _ACTIVE_TRACE.get()
    if active is not None:
        active["skipped_reason"] = reason


# --- Workspace derivation ---------------------------------------------------


def derive_workspace_id(cwd: str | None) -> str | None:
    """Derive a stable workspace_id from cwd.

    Strategy:
      1. SMARTMEMORY_WORKSPACE_ID env var wins.
      2. Otherwise: realpath(cwd) → optional `git rev-parse --show-toplevel` →
         realpath again → sha1[:12], prefixed `ws_`.
      3. None cwd returns None.

    Symlinked paths collapse to the same id as the real path.
    """
    env = os.environ.get("SMARTMEMORY_WORKSPACE_ID")
    if env:
        return env
    if not cwd:
        return None

    canonical = os.path.realpath(cwd)
    try:
        result = subprocess.run(
            ["git", "-C", canonical, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        toplevel = result.stdout.strip()
        if toplevel:
            canonical = os.path.realpath(toplevel)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        log.warning(
            "workspace lost git-root resolution; using cwd %s: %s", canonical, exc
        )

    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return f"ws_{digest}"


# --- Trace -------------------------------------------------------------------


def _trace(
    *,
    phase: str,
    workspace_id: str | None,
    cwd: str | None,
    query: str | None,
    candidate_count: int,
    emitted: int,
    snapshot_used: bool,
    latency_ms: int,
    trace_path: Path | None = None,
    session_id: str | None = None,
    ranked_ids: list[str] | None = None,
    payload: str = "",
    error: str | None = None,
    skipped_reason: str | None = None,
) -> None:
    """Append one JSONL line per hook invocation. Never raises."""
    active = _ACTIVE_TRACE.get()
    if active is not None:
        active["ranked_ids"].extend(ranked_ids or payload_ids(payload))
        active["snapshot_used"] |= snapshot_used
        if error:
            active["errors"].append(error)
        return
    path = (
        Path(trace_path)
        if trace_path is not None
        else Path(os.environ.get("SMARTMEMORY_HOOK_TRACE", str(DEFAULT_TRACE_PATH)))
    )
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "session_id": session_id,
        "ranked_ids": ranked_ids or [],
        "injected_ids": payload_ids(payload),
        "payload": payload,
        "payload_tokens": (len(payload) + 3) // 4,
        "error": error,
        "workspace_id": workspace_id,
        "cwd": cwd,
        "query": query,
        "candidate_count": candidate_count,
        "emitted": emitted,
        "snapshot_used": snapshot_used,
        "latency_ms": latency_ms,
    }
    if skipped_reason is not None:
        record["skipped_reason"] = skipped_reason
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > TRACE_MAX_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
            except OSError as exc:
                log.warning("hook trace lost rotation: %s", exc)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 — trace must never break the hook
        log.warning("hook-recall trace lost injection record: %s", exc)


def time_ms() -> int:
    """Helper: monotonic ms for latency measurement."""
    return int(time.monotonic() * 1000)
