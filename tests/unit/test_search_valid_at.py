"""DHRUV-LOCAL-VALIDAT-WRAPPER-1: dated local searches never degrade silently."""

import inspect
import logging
from datetime import datetime, timezone
from functools import wraps

import pytest
from smartmemory import SmartMemory

from smartmemory_app import storage

requires_valid_at = pytest.mark.skipif(
    "valid_at" not in inspect.signature(SmartMemory.search).parameters,
    reason="valid-time regression requires core with valid_at support",
)


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from smartmemory.tools.factory import create_lite_memory

    memory = create_lite_memory(
        data_dir=str(tmp_path / "test_valid_at_lite"), spawn_worker=False
    )
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    try:
        yield memory
    finally:
        memory.close()


@pytest.mark.parametrize(
    ("query", "filters"), [("", None), ("*", None), ("", {"project": "atlas"})]
)
@requires_valid_at
def test_valid_at_reaches_real_core(lite, monkeypatch, query, filters):
    from smartmemory.models.memory_item import MemoryItem

    assert "valid_at" in inspect.signature(lite.search).parameters, (
        "This regression row requires a core with valid_at support"
    )
    item_id = lite.add(
        MemoryItem(
            content="test_valid_at: coding rule applies from March",
            memory_type="semantic",
            valid_start_time=datetime(2026, 3, 1, tzinfo=timezone.utc),
            metadata={"project": "atlas"},
        )
    )
    received = []
    real_search = lite.search

    @wraps(real_search)
    def record(*args, **kwargs):
        received.append(kwargs.copy())
        return real_search(*args, **kwargs)

    monkeypatch.setattr(lite, "search", record)
    results = storage.search(
        query, filters=filters, valid_at="2026-04-10", sort_by="recency"
    )
    assert received and all(c["valid_at"] == "2026-04-10" for c in received)
    assert item_id in [r["item_id"] for r in results]
    assert all(r["metadata"]["valid_at_resolution"] == "valid_time" for r in results)


def test_unknown_kwargs_warn_once_and_limit_stays_excluded(lite, caplog):
    with caplog.at_level(logging.WARNING, logger=storage.__name__):
        storage.search(
            "", sort_by="recency", future_option=True, another_option=None, limit=99
        )
    warnings = [r for r in caplog.records if r.name == storage.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert all(
        key in warnings[0].message
        for key in ("future_option", "another_option", "limit")
    )
    assert "limit" not in storage._ALLOWED


@pytest.mark.parametrize(
    "options",
    [
        {"valid_at": "2026-04-10"},
        {"manual_scan": False},
        {"manual_scan": False, "valid_at": "2026-04-10"},
        {key: True for key in storage._ALLOWED - {"sort_by", "origin"}},
    ],
    ids=["valid-at", "manual-scan", "manual-scan-and-valid-at", "all-options"],
)
def test_older_core_warns_instead_of_raising(lite, monkeypatch, caplog, options):
    from smartmemory.models.memory_item import MemoryItem

    item_id = lite.add(
        MemoryItem(content="test_valid_at: older core result", memory_type="semantic")
    )
    real_search = lite.search
    calls = []

    # Reproduce the older facade signature, while executing a real Lite search.
    def old_search(query, top_k=5, sort_by=None):
        calls.append((query, top_k, sort_by))
        return real_search(query, top_k=top_k, sort_by=sort_by)

    monkeypatch.setattr(lite, "search", old_search)
    with caplog.at_level(logging.WARNING, logger=storage.__name__):
        results = storage.search("", sort_by="recency", **options)
    assert item_id in [r["item_id"] for r in results]
    assert calls == [("", 5, "recency")]
    warnings = [r for r in caplog.records if r.name == storage.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert all(key in warnings[0].message for key in options)
    if "valid_at" in options:
        assert "historical" in warnings[0].message.lower()


@requires_valid_at
def test_local_backend_reaches_real_wrapper(lite, monkeypatch):
    LocalBackend = pytest.importorskip("smartmemory_mcp.backends.local").LocalBackend

    received = []
    real_search = lite.search

    @wraps(real_search)
    def record(*args, **kwargs):
        received.append(kwargs.copy())
        return real_search(*args, **kwargs)

    monkeypatch.setattr(lite, "search", record)
    assert LocalBackend().search("", valid_at="2026-04-10", sort_by="recency") == []
    assert received[0]["valid_at"] == "2026-04-10"
