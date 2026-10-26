"""HOOK-CONSUMER-SCAN-1: /health never waits on the store.

Each row holds one lock or one store read for the whole probe and asks /health: it must answer
200 inside the budget, with a memory count or an explicit ``stale`` marker, never by waiting out
the holder. Rows cover the backend connection lock (a SQLite file, and a backend with no file),
the viewer's read/write lock, and the two other store-touching fields (re-extract offer, work
status). A last row proves a free store still answers with the real count and no marker.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BUDGET_S = 0.2
HOLD_S = 5.0


def _file_backend(tmp_path):
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE nodes (item_id TEXT PRIMARY KEY, memory_type TEXT)")
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?)",
        [("a", "episodic"), ("b", "semantic"), ("c", "Version")],
    )
    conn.commit()
    return SimpleNamespace(
        cache_namespace=f"sqlite:{db}",
        _conn=conn,
        _lock=threading.RLock(),
        serialize=lambda: (_ for _ in ()).throw(AssertionError("serialized")),
    )


def _fileless_backend():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE nodes (item_id TEXT PRIMARY KEY, memory_type TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('a', 'episodic')")
    return SimpleNamespace(
        cache_namespace=None,
        _conn=conn,
        _lock=threading.RLock(),
        serialize=lambda: {"nodes": []},
    )


def _serialize_backend():
    return SimpleNamespace(serialize=lambda: {"nodes": [{"memory_type": "x"}]})


# name, backend factory, what is held (backend lock | rw lock | slow offer | slow work),
# the count when the store is free
ROWS = [
    ("sqlite file, backend lock held", lambda p: _file_backend(p), "backend", 2),
    ("no file, backend lock held", lambda p: _fileless_backend(), "backend", 1),
    ("no file or conn, rw lock held", lambda p: _serialize_backend(), "rw", 1),
    ("offer scan slow", lambda p: _file_backend(p), "offer", 2),
    ("work status slow", lambda p: _file_backend(p), "work", 2),
]


def _client(backend, offer, work):
    config = SimpleNamespace(mode="local", llm_provider="none", embedding_provider="local")
    memory = SimpleNamespace(_graph=SimpleNamespace(backend=backend))
    patches = [
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", return_value=memory),
        patch("smartmemory_app.work_graph.get_work_status", side_effect=work),
        patch("smartmemory_app.work_graph.get_work_graph", return_value=None),
        patch("smartmemory.pipeline.work_graph.reextract.reextract_offer", side_effect=offer),
    ]
    for p in patches:
        p.start()
    from smartmemory_app.viewer_server import _build_app

    return TestClient(_build_app()), patches


def test_health_answers_while_the_store_is_busy(tmp_path):
    from smartmemory_app import local_api

    failures = []
    for name, make, held, _ in ROWS:
        release = threading.Event()
        backend = make(tmp_path)

        def slow(kind):
            def run(*a, **k):
                if held == kind:
                    release.wait(HOLD_S)
                return None if kind == "offer" else {}

            return run

        client, patches = _client(backend, slow("offer"), slow("work"))
        lock = {"backend": getattr(backend, "_lock", None), "rw": local_api._rw_lock}.get(held)
        holder = None
        if lock is not None:
            taken = threading.Event()

            def hold(lock=lock, taken=taken):
                with lock:
                    taken.set()
                    release.wait(HOLD_S)

            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            assert taken.wait(2), name
        try:
            begin = time.monotonic()
            response = client.get("/health")
            elapsed = time.monotonic() - begin
            body = response.json()
            counted = isinstance(body.get("memories"), int) and body["memories"] >= 0
            marked = "stale" in body
            if response.status_code != 200 or elapsed > BUDGET_S or not (counted or marked):
                failures.append((name, response.status_code, round(elapsed, 3), body.get("memories"), body.get("stale")))
            elif body["status"] != "ok":
                failures.append((name, "status", body["status"]))
        finally:
            release.set()
            if holder:
                holder.join(5)
            for p in reversed(patches):
                p.stop()
    assert failures == [], f"(row, status, seconds, memories, stale): {failures}"


def test_free_store_answers_with_the_real_count_and_no_marker(tmp_path):
    failures = []
    for name, make, held, want in ROWS:
        client, patches = _client(make(tmp_path), lambda *a, **k: None, lambda *a, **k: {})
        try:
            body = client.get("/health").json()
        finally:
            for p in reversed(patches):
                p.stop()
        if (body["memories"], "stale" in body) != (want, False):
            failures.append((name, body["memories"], body.get("stale"), want))
    assert failures == [], f"(row, memories, stale, wanted count): {failures}"
