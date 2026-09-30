"""CORE-RECALL-FRESHNESS-1: every recalled item carries a freshness signal.

Before the fix the hook formatter dated only `decision` rows
(`recall_format.py:247-251`), so a months-old memory and yesterday's conflicting
update were injected into Claude Code indistinguishably. These tests are
format-agnostic on purpose (the rendered shape is a design-gate decision): each
line must carry either the item's ISO date or a relative age, and an old and a
new item must not render the same freshness.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.xfail(
    strict=True, reason="CORE-RECALL-FRESHNESS-1: fix awaiting design gate"
)


OLD = "2026-03-02T00:00:00+00:00"
NEW = "2026-09-29T00:00:00+00:00"
_FRESHNESS = re.compile(r"\b20\d\d-\d\d-\d\d\b|\b\d+(?:d|w|mo|y) ago\b|\btoday\b")


def _line_for(out: str, needle: str) -> str:
    return next(line for line in out.splitlines() if needle in line)


def _freshness(line: str) -> str:
    match = _FRESHNESS.search(line)
    assert match, f"no date or age on recalled line: {line!r}"
    return match.group(0)


def test_dict_rows_old_and_new_both_carry_distinct_freshness():
    """Remote path: service rows are dicts with metadata.created_at."""
    from smartmemory_app.recall_format import format_recall_lines

    out = format_recall_lines(
        [
            {
                "item_id": "old",
                "memory_type": "semantic",
                "content": "Payments deploys from the legacy Jenkins pipeline.",
                "metadata": {"created_at": OLD},
            },
            {
                "item_id": "new",
                "memory_type": "semantic",
                "content": "Payments deploys from GitHub Actions now.",
                "metadata": {"created_at": NEW},
            },
        ],
        top_k=10,
    )
    old_f = _freshness(_line_for(out, "[mem:old]"))
    new_f = _freshness(_line_for(out, "[mem:new]"))
    assert old_f != new_f


def test_memory_item_rows_carry_freshness():
    """Local path: storage.recall hands MemoryItem objects to the formatter."""
    from smartmemory.models.memory_item import MemoryItem

    from smartmemory_app.recall_format import format_recall_lines

    old = MemoryItem(
        item_id="old",
        content="Payments deploys from the legacy Jenkins pipeline.",
        memory_type="zettel",
        valid_start_time=datetime(2026, 3, 2, tzinfo=timezone.utc),
        metadata={"created_at": OLD},
    )
    new = MemoryItem(
        item_id="new",
        content="Payments deploys from GitHub Actions now.",
        memory_type="zettel",
        valid_start_time=datetime(2026, 9, 29, tzinfo=timezone.utc),
        metadata={"created_at": NEW},
    )
    out = format_recall_lines([old, new], top_k=10)
    assert _freshness(_line_for(out, "[mem:old]")) != _freshness(
        _line_for(out, "[mem:new]")
    )
