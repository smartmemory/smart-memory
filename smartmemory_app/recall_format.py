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
    raw = (
        item
        if isinstance(item, dict)
        else {
            key: getattr(item, key, None)
            for key in (
                "item_id",
                "memory_type",
                "content",
                "origin",
                "confidence",
                "stale",
                "metadata",
                "node_category",
                "created_at",
                "reference",
            )
        }
    )
    metadata = {
        **(raw.get("properties") or {}),
        **(raw.get("context_snapshot") or {}),
        **(raw.get("metadata") or {}),
    }
    metadata = {**(metadata.get("context_snapshot") or {}), **metadata}
    for key in (
        "status",
        "node_category",
        "workspace_id",
        "session_date",
        "created_at",
    ):
        if raw.get(key) is not None:
            metadata[key] = raw[key]
    return {
        "item_id": raw.get("item_id") or raw.get("id") or raw.get("decision_id"),
        "memory_type": raw.get("memory_type")
        or ("decision" if raw.get("decision_id") else "?"),
        "content": raw.get("content") or "",
        "origin": raw.get("origin") or "",
        "confidence": raw.get("confidence")
        if raw.get("confidence") is not None
        else 1.0,
        "stale": raw.get("stale", False),
        "reference": raw.get("reference", False),
        "metadata": metadata,
    }


def filter_hook_items(items, excluded=None):
    """Exclude graph infrastructure in every recall channel, without per-row logs."""
    kept = []
    for item in items:
        row = _item_to_recall_dict(item)
        meta = row["metadata"]
        if row["memory_type"] in {
            "entity",
            "relation",
            "Version",
            "pattern",
        } or meta.get("node_category") in {"entity", "relation"}:
            if excluded is not None:
                excluded.add(recall_item_label(row))
            continue
        if (
            row["memory_type"] == "decision"
            and meta.get("status", "active") != "active"
        ):
            continue
        kept.append(item)
    return kept


_STOPWORDS = frozenset(
    """the and for from with that this these those them they their there then than
    into onto over under when where which what while who why how are was were been
    being have has had not but all any can will would should could may might must
    our your its also just only per via add support use using new now get set""".split()
)


def query_words(text):
    """Content words shared by a query and a memory; tokens under 3 chars never count.

    DEMO-CC-UPLIFT-1: "Stripe's" once yielded `s`, which matched every lesson with an
    apostrophe and let irrelevant lessons evict relevant recall.
    """
    return {
        word
        for word in re.findall(r"[a-z0-9_]+", text.lower())
        if len(word) >= 3 and word not in _STOPWORDS
    }


def matching_lessons(items, query):
    """Active lessons relevant to `query`, strongest overlap first.

    A lesson must share at least two content words with the query (one for a one-word
    query); a single incidental shared word is not relevance.
    """
    rows = [
        item
        for item in filter_hook_items(items)
        if _item_to_recall_dict(item)["memory_type"] == "decision"
    ]
    if not query:
        return rows
    words = query_words(query)
    needed = min(2, len(words))
    if not needed:
        return []
    scored = [
        (len(words & query_words(_item_to_recall_dict(item)["content"])), index, item)
        for index, item in enumerate(rows)
    ]
    return [
        item
        for score, _, item in sorted(scored, key=lambda s: (-s[0], s[1]))
        if score >= needed
    ]


# --- Formatter --------------------------------------------------------------


def recall_item_label(item: dict) -> str:
    """Identify discarded rows without logging their potentially private content."""
    return str(
        item.get("item_id")
        or "content-sha256:"
        + hashlib.sha256((item.get("content") or "").encode("utf-8")).hexdigest()[:16]
    )


def format_recall_lines(
    items: Iterable[dict],
    top_k: int,
    budget: int | None = None,
    query: str = "",
    lessons_first: bool = True,
) -> str:
    """Format items as the `## SmartMemory Context` block.

    Bug fixes vs legacy storage.recall():
      - empty/whitespace content suppressed
      - dedup by item_id (then lowercased full content within memory type)
      - top_k cap applied AFTER dedup, not before
      - `lessons_first=False` keeps the caller's relevance order (query recall ranks
        matching lessons itself; a stray decision hit must not jump a relevant chunk)

    Returns "" if no items survive filtering.
    """
    seen_ids: set[str] = set()
    seen_bodies: set[tuple[str, str]] = set()
    lines: list[str] = []

    items = [_item_to_recall_dict(it) for it in filter_hook_items(items)]
    if lessons_first:
        items.sort(key=lambda it: it["memory_type"] != "decision")
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

        if mtype == "decision":
            meta = it["metadata"]
            date = str(meta.get("session_date") or meta.get("created_at") or "unknown")[
                :10
            ]
            body = f"[session:{date}] {body}"
        body = body.replace("\n", "\n  ")
        lines.append(
            f"- {stale_marker}{conf_marker}[{mtype}] [mem:{recall_item_label(it)}] {body}"
        )

    if not lines:
        return ""
    lessons = [line for line in lines if re.match(r"- [^\[]*\[decision\]", line)]
    memories = [line for line in lines if line not in lessons]
    block = "\n".join(
        heading + "\n" + "\n".join(rows)
        for heading, rows in (
            ("## Lessons & decisions", lessons),
            ("## SmartMemory Context", memories),
        )
        if rows
    )
    return (
        budget_blocks([(block, None)], budget, query=query)
        if budget is not None
        else block
    )


def payload_ids(payload: str) -> list[str]:
    """IDs on rendered item headers, in injection order."""
    return re.findall(r"(?m)^- [^\n]*?\[mem:([^\]]+)\]", payload)


def budget_blocks(
    blocks: list[tuple[str, str | None]], budget: int, query: str = ""
) -> str:
    """Keep complete ranked items, accounting for headers and separators too.

    Formatter continuation lines are indented, so a multiline memory remains
    atomic. Oversize memories use query-focused sentences; lessons stay complete.
    """
    limit = max(0, budget) * 4
    output = ""
    seen: set[str] = set()
    sections = []
    for block, heading in blocks:
        if not block:
            continue
        for section in re.split(r"(?m)(?=^## )", block):
            if section.strip():
                sections.append(
                    (section, None if section.startswith("## Lessons") else heading)
                )
    sections.sort(key=lambda part: not part[0].startswith("## Lessons & decisions"))
    for block, heading in sections:
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
                # Lessons are atomic: never present a partial constraint as complete.
                if "[decision]" in entry:
                    log.info("recall dropped lesson %s for budget", iid)
                    continue
                marker = f"…[excerpt, mem:{iid}]"
                available = limit - len(output + prefix) - len(marker)
                tag_end = entry.find("] ", entry.find("[mem:")) + 2 if ids else 0
                tags, content = entry[:tag_end], entry[tag_end:]
                sentences = re.split(r"(?<=[.!?。！？])\s+|\n\s*", content)
                words = query_words(query)
                ranked = sorted(
                    range(len(sentences)),
                    key=lambda i: (-len(words & query_words(sentences[i])), i),
                )
                matching = [i for i in ranked if words & query_words(sentences[i])]
                if matching:
                    ranked = matching
                selected = []
                remaining = available - len(tags)
                for i in ranked:
                    cost = len(sentences[i]) + (1 if selected else 0)
                    if sentences[i] and cost <= remaining:
                        selected.append(i)
                        remaining -= cost
                if not selected:
                    log.info("recall dropped %s: no complete excerpt fits budget", iid)
                    continue
                entry = tags + " ".join(sentences[i] for i in sorted(selected)) + marker
                log.warning(
                    "recall lost full content for %s: query-focused excerpt", iid
                )
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


def record_hook_degradation(message: str, error: Exception | str) -> None:
    """Log a non-fatal loss separately from a failed injection."""
    detail = f"{message}: {error}"
    log.warning("%s", detail)
    active = _ACTIVE_TRACE.get()
    if active is not None:
        active.setdefault("degradations", []).append(detail)


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
    degradations: list[str] | None = None,
    excluded_non_memory: int = 0,
) -> None:
    """Append one JSONL line per hook invocation. Never raises."""
    active = _ACTIVE_TRACE.get()
    if active is not None:
        active["ranked_ids"].extend(ranked_ids or payload_ids(payload))
        active["snapshot_used"] |= snapshot_used
        active["excluded_non_memory"] = (
            active.get("excluded_non_memory", 0) + excluded_non_memory
        )
        active.setdefault("degradations", []).extend(degradations or [])
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
        "excluded_non_memory": excluded_non_memory,
        "lesson_ids": re.findall(
            r"(?m)^- [^\n]*?\[decision\] \[mem:([^\]]+)\]", payload
        ),
        "payload": payload,
        "payload_tokens": (len(payload) + 3) // 4,
        "error": error,
        "degradations": degradations or [],
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
