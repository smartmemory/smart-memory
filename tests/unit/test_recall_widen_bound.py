"""LITE-RECALL-WIDEN-UNBOUNDED-1: recall's widening loop is bounded by a fetch ceiling and the read's deadline.

A workspace id that matches no row (a session started in a fresh git worktree) made every row fail the filter, and
the loop doubled its fetch until one page held the whole store: a full-table read per round for hours. The fake core
below holds more rows than any ceiling and counts every fetch.
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartmemory_app import recall_format, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.lifecycle_config import LifecycleConfig

STORE_ROWS = 5000  # far past any ceiling: only a bound can stop the loop


def item(name, workspace):
    return SimpleNamespace(
        item_id=name,
        content=f"content {name}",
        memory_type="semantic",
        metadata={"workspace_id": workspace},
        confidence=1.0,
        reference=False,
    )


@pytest.fixture
def core(monkeypatch):
    """A fake store of STORE_ROWS rows; ``calls`` records (text, top_k) per fetch, ``traces`` the trace records."""
    state = SimpleNamespace(calls=[], traces=[], rows=[], clock=[], now=0.0)
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: state.traces.append(kw))
    monkeypatch.setattr("smartmemory.origin_policy.get_default_tiers", lambda _: {1, 2})
    monkeypatch.setattr(
        "smartmemory.origin_policy.filter_by_tiers", lambda rows, tiers: rows
    )
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(storage, "_RECALL_WIDEN_FACTOR", 8)

    def search(text, *, top_k, **kwargs):
        state.calls.append((text, top_k))
        state.now += 1.0  # every fetch costs one injected second
        return state.rows[:top_k]

    def clock():
        state.clock.append(state.now)
        return state.now

    no_decisions = SimpleNamespace(search_nodes=lambda _query: [])
    monkeypatch.setattr(
        storage,
        "get_memory",
        lambda: SimpleNamespace(search=search, _graph=no_decisions),
    )
    monkeypatch.setattr(storage, "_recall_clock", clock)
    return state


def recall(**kw):
    return storage.recall(
        include_snapshot=False, workspace_id="ws_fresh", strict=False, **kw
    )


def shortfalls(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "recall shortfall" in r.getMessage()
    ]


def test_no_matching_row_stops_at_the_ceiling(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    out = recall(top_k=10)
    # Orient (no query): 10, 20, 40, then the ceiling 8 x 10 = 80. log2(80 / 10) + 1 = 4 rounds.
    assert [k for _, k in core.calls] == [10, 20, 40, 80]
    assert out == ""
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0; stopped widening at a fetch of 80 (ceiling), so eligible "
        "candidates past it are not shown"
    ]
    assert core.traces[-1]["degradations"] == [
        "Recall partial: stopped widening at a fetch of 80 (ceiling)"
    ]


def test_ceiling_bounds_query_and_workspace_channels(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    recall(top_k=5, query="atlas")
    # Query: first fetch 2 x 5 = 10, ceiling 40.
    assert core.calls == [("atlas", 10), ("atlas", 20), ("atlas", 40)]
    core.calls.clear()
    recall(top_k=4, cwd="/project")
    # Both channels per round; ceiling max(16, 8 x 4) = 32.
    assert core.calls == [(t, k) for k in (4, 8, 16, 32) for t in ("", "/project")]
    assert len(shortfalls(caplog)) == 2 and all(
        "(ceiling)" in m for m in shortfalls(caplog)
    )


def test_top_k_one_uses_the_floor(core):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    recall(top_k=1)
    assert [k for _, k in core.calls] == [1, 2, 4, 8, 16]


def test_deadline_passing_mid_loop_stops_at_the_next_round(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    # Rounds end at t=1 and t=2; the deadline 1.5 has passed before round 3 would start.
    out = recall(top_k=10, deadline=1.5)
    assert [k for _, k in core.calls] == [10, 20]
    assert core.clock == [1.0, 2.0]
    assert out == ""
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0; stopped widening at a fetch of 20 (deadline), so eligible "
        "candidates past it are not shown"
    ]
    assert core.traces[-1]["degradations"] == [
        "Recall partial: stopped widening at a fetch of 20 (deadline)"
    ]


def test_recall_that_fills_on_round_one_makes_one_fetch(core, caplog):
    core.rows = [item(f"r{i}", "ws_fresh") for i in range(STORE_ROWS)]
    out = recall(top_k=10, deadline=1e12)
    assert core.calls == [("", 10)]
    assert core.clock == []  # no widening round, so the deadline is never read
    assert out.count("\n- ") == 10
    assert shortfalls(caplog) == []
    assert "degradations" not in core.traces[-1]


def test_short_store_is_exhaustion_not_a_bound(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(15)]
    recall(top_k=10)
    assert [k for _, k in core.calls] == [10, 20]
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0, eligible distinct candidates exhausted"
    ]


def test_widen_factor_env_is_read_like_the_other_caps(monkeypatch, caplog):
    monkeypatch.setenv("SMARTMEMORY_RECALL_WIDEN_FACTOR", "3")
    assert storage._positive_int_env("SMARTMEMORY_RECALL_WIDEN_FACTOR", 8) == 3
    monkeypatch.setenv("SMARTMEMORY_RECALL_WIDEN_FACTOR", "zero")
    assert storage._positive_int_env("SMARTMEMORY_RECALL_WIDEN_FACTOR", 8) == 8
    assert (
        "SMARTMEMORY_RECALL_WIDEN_FACTOR='zero' is not a positive integer"
        in caplog.text
    )


def test_lifecycle_passes_its_deadline_to_every_recall(monkeypatch):
    lookup = Mock(return_value="")
    monkeypatch.setattr(storage, "recall", lookup)
    monkeypatch.setattr(MemoryLifecycle, "_save_state", lambda self: None)
    monkeypatch.setattr(MemoryLifecycle, "_should_recall", lambda self, prompt: True)
    lc = MemoryLifecycle("test_widen", LifecycleConfig(rules_card_enabled=False))

    lc.orient("/tmp/project", deadline=123.0)
    assert [c.kwargs.get("deadline") for c in lookup.call_args_list] == [123.0, 123.0]
    lookup.reset_mock()
    lc.recall("Explain the facts", cwd="/tmp/project", deadline=456.0)
    assert lookup.call_args.kwargs["deadline"] == 456.0
    lookup.reset_mock()
    # No deadline: the call shape is the one before this change.
    lc.orient("/tmp/project")
    assert all("deadline" not in c.kwargs for c in lookup.call_args_list)
