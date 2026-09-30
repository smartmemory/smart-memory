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


def test_event_time_beats_write_time():
    """An imported fact written today but true in March renders March."""
    from smartmemory_app.recall_format import format_recall_lines

    out = format_recall_lines(
        [
            {
                "item_id": "imp",
                "memory_type": "semantic",
                "content": "Imported fact.",
                "valid_start_time": OLD,
                "created_at": NEW,
                "transaction_time": NEW,
            }
        ],
        top_k=10,
    )
    assert "- [semantic] [2026-03-02] [mem:imp] Imported fact." in out


def test_reference_time_and_session_date_precedence():
    from smartmemory_app.recall_format import event_date

    assert (
        event_date(
            {
                "valid_start_time": NEW,
                "metadata": {"reference_time": OLD, "created_at": NEW},
            }
        )
        == "2026-03-02"
    )
    assert (
        event_date({"metadata": {"session_date": "2026-03-02", "created_at": NEW}})
        == "2026-03-02"
    )
    assert event_date({"transaction_time": NEW, "metadata": {}}) == "2026-09-29"


def test_unparseable_session_date_warns_and_uses_write_time(caplog):
    from smartmemory_app.recall_format import event_date

    with caplog.at_level("WARNING", logger="smartmemory_app.recall_format"):
        got = event_date(
            {
                "item_id": "x",
                "metadata": {"session_date": "whenever", "created_at": NEW},
            }
        )
    assert got == "2026-09-29"
    assert "unparseable session_date" in caplog.text


def test_dateless_item_renders_marker_and_warns(caplog):
    from smartmemory_app.recall_format import format_recall_lines

    with caplog.at_level("WARNING", logger="smartmemory_app.recall_format"):
        out = format_recall_lines(
            [{"item_id": "nd", "memory_type": "semantic", "content": "No date."}],
            top_k=10,
        )
    assert "- [semantic] [date?] [mem:nd] No date." in out
    assert "recall lost freshness for nd" in caplog.text


def test_decision_lines_keep_session_tag_only():
    from smartmemory_app.recall_format import format_recall_lines

    out = format_recall_lines(
        [
            {
                "decision_id": "d1",
                "content": "Always run ruff format.",
                "session_date": "2026-09-24",
                "valid_start_time": OLD,
            }
        ],
        top_k=10,
    )
    assert "- [decision] [mem:d1] [session:2026-09-24] Always run ruff format." in out


def test_remote_row_top_level_stale_renders_marker():
    """Service `_format_memory_item` returns `stale` top-level (crud.py), not in metadata."""
    from smartmemory_app.recall_format import format_recall_lines

    out = format_recall_lines(
        [
            {
                "item_id": "s",
                "memory_type": "semantic",
                "content": "Old fact.",
                "stale": True,
                "created_at": OLD,
                "metadata": {},
            }
        ],
        top_k=10,
    )
    assert "- ⚠[semantic] [2026-03-02] [mem:s] Old fact." in out
