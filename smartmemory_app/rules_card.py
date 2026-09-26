"""Always-on, workspace-scoped active constraints for SessionStart."""

import logging
import os
import unicodedata
from datetime import datetime, timezone

from smartmemory_app.recall_format import (
    _item_to_recall_dict,
    budget_blocks,
    derive_workspace_id,
    filter_hook_items,
    format_recall_lines,
    payload_ids,
    record_hook_degradation,
)

log = logging.getLogger(__name__)


def _timestamp(value):
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return float("-inf")


def _newest(row):
    meta = row["metadata"]
    created = _timestamp(meta.get("created_at"))
    return (_timestamp(meta.get("session_date") or meta.get("created_at")), created)


def build_rules_card(cwd=None, *, budget=2000):
    """List local lessons, then apply recall eligibility before allocating budget."""
    from smartmemory.origin_policy import get_default_tiers, get_tier
    from smartmemory_app.remote_backend import RemoteMemory
    from smartmemory_app.session_lessons import ORIGIN, _RULE
    from smartmemory_app.storage import get_memory

    mem = get_memory()
    if isinstance(mem, RemoteMemory):
        record_hook_degradation(
            "Orient lost rules card",
            "remote backend has no complete workspace lesson listing; card unavailable",
        )
        return ""
    workspace = derive_workspace_id(cwd)
    strict = os.environ.get("SMARTMEMORY_RECALL_STRICT", "").lower() in (
        "1",
        "true",
        "yes",
    )
    floor = float(os.environ.get("SMARTMEMORY_RECALL_FLOOR", "0.3"))
    tiers = get_default_tiers("recall")
    rows = mem._graph.search_nodes({"memory_type": "decision", "origin": ORIGIN})
    constraints = []
    legacy_count = 0
    for item in filter_hook_items(rows):
        row = _item_to_recall_dict(item)
        meta = row["metadata"]
        scope = meta.get("workspace_id")
        if (
            row["memory_type"] != "decision"
            or row["origin"] != ORIGIN
            or (workspace and scope != workspace and (scope is not None or strict))
            or get_tier(row["origin"]) not in tiers
            or row["confidence"] < floor
            or row["reference"]
        ):
            continue
        if "lesson_kind" not in meta:
            legacy_count += 1
            is_constraint = bool(_RULE.search(row["content"]))
        else:
            is_constraint = meta["lesson_kind"] == "constraint"
        if is_constraint and row["content"].strip():
            constraints.append(row)
    if legacy_count:
        log.warning(
            "Rules card classified %d legacy lessons with rule regex", legacy_count
        )
    seen = set()
    distinct = []
    for row in sorted(constraints, key=_newest, reverse=True):
        key = " ".join(unicodedata.normalize("NFKC", row["content"]).casefold().split())
        if key not in seen:
            seen.add(key)
            distinct.append(row)
    block = format_recall_lines(distinct, top_k=len(distinct)).replace(
        "## Lessons & decisions", "## Rules learned in this project", 1
    )
    card = budget_blocks([(block, None)], budget)
    dropped = len(payload_ids(block)) - len(payload_ids(card))
    if dropped:
        log.warning(
            "Rules card dropped %d lessons for token budget %d", dropped, budget
        )
    return card
