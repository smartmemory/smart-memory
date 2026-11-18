"""U5.22 wrapper search/get/wildcard/recall carry the merged-read extraction status. # trace G:G43,G46,G114,G136,G142

Real SQLite lite memory with an in-memory pending store. The read gate is always on: a receipt-only
item must be listed with `read_status`, graph-only items carry the neutral
"unavailable" status.
"""

import logging

import pytest

from smartmemory.models.read_status import READ_STATUS_KEY
from smartmemory.save.read_port import (
    AdmissionRow,
    InMemoryPendingStore,
    LexicalRecord,
    ScopeKeys,
    source_sha256,
)
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
    # F3: everything after the memory exists runs inside the try, so a setup failure (admission, the graph row,
    # the pending-read binding) still closes it.
    try:
        store = InMemoryPendingStore()
        row = _row()
        store.admit(
            row, LexicalRecord(row.item_id, 1, TEXT, source_sha256(TEXT), SCOPE)
        )
        mem._graph.backend.add_node(
            "test_legacy",
            {
                "content": "atlas budget stored text",
                "origin": "user:test",
                "created_at": TIME,
            },
            memory_type="semantic",
        )
        mem.bind_pending_reads(store, workspace_hidden=False)
        monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
        yield mem, store
    finally:
        mem.close()


def _by_id(rows):
    return {r["item_id"]: r for r in rows}


def test_wildcard_gate_on_lists_pending_and_stamps_graph_rows(lane):
    got = _by_id(storage.search("*"))
    assert (
        got["test_pending"]["read_status"]["extraction_state"] == "extraction_pending"
    )
    assert got["test_legacy"]["read_status"]["extraction_state"] == "unavailable"


def test_wildcard_gate_on_applies_filters_and_type_to_pending(lane):
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


def _bounded_search(monkeypatch, mem, limit=5, extra=0):
    """Wrap the real core search: fail the test instead of looping forever (the r1 cap mutation hung), and
    optionally ask core for ``extra`` more rows than the window to break the window contract (C4 fault probe)."""
    real = mem.search
    calls = []

    def search(query, *args, top_k=5, **kwargs):
        calls.append(top_k)
        if len(calls) > limit:
            pytest.fail(f"wildcard enumeration did not terminate: windows {calls}")
        return real(query, *args, top_k=top_k + extra, **kwargs)

    monkeypatch.setattr(mem, "search", search)
    return calls


def test_wildcard_gate_on_full_window_at_cap_is_a_typed_refusal(
    lane, caplog, monkeypatch
):
    mem, _store = lane
    calls = _bounded_search(monkeypatch, mem)
    monkeypatch.setattr(storage, "_WILDCARD_PENDING_WINDOW", 1)
    monkeypatch.setattr(storage, "_WILDCARD_CORE_CAP", 1)
    caplog.set_level(logging.WARNING)
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
    assert calls == [1]


def test_wildcard_over_return_is_a_typed_refusal(lane, caplog, monkeypatch):
    """C4 fault probe: a real core search that returns more rows than the window broke the listing bound."""
    mem, _store = lane
    calls = _bounded_search(monkeypatch, mem, extra=1)
    monkeypatch.setattr(storage, "_WILDCARD_PENDING_WINDOW", 1)
    caplog.set_level(logging.WARNING)
    with pytest.raises(
        storage.WildcardEnumerationOverflowError,
        match="^wildcard enumeration returned 2 hits for a window of 1$",
    ):
        storage.search("*")
    assert calls == [1]
    assert [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == storage.log.name
    ] == [
        (
            "WARNING",
            "wildcard search refused: core returned 2 hits for a window of 1, so the listing bound is broken",
        )
    ]


def test_query_search_and_get_carry_the_gate_status(lane):
    hits = _by_id(
        storage.search("atlas budget", top_k=10, reranker="none", enable_hybrid=False)
    )
    got = storage.get("test_pending")
    assert "test_pending" in hits
    assert (
        hits["test_pending"]["read_status"]["extraction_state"] == "extraction_pending"
    )
    assert got["read_status"]["extraction_state"] == "extraction_pending"


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


def test_storage_recall_line_carries_the_gate_status(lane):
    text = storage.recall(None, 5, query="atlas budget", include_snapshot=False)
    assert "[extraction_pending]" in text
    assert "[unavailable]" in text
    assert "test_legacy" in text


def test_storage_search_filter_path_carries_status(lane):
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
    ("empty-dict", {}, "a dict without a string extraction_state"),
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


def _stamped(mem, carrier):
    """A real core MemoryItem whose response-only carrier is unreadable (the shape core's to_dict would copy)."""
    item = mem.get("test_legacy")
    item.__dict__[READ_STATUS_KEY] = carrier
    return item


def _site_search(mem, carrier, monkeypatch):
    monkeypatch.setattr(mem, "search", lambda *a, **k: [_stamped(mem, carrier)])
    return storage.search(
        "atlas budget", top_k=10, reranker="none", enable_hybrid=False
    )


def _site_grouped(mem, carrier, monkeypatch):
    monkeypatch.setattr(
        mem, "search", lambda *a, **k: {"semantic": [_stamped(mem, carrier)]}
    )
    return storage.search("atlas budget", top_k=10)


def _site_filter(mem, carrier, monkeypatch):
    monkeypatch.setattr(mem, "search", lambda *a, **k: [_stamped(mem, carrier)])
    return storage.search("atlas budget", top_k=10, filters={"origin": "user:test"})


def _site_get(mem, carrier, monkeypatch):
    item = _stamped(mem, carrier)
    monkeypatch.setattr(mem, "get", lambda item_id: item)
    return storage.get("test_legacy")


def _site_wildcard(mem, carrier, monkeypatch):
    monkeypatch.setattr(mem, "search", lambda *a, **k: [_stamped(mem, carrier)])
    return storage.search("*")


# C8/C1 table: every storage site that serializes a core result (`_result_dict`) refuses an unreadable carrier on a
# real MemoryItem, never `to_dict()`'s raw ValueError/TypeError or a copied stateless dict. One row per malformed
# shape, each site at least once (the shapes themselves are the MALFORMED table above, through recall_format).
RESULT_SITE_ROWS = [
    ("search-string", _site_search, "extraction_pending", "a str, not a dict"),
    ("grouped-list", _site_grouped, ["extraction_pending"], "a list, not a dict"),
    ("filter-int", _site_filter, 7, "a int, not a dict"),
    ("get-empty-string", _site_get, "", "a str, not a dict"),
    ("wildcard-false", _site_wildcard, False, "a bool, not a dict"),
    ("search-empty-dict", _site_search, {}, "a dict without a string extraction_state"),
    (
        "grouped-dict-without-state",
        _site_grouped,
        {"revision": 1},
        "a dict without a string extraction_state",
    ),
    (
        "get-dict-empty-state",
        _site_get,
        {"extraction_state": ""},
        "a dict without a string extraction_state",
    ),
    (
        "wildcard-dict-int-state",
        _site_wildcard,
        {"extraction_state": 3},
        "a dict without a string extraction_state",
    ),
    (
        "filter-dict-without-state",
        _site_filter,
        {"revision": 1},
        "a dict without a string extraction_state",
    ),
]


@pytest.mark.parametrize(
    "name,site,carrier,reason", RESULT_SITE_ROWS, ids=[r[0] for r in RESULT_SITE_ROWS]
)
def test_result_sites_refuse_an_unreadable_carrier(
    lane, monkeypatch, caplog, name, site, carrier, reason
):
    mem, _store = lane
    caplog.set_level(logging.WARNING)
    with pytest.raises(MalformedReadStatusError) as raised:
        site(mem, carrier, monkeypatch)
    assert str(raised.value) == f"read_status on test_legacy is {reason}"
    assert [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == "smartmemory_app.recall_format"
    ] == [
        (
            "WARNING",
            f"read_status carrier on test_legacy is {reason}: refusing to normalize the row without its extraction status",
        )
    ]


def test_lane_fixture_closes_memory_when_setup_fails_after_it_exists(
    tmp_path, monkeypatch
):
    """F3: a failure after the lite memory exists (here: binding pending reads) still closes it."""
    import sys

    module = sys.modules[__name__]
    opened = []
    factory = create_lite_memory  # the real one, captured before the module global is replaced

    def tracked_factory(*args, **kwargs):
        mem = factory(*args, **kwargs)
        record = {"closed": False}
        opened.append(record)
        close = mem.close

        def tracked_close():
            record["closed"] = True
            return close()

        def fail_binding(*a, **k):
            raise RuntimeError("test_u522 injected binding failure")

        mem.close = tracked_close
        mem.bind_pending_reads = fail_binding
        return mem

    monkeypatch.setattr(module, "create_lite_memory", tracked_factory)
    gen = lane.__wrapped__(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="injected binding failure"):
        next(gen)
    assert [r["closed"] for r in opened] == [True]
