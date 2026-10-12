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
from smartmemory_app.recall_format import _item_to_recall_dict, format_recall_lines

SCOPE = ScopeKeys(None, "local", None)
TEXT = "atlas budget accepted full source text"
TIME = "2026-10-11T12:00:00.000000+00:00"


def _row(item_id="test_pending"):
    return AdmissionRow(
        item_id,
        1,
        "test_run",
        TIME,
        "semantic",
        "atlas",
        "atlas budget",
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
        yield mem
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


def test_wildcard_bound_hit_warns_once(lane, caplog, monkeypatch):
    monkeypatch.setattr(storage, "_WILDCARD_PENDING_WINDOW", 2)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True):
        storage.search("*")
    msgs = [
        r for r in caplog.records if "wildcard search merged only" in r.getMessage()
    ]
    assert len(msgs) == 1


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
