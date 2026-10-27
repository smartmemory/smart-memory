"""CLI-SEARCH-FACT-COLLAPSE-1: rank sources, not rows.

Core search returns a memory and each of its extracted ``[fact]`` rows as separate results,
so one memory's facts can fill most of a Lite top-5 and push a second memory out. This groups
the rows under the memory that produced them (``metadata.source_item``): a group ranks by its
best row, the page holds ``k`` parents, each parent is returned once with its matching facts
nested under ``children`` (best first, capped) and a ``collapse`` block saying how the row got
there. ``children`` and ``collapse`` are the public JSON names (CORE-DOC-SUMMARY-POINTER-1 F3).

Contract and the six things it must not lose:
``units/CLI-ASK-EVIDENCE-FACT-COLLAPSE-1/report.md`` ``## Ranking contract``.
"""

import logging
from typing import Callable

from smartmemory.temporal.axes import created_at_of, parse_time_boundary

log = logging.getLogger(__name__)

FACTS_PER_PARENT = 3
POOL_FACTOR = 6
POOL_MIN = 30


def pool_size(k: int) -> int:
    """How deep to fetch so the slots facts used to take can go to the next memory."""
    return max(POOL_FACTOR * k, POOL_MIN)


def scoped_get(item_id: str) -> dict:
    """The scoped read behind parent promotion. A failed read is an unreadable parent (``{}``)."""
    from smartmemory_app.storage import get

    try:
        return get(item_id)
    except Exception as exc:  # noqa: BLE001 - collapse_to_parents logs the standalone fallback
        log.warning("search: reading source memory %s failed: %s", item_id, exc)
        return {}


def _in_window(row: dict, since, until) -> bool:
    """Rows with no creation evidence are kept: absent evidence is not an out-of-window verdict."""
    created = created_at_of(row)
    if created is None:
        return True
    if since is not None and created < parse_time_boundary(since):
        return False
    return until is None or created <= parse_time_boundary(until)


def _score_of(row: dict):
    meta = row.get("metadata") or {}
    for value in (row.get("score"), meta.get("fact_score"), meta.get("score")):
        if value is not None:
            return value
    return None


def _child(rank: int, fact: dict) -> dict:
    return {
        "item_id": fact.get("item_id"),
        "memory_type": fact.get("memory_type", "fact"),
        "content": fact.get("content", ""),
        "score": _score_of(fact),
        "rank": rank,
        "kind": "fact",
    }


def _collapsed(row: dict, children: list, **flags) -> dict:
    """``row`` with the capped best-first ``children`` and the ``collapse`` provenance block."""
    return {
        **row,
        "children": [_child(rank, fact) for rank, fact in children[:FACTS_PER_PARENT]],
        "collapse": {
            "parent_matched": False,
            "promoted": False,
            "parent_unresolved": False,
            **flags,
            "children_total": len(children),
            "kind": "fact",
        },
    }


def collapse_to_parents(
    rows: list[dict],
    k: int,
    fetch_parent: Callable[[str], dict],
    *,
    since=None,
    until=None,
) -> list[dict]:
    """Return at most ``k`` rows, one per source memory, facts nested under ``children``.

    ``rows`` is score-ordered, so a group's rank is where its first row appeared.
    ``fetch_parent`` is a scoped read (``storage.get``): it returns ``{}`` for a memory the
    caller cannot read, in which case the fact stays a standalone row.
    ``since``/``until`` drop rows outside the window BEFORE grouping, so an out-of-window
    fact never admits its parent, and a promoted parent is re-checked against the window.
    """
    groups: dict[str, dict] = {}
    no_source = 0
    for rank, row in enumerate(rows, 1):  # rank = position in the pre-collapse pool
        if not isinstance(row, dict) or not _in_window(row, since, until):
            continue
        item_id = row.get("item_id") or row.get("id")
        if row.get("memory_type") == "fact":
            source = (row.get("metadata") or {}).get("source_item")
            if not source:
                # Legacy edge: nothing to group it with, so it keeps its own slot.
                no_source += 1
                groups.setdefault(
                    f"standalone:{item_id}",
                    {"memory": row, "facts": [], "legacy": True},
                )
                continue
            groups.setdefault(source, {"memory": None, "facts": []})["facts"].append(
                (rank, row)
            )
        else:
            groups.setdefault(item_id, {"memory": None, "facts": []})["memory"] = row
    if no_source:
        log.warning(
            "search: %d fact row(s) carry no source_item (stored before facts recorded it) "
            "and stay standalone results",
            no_source,
        )

    page: list[dict] = []
    unreadable = 0
    for parent_id, group in groups.items():
        if len(page) >= k:
            break
        memory, facts = group["memory"], group["facts"]
        promoted = False
        if memory is None:
            fetched = fetch_parent(parent_id)
            if fetched and _in_window(fetched, since, until):
                memory, promoted = fetched, True
            elif fetched:
                continue
            else:
                # Unreadable or gone: the best matching fact stands for the group.
                unreadable += 1
                page.append(_collapsed(facts[0][1], facts, parent_unresolved=True))
                continue
        if group.get("legacy"):
            page.append(memory)
            continue
        # `children` is additive: a memory with no matching fact keeps its row shape unchanged.
        page.append(
            _collapsed(memory, facts, parent_matched=not promoted, promoted=promoted)
            if facts
            else memory
        )
    if unreadable:
        log.warning(
            "search: %d fact row(s) matched but their source memory could not be read; "
            "returned as standalone results",
            unreadable,
        )
    return page
