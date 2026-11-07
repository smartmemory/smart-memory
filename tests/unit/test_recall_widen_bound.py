"""LITE-RECALL-WIDEN-UNBOUNDED-1: recall's widening loop is bounded by a fetch ceiling and the read's deadline.

A workspace id that matches no row (a session started in a fresh git worktree) made every row fail the filter, and
the loop doubled its fetch until one page held the whole store: a full-table read per round for hours. The fake core
below holds more rows than any ceiling and counts every fetch. On 2.0 the ceiling sits under the read gate's bound
(``_gated_fetch_cap``) and a stop at either bound carries ``fetch_bound`` on the answer.
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartmemory.search.results import PartialText

from smartmemory_app import recall_format, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.lifecycle_config import LifecycleConfig

STORE_ROWS = 5000  # far past any ceiling: only a bound can stop the loop
FETCH_BOUND = "fetch_bound"


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
    state = SimpleNamespace(calls=[], traces=[], rows=[], clock=[], now=0.0, lookups=[])
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

    no_decisions = SimpleNamespace(
        search_nodes=lambda query: state.lookups.append(query) or []
    )
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
    # Orient (no query): 10, then one widen directly to the existing ceiling 8 x 10 = 80.
    assert [k for _, k in core.calls] == [10, 80]
    assert out == ""
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0; stopped widening at a fetch of 80 (ceiling: the page came back "
        "full), so eligible candidates past what core examined are not shown"
    ]
    assert type(out) is PartialText and out.partial == (FETCH_BOUND,)


def test_ceiling_bounds_query_and_workspace_channels(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    recall(top_k=5, query="atlas")
    # Query: first fetch 2 x 5 = 10, ceiling 40.
    assert core.calls == [("atlas", 10), ("atlas", 40)]
    core.calls.clear()
    recall(top_k=4, cwd="/project")
    # Both channels per round; ceiling max(16, 8 x 4) = 32.
    assert core.calls == [(t, k) for k in (32,) for t in ("", "/project")]
    assert len(shortfalls(caplog)) == 2 and all(
        "(ceiling: " in m for m in shortfalls(caplog)
    )


def test_gate_bound_still_wins_when_it_is_smaller(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    recall(top_k=100)
    # Ceiling 8 x 100 = 800 is past the gate's 200: the gate bound stops it, with its own wording.
    assert [k for _, k in core.calls] == [100, 200]
    assert shortfalls(caplog) == [
        "recall shortfall: requested=100 emitted=0; stopped widening at a fetch of 200 (the page came back full), "
        "so eligible candidates past what core examined are not shown"
    ]


def test_top_k_one_uses_the_floor(core):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    recall(top_k=1)
    assert [k for _, k in core.calls] == [1, 16]


def test_deadline_passing_mid_loop_stops_at_the_next_round(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(STORE_ROWS)]
    # The first round ends at t=1, after the deadline, so the widen must not start.
    out = recall(top_k=10, deadline=0.5)
    assert [k for _, k in core.calls] == [10]
    # Read before round 1, then before each later round.
    assert core.clock == [0.0, 1.0]
    assert out == ""
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0; stopped widening at a fetch of 10 (deadline), so eligible "
        "candidates past what core examined are not shown"
    ]
    assert type(out) is PartialText and out.partial == (FETCH_BOUND,)


def test_recall_that_fills_on_round_one_makes_one_fetch(core, caplog):
    core.rows = [item(f"r{i}", "ws_fresh") for i in range(STORE_ROWS)]
    out = recall(top_k=10, deadline=1e12)
    assert core.calls == [("", 10)]
    # Read once, before round 1; no widening round follows.
    assert core.clock == [0.0]
    assert out.count("\n- ") == 10
    assert shortfalls(caplog) == []
    assert type(out) is str


def test_short_store_is_exhaustion_not_a_bound(core, caplog):
    core.rows = [item(f"r{i}", "ws_other") for i in range(15)]
    recall(top_k=10)
    assert [k for _, k in core.calls] == [10, 80]
    assert shortfalls(caplog) == [
        "recall shortfall: requested=10 emitted=0; eligible distinct candidates exhausted"
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
    # Every deadline here is still ahead.
    monkeypatch.setattr(storage, "_recall_clock", lambda: 0.0)
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


def test_expired_on_entry_starts_no_lookup_and_no_fetch(core, caplog):
    """RCLWIDEN-CX F1: a deadline that passed before recall began starts no lookup and no round, on every channel."""
    core.rows = [item(f"r{i}", "ws_fresh") for i in range(STORE_ROWS)]
    channels = [
        dict(include_snapshot=True),
        dict(include_snapshot=False, query="facts"),
        dict(include_snapshot=False, cwd="/tmp/project"),
    ]
    for channel in channels:
        caplog.clear()
        core.calls.clear(), core.lookups.clear(), core.clock.clear()
        out = storage.recall(
            workspace_id="ws_fresh", strict=False, top_k=10, deadline=0.0, **channel
        )
        assert core.calls == [] and core.lookups == [], channel
        assert core.clock == [0.0], channel
        assert out == ""
        assert shortfalls(caplog) == [
            "recall shortfall: requested=10 emitted=0; stopped widening before the first fetch (deadline), so "
            "eligible candidates past what core examined are not shown"
        ]
        assert type(out) is PartialText and out.partial == (FETCH_BOUND,)


def test_orient_does_not_start_its_patterns_recall_after_the_deadline(
    monkeypatch, caplog
):
    """RCLWIDEN-CX F1: orient's first recall spends the deadline, so its patterns recall never starts."""
    from smartmemory_app import rules_card

    now = [0.0]
    monkeypatch.setattr(storage, "_recall_clock", lambda: now[0])

    def first_recall_spends_the_deadline(*args, **kwargs):
        now[0] = 10.0
        return ""

    lookup = Mock(side_effect=first_recall_spends_the_deadline)
    monkeypatch.setattr(storage, "recall", lookup)
    build = Mock(return_value="")
    monkeypatch.setattr(rules_card, "build_rules_card", build)
    monkeypatch.setattr(MemoryLifecycle, "_save_state", lambda self: None)
    lc = MemoryLifecycle("test_widen", LifecycleConfig(rules_card_enabled=True))

    lc.orient("/tmp/project", deadline=5.0)
    assert build.call_count == 1 and lookup.call_count == 1
    assert "patterns" not in str(lookup.call_args)
    assert "Orient skipped patterns context: deadline passed" in caplog.text

    # Already past the deadline when orient begins: the rules card does not start either. The one recall it still
    # hands the deadline to makes no fetch (test_expired_on_entry_starts_no_lookup_and_no_fetch).
    lookup.reset_mock(), build.reset_mock(), caplog.clear()
    lc.orient("/tmp/project", deadline=5.0)
    assert build.call_count == 0 and lookup.call_count == 1
    assert lookup.call_args.kwargs["deadline"] == 5.0
    assert "Orient skipped rules card: deadline passed" in caplog.text


@pytest.mark.parametrize("arm", [{}, {"query": "atlas"}, {"cwd": "/project"}])
def test_ninety_percent_foreign_keeps_legacy_results_with_two_searches(core, arm):
    """The old final window held eight local rows. Keep all eight without rescanning intermediate windows."""
    core.rows = [item(f"r{i}", "ws_fresh" if i % 10 == 9 else "ws_other") for i in range(80)]
    out = recall(top_k=10, **arm)
    expected = [f"r{i}" for i in range(9, 80, 10)]
    assert [line.split("[mem:")[1].split("]")[0] for line in out.splitlines() if "[mem:" in line] == expected
    assert len(core.calls) <= 2
    assert max(k for _, k in core.calls) == 80


def test_real_sqlite_foreign_workspace_fixture(tmp_path, monkeypatch, caplog):
    """Exercise the actual recency lane on a disposable SQLite store, no models or services."""
    from datetime import datetime, timedelta, timezone
    from smartmemory.tools.factory import create_lite_memory
    from smartmemory.save.read_port import InMemoryPendingStore

    mem = create_lite_memory(data_dir=str(tmp_path / "test_refetch"), spawn_worker=False)
    calls = []
    real_search = mem.search

    def search(text, **kwargs):
        calls.append(kwargs["top_k"])
        return real_search(text, **kwargs)

    monkeypatch.setattr(mem, "search", search)
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    monkeypatch.setattr(storage, "_RECALL_WIDEN_FACTOR", 8)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        for n in range(80):
            mem._graph.backend.add_node(
                f"test_refetch_{n:03d}",
                {"content": f"atlas fixture {n}", "origin": "user:test",
                 "created_at": (base + timedelta(seconds=80 - n)).isoformat(),
                 "confidence": 1.0,
                 "metadata": {"workspace_id": "ws_fresh" if n % 10 == 9 else "ws_other"}},
                memory_type="semantic",
            )
        mem.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
        out = recall(top_k=10)
        assert calls == [10, 80]
        assert [line.split("[mem:")[1].split("]")[0] for line in out.splitlines() if "[mem:" in line] == [
            f"test_refetch_{n:03d}" for n in range(9, 80, 10)
        ]
        assert getattr(out, "partial", ()) == (FETCH_BOUND,)
        assert any("requested=10 emitted=8" in msg for msg in shortfalls(caplog))
    finally:
        mem.close()
