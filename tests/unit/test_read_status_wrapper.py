"""U5.22 wrapper search/get/wildcard/recall carry the merged-read extraction status. # trace G:G43,G46,G114,G136,G142

Real SQLite lite memory with an in-memory pending store. Gate ON: a receipt-only
item must be listed with `read_status`, graph-only items carry the neutral
"unavailable" status. Gate OFF: output equals the legacy shape (no carrier anywhere).
"""

import logging

import pytest

from smartmemory.configuration.switch_testing import switch_override
from smartmemory.save.read_port import (
    AdmissionRow,
    InMemoryPendingStore,
    LexicalRecord,
    ScopeKeys,
    source_sha256,
)
from smartmemory.search.visibility_gate import SWITCH
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import storage
from smartmemory_app.recall_format import (
    MalformedReadStatusError,
    _item_to_recall_dict,
    format_recall_lines,
)

SCOPE = ScopeKeys(None, "local", None)
TEXT = "atlas budget accepted full source text"
TIME = "2026-10-11T12:00:00.000000+00:00"


def _row(item_id="test_pending", revision=1, summary="atlas budget"):
    return AdmissionRow(
        item_id,
        revision,
        "test_run",
        TIME,
        "semantic",
        "atlas",
        summary,
        "received",
        "available",
        "indexed",
        SCOPE,
        "queued",
        None,
    )


@pytest.fixture
def lane(tmp_path, monkeypatch):
    mem = create_lite_memory(data_dir=str(tmp_path / "test_u522_wrapper"))
    store = InMemoryPendingStore()
    row = _row()
    store.admit(row, LexicalRecord(row.item_id, 1, TEXT, source_sha256(TEXT), SCOPE))
    mem._graph.backend.add_node(
        "test_legacy",
        {
            "content": "atlas budget stored text",
            "origin": "user:test",
            "created_at": TIME,
        },
        memory_type="semantic",
    )
    with switch_override(SWITCH, True):
        mem.bind_pending_reads(store, workspace_hidden=False)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
    try:
        yield mem, store
    finally:
        mem.close()


def _by_id(rows):
    return {r["item_id"]: r for r in rows}


def test_wildcard_gate_on_lists_pending_and_stamps_graph_rows(lane):
    with switch_override(SWITCH, True):
        got = _by_id(storage.search("*"))
    assert (
        got["test_pending"]["read_status"]["extraction_state"] == "extraction_pending"
    )
    assert got["test_legacy"]["read_status"]["extraction_state"] == "unavailable"


def test_wildcard_gate_off_is_the_legacy_snapshot(lane):
    with switch_override(SWITCH, False):
        got = _by_id(storage.search("*"))
    assert set(got) == {"test_legacy"}
    assert "read_status" not in got["test_legacy"]


def test_wildcard_gate_on_applies_filters_and_type_to_pending(lane):
    with switch_override(SWITCH, True):
        assert "test_pending" in _by_id(storage.search("*", memory_type="semantic"))
        assert "test_pending" not in _by_id(storage.search("*", memory_type="decision"))


def _add_graph(mem, item_id, content, *, origin="user:test", revision=None):
    props = {"content": content, "origin": origin, "created_at": TIME}
    if revision is not None:
        props["revision"] = revision
    mem._graph.backend.add_node(item_id, props, memory_type="semantic")


def _edit(mem, store):
    """A newer admission revision of a graph row: core serves revision 2, the graph still holds revision 1."""
    _add_graph(mem, "test_same", "atlas obsolete graph content", revision=1)
    row = _row("test_same", revision=2, summary="atlas revised current summary")
    text = "atlas revised current source"
    store.admit(row, LexicalRecord(row.item_id, 2, text, source_sha256(text), SCOPE))


def _tombstone(mem, store):
    _edit(mem, store)
    assert store.tombstone(SCOPE, "test_same")


def _hidden_origin(mem, store):
    _add_graph(mem, "test_hidden", "atlas hidden hook material", origin="hook:test")


def _extra_graph_row(mem, store):
    _add_graph(mem, "test_other", "atlas other")


# C5/C3/C4 table: (case, setup, window, ids that must be absent, ids that must be present).
# `window` is the first-fetch window, so 1 forces the doubling loop to run. The edit/tombstone rows are the bugs the
# round-1 review reproduced (merged identity and tombstone precedence lost), hidden-origin is the snapshot's missing filter.
WILDCARD_CASES = [
    ("newer-edit", _edit, 200, set(), {"test_same", "test_legacy", "test_pending"}),
    ("tombstone", _tombstone, 200, {"test_same"}, {"test_legacy", "test_pending"}),
    (
        "hidden-origin",
        _hidden_origin,
        200,
        {"test_hidden"},
        {"test_legacy", "test_pending"},
    ),
    (
        "window-smaller-than-corpus",
        _extra_graph_row,
        1,
        set(),
        {"test_other", "test_legacy", "test_pending"},
    ),
]


@pytest.mark.parametrize(
    "name,setup,window,absent,present",
    WILDCARD_CASES,
    ids=[c[0] for c in WILDCARD_CASES],
)
def test_wildcard_gate_on_is_core_merged_identity(
    lane, monkeypatch, name, setup, window, absent, present
):
    mem, store = lane
    setup(mem, store)
    monkeypatch.setattr(storage, "_WILDCARD_PENDING_WINDOW", window)
    with switch_override(SWITCH, True):
        authoritative = {i.item_id: i.to_dict() for i in mem.search("*", top_k=50)}
        got = _by_id(storage.search("*"))
    assert absent.isdisjoint(got)
    assert present <= set(got)
    assert set(got) == set(authoritative)
    for item_id, row in got.items():
        assert row["content"] == authoritative[item_id]["content"]
        assert (
            row["read_status"] == authoritative[item_id]["read_status"]
        )  # every returned row carries core's status
    if name == "newer-edit":
        assert got["test_same"]["content"] == "atlas revised current summary"
        assert got["test_same"]["read_status"]["revision"] == 2


def test_wildcard_gate_on_full_window_at_cap_is_a_typed_refusal(
    lane, caplog, monkeypatch
):
    monkeypatch.setattr(storage, "_WILDCARD_PENDING_WINDOW", 1)
    monkeypatch.setattr(storage, "_WILDCARD_CORE_CAP", 1)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True):
        with pytest.raises(storage.WildcardEnumerationOverflowError):
            storage.search("*")
    assert [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == storage.log.name
    ] == [
        (
            "WARNING",
            "wildcard search refused: core still returned a full window of 1 hits at the cap, "
            "so the listing would be incomplete",
        )
    ]


def test_wildcard_gate_off_never_asks_core(lane, monkeypatch):
    mem, _store = lane
    monkeypatch.setattr(
        mem,
        "search",
        lambda *a, **k: pytest.fail("gate OFF wildcard must read the snapshot only"),
    )
    with switch_override(SWITCH, False):
        assert set(_by_id(storage.search("*"))) == {"test_legacy"}


@pytest.mark.parametrize("on", [False, True])
def test_query_search_and_get_follow_the_gate(lane, on):
    with switch_override(SWITCH, on):
        hits = _by_id(
            storage.search(
                "atlas budget", top_k=10, reranker="none", enable_hybrid=False
            )
        )
        got = storage.get("test_pending") if on else storage.get("test_legacy")
    assert ("test_pending" in hits) is on
    if on:
        assert (
            hits["test_pending"]["read_status"]["extraction_state"]
            == "extraction_pending"
        )
        assert got["read_status"]["extraction_state"] == "extraction_pending"
    else:
        assert all("read_status" not in h for h in hits.values())
        assert "read_status" not in got


def test_recall_dict_and_line_carry_status_only_when_present():
    base = {
        "item_id": "test_a",
        "content": "atlas budget",
        "memory_type": "semantic",
        "created_at": TIME,
    }
    status = {"extraction_state": "extraction_pending", "revision": 1}
    assert (
        _item_to_recall_dict({**base, "read_status": status})["read_status"] == status
    )
    assert "read_status" not in _item_to_recall_dict(base)
    with_line = format_recall_lines([{**base, "read_status": status}], top_k=5)
    bare_line = format_recall_lines([base], top_k=5)
    assert "[extraction_pending]" in with_line
    assert "extraction_pending" not in bare_line


@pytest.mark.parametrize("on", [False, True])
def test_storage_recall_line_follows_the_gate(lane, on):
    with switch_override(SWITCH, on):
        text = storage.recall(None, 5, query="atlas budget", include_snapshot=False)
    assert ("[extraction_pending]" in text) is on
    assert ("[unavailable]" in text) is on
    assert "test_legacy" in text


def test_storage_search_filter_path_carries_status(lane):
    with switch_override(SWITCH, True):
        rows = _by_id(
            storage.search("atlas budget", top_k=10, filters={"origin": "user:test"})
        )
    assert rows["test_legacy"]["read_status"]["extraction_state"] == "unavailable"


# C8 table: a present-but-unreadable carrier is a typed refusal with exactly one WARNING, never read as absent.
MALFORMED = [
    ("string", "extraction_pending", "a str, not a dict"),
    ("list", ["extraction_pending"], "a list, not a dict"),
    ("int", 7, "a int, not a dict"),
    ("empty-string", "", "a str, not a dict"),
    ("false", False, "a bool, not a dict"),
    ("dict-without-state", {"revision": 1}, "a dict without a string extraction_state"),
    (
        "dict-empty-state",
        {"extraction_state": ""},
        "a dict without a string extraction_state",
    ),
    (
        "dict-int-state",
        {"extraction_state": 3},
        "a dict without a string extraction_state",
    ),
]


@pytest.mark.parametrize(
    "name,carrier,reason", MALFORMED, ids=[m[0] for m in MALFORMED]
)
def test_malformed_carrier_is_a_typed_refusal_at_both_sites(
    name, carrier, reason, caplog
):
    row = {
        "item_id": "test_bad",
        "content": "atlas",
        "memory_type": "semantic",
        "read_status": carrier,
    }
    expected = (
        "WARNING",
        f"read_status carrier on test_bad is {reason}: refusing to normalize the row without its extraction status",
    )
    for site in (_item_to_recall_dict, lambda r: format_recall_lines([r], top_k=5)):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="smartmemory_app.recall_format"):
            with pytest.raises(MalformedReadStatusError):
                site(row)
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [expected]


def test_absent_carrier_stays_legacy_and_silent(caplog):
    row = {
        "item_id": "test_a",
        "content": "atlas",
        "memory_type": "semantic",
        "read_status": None,
    }
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.recall_format"):
        assert "read_status" not in _item_to_recall_dict(row)
        assert "[" in format_recall_lines([row], top_k=5)
    assert [r for r in caplog.records if "read_status" in r.getMessage()] == []
