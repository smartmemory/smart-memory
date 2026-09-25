"""Recall allocation regression tests using finite, top-k-respecting fakes."""

from types import SimpleNamespace

import pytest

from smartmemory_app import recall_format, storage


def item(name, **kwargs):
    return SimpleNamespace(
        item_id=name,
        content=kwargs.pop("content", name),
        memory_type="semantic",
        metadata={},
        confidence=1.0,
        reference=False,
        **kwargs,
    )


@pytest.fixture
def run_recall(monkeypatch):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    monkeypatch.setattr("smartmemory.origin_policy.get_default_tiers", lambda _: {1, 2})
    monkeypatch.setattr(
        "smartmemory.origin_policy.filter_by_tiers",
        lambda rows, tiers: [r for r in rows if getattr(r, "tier", 1) in tiers],
    )

    def run(recent, semantic, *, cwd="/project", query=None, top_k=3):
        def search(text, *, top_k, **kwargs):
            return (recent if not text else semantic)[:top_k]

        monkeypatch.setattr(
            storage, "get_memory", lambda: SimpleNamespace(search=search)
        )
        return storage.recall(
            cwd=cwd,
            query=query,
            top_k=top_k,
            include_snapshot=False,
            workspace_id="test",
            strict=False,
        )

    return run


@pytest.mark.parametrize("cwd", [None, "/project"])
def test_semantic_empty_fills_from_recency(run_recall, cwd):
    out = run_recall([item(f"r{i}") for i in range(5)], [], cwd=cwd)
    assert out.splitlines()[1:] == [f"- [semantic] [mem:r{i}] r{i}" for i in range(3)]


def test_populated_preserves_blend_order(run_recall):
    out = run_recall(
        [item(f"r{i}") for i in range(5)], [item(f"s{i}") for i in range(5)]
    )
    assert out.splitlines()[1:] == [
        "- [semantic] [mem:r0] r0",
        "- [semantic] [mem:r1] r1",
        "- [semantic] [mem:s0] s0",
    ]


@pytest.mark.parametrize("query", [None, "Xavier"])
def test_short_union_warns_without_padding(run_recall, caplog, query):
    out = run_recall([], [item("only")], query=query)
    assert out.count("\n- ") == 1
    assert "shortfall" in caplog.text and "requested=3" in caplog.text


def test_cross_channel_duplicates_fill_remaining_slots(run_recall, caplog):
    out = run_recall([item("a"), item("b"), item("c")], [item("a"), item("d")])
    assert out.splitlines()[1:] == [
        "- [semantic] [mem:a] a",
        "- [semantic] [mem:b] b",
        "- [semantic] [mem:c] c",
    ]
    assert "duplicate item_id" in caplog.text and "a" in caplog.text


def test_recency_empty_uses_semantic(run_recall):
    assert run_recall([], [item(str(i)) for i in range(5)]).count("\n- ") == 3


def test_filtered_window_widens_and_warns(run_recall, caplog):
    rows = [item(f"drop{i}", tier=4) for i in range(3)] + [
        item(str(i)) for i in range(3)
    ]
    assert run_recall(rows, []).count("\n- ") == 3
    assert "drop0" in caplog.text and "tier" in caplog.text


def test_prefix_collision_is_not_a_duplicate():
    prefix = "x" * 120
    out = recall_format.format_recall_lines(
        [
            {"item_id": "a", "content": prefix + " first"},
            {"item_id": "b", "content": prefix + " second"},
        ],
        top_k=3,
    )
    assert out.count("\n- ") == 2


def test_formatter_warns_for_each_discard(caplog):
    recall_format.format_recall_lines(
        [
            {"item_id": "empty", "content": " "},
            {"item_id": "a", "content": "alpha"},
            {"item_id": "b", "content": "ALPHA"},
            {"item_id": "c", "content": "gamma"},
        ],
        top_k=1,
    )
    for name, reason in [
        ("empty", "empty content"),
        ("b", "duplicate content"),
        ("c", "top_k cap"),
    ]:
        assert name in caplog.text and reason in caplog.text
    assert all(record.levelname == "WARNING" for record in caplog.records)
