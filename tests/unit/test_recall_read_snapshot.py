"""LITE-RECALL-READ-SNAPSHOT-1: hook recall answers while the hook-queue drain writes.

A live Lite store answered 0 of 5 recalls at the 4.5 s cap while the drain held the store's graph lock: the recall's
reads queued behind the writer, and the route sent "" even when the worker already held rows. Each test names what it
proves:
- the read route answers with the latest partial answer at its cap, with a WARNING (no store);
- the decision lookup filters workspaces in SQL and keeps exactly the rows the old full scan kept, strict or not;
  a core without the SQL filter loads them all as before, with one WARNING (real SQLite, no models);
- on a real Lite store, a recall while a writer holds the graph RLock answers in full inside the cap, and while the
  writer also holds the vector index lock the route answers with the decision rows found so far.
"""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from smartmemory_app import lifecycle_api, recall_format, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.lifecycle_config import LifecycleConfig, RecallStrategy

BLOCK_S = 10.0  # a held writer or a stuck fake never waits longer than this
CAP_S = 2.0
WS = "ws_test_recall_snapshot"


def _app(monkeypatch, lifecycle, cap=CAP_S):
    pool = ThreadPoolExecutor(2, thread_name_prefix="test_recall_snapshot_read")
    monkeypatch.setattr(lifecycle_api, "_read_pool", pool)
    monkeypatch.setattr(lifecycle_api, "_get_lifecycle", lambda body: lifecycle)
    monkeypatch.setattr(lifecycle_api, "_RECALL_CAP_S", cap)
    app = FastAPI()
    app.include_router(lifecycle_api.lifecycle_router, prefix="/lifecycle")
    return app, pool


def _post_recall(app, prompt):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon"
        ) as client:
            begin = time.monotonic()
            response = await client.post(
                "/lifecycle/recall",
                json={
                    "session_id": "test_recall_snapshot",
                    "prompt": prompt,
                    "cwd": "/tmp",
                },
            )
            return response, time.monotonic() - begin

    return asyncio.run(run())


class _StuckRecall:
    """Offers ``offers`` as partial answers, then blocks until released (a recall stuck behind a lock)."""

    def __init__(self, offers):
        self.offers, self.release = offers, threading.Event()

    def recall(self, prompt, cwd=None, deadline=None, on_partial=None):
        for text in self.offers:
            on_partial(text)
        self.release.wait(BLOCK_S)
        return "## SmartMemory Context\n\n- the full answer"


@pytest.mark.parametrize(
    "offers, expected, warning",
    [
        (
            [
                "## SmartMemory Context\n\n- one",
                "## SmartMemory Context\n\n- one\n- two",
            ],
            "- two",
            "answered partial",
        ),
        ([], "", "answered empty"),
    ],
    ids=["latest-partial", "nothing-offered"],
)
def test_route_answers_with_the_latest_partial_at_its_cap(
    monkeypatch, caplog, offers, expected, warning
):
    stuck = _StuckRecall(offers)
    app, pool = _app(monkeypatch, stuck, cap=0.5)
    try:
        with caplog.at_level(logging.WARNING, logger=lifecycle_api.log.name):
            response, elapsed = _post_recall(app, "why is recall empty")
    finally:
        stuck.release.set()
        pool.shutdown(wait=True)
    context = response.json()["context"]
    assert response.status_code == 200 and elapsed < 0.5 + 1.0
    assert context.endswith(expected) if expected else context == ""
    assert any(warning in r.getMessage() for r in caplog.records)


# (item_id, properties): the decision shapes a store holds.
DECISIONS = [
    ("top-A", {"workspace_id": WS}),
    ("nested-A", {"context_snapshot": {"workspace_id": WS}}),
    ("flat-A", {"context_snapshot__workspace_id": WS}),
    (
        "top-null-nested-A",
        {"workspace_id": None, "context_snapshot": {"workspace_id": WS}},
    ),
    (
        "top-A-over-nested-B",
        {"workspace_id": WS, "context_snapshot": {"workspace_id": "ws_b"}},
    ),
    ("top-B", {"workspace_id": "ws_b"}),
    ("nested-B", {"context_snapshot": {"workspace_id": "ws_b"}}),
    ("unscoped", {}),
]


def _kept(rows, workspace_id, strict):
    """The workspace clause of ``storage.recall``'s ``eligible``: what the old full scan kept."""
    kept = set()
    for row in rows:
        meta = recall_format._item_to_recall_dict(row)["metadata"]
        r_ws = meta.get("workspace_id") if isinstance(meta, dict) else None
        if not (workspace_id and r_ws != workspace_id and (r_ws is not None or strict)):
            kept.add(row.item_id)
    return kept


@pytest.fixture
def decision_graph(tmp_path):
    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory.graph.smartgraph import SmartGraph

    backend = SQLiteBackend(db_path=str(tmp_path / "memory.db"))
    for item_id, props in DECISIONS:
        backend.add_node(
            item_id,
            {
                "item_id": item_id,
                "content": item_id,
                "memory_type": "decision",
                **props,
            },
            memory_type="decision",
        )
    try:
        yield SmartGraph(backend=backend)
    finally:
        backend.close()


def test_decision_lookup_keeps_what_the_full_scan_kept(
    decision_graph, monkeypatch, caplog
):
    if not callable(getattr(decision_graph, "search_nodes_in_workspace", None)):
        pytest.skip(
            "needs smartmemory-core with search_nodes_in_workspace (LITE-RECALL-READ-SNAPSHOT-1)"
        )
    monkeypatch.setattr(storage, "_RECALL_FALLBACK_WARNED", set())
    mem = SimpleNamespace(_graph=decision_graph)
    full = decision_graph.search_nodes({"memory_type": "decision"})
    with caplog.at_level(logging.WARNING, logger=storage.log.name):
        for workspace_id in (WS, "ws_b", "ws_none"):
            for strict in (False, True):
                scoped = storage._workspace_decisions(mem, workspace_id, strict)
                assert len(scoped) < len(full)  # the filter ran in SQL
                assert _kept(scoped, workspace_id, strict) == _kept(
                    full, workspace_id, strict
                ), (workspace_id, strict)
    assert not [
        r for r in caplog.records if "filters workspaces in Python" in r.getMessage()
    ]


def test_decision_lookup_without_the_sql_filter_scans_and_warns_once(
    decision_graph, monkeypatch, caplog
):
    monkeypatch.setattr(storage, "_RECALL_FALLBACK_WARNED", set())
    mem = SimpleNamespace(
        _graph=SimpleNamespace(search_nodes=decision_graph.search_nodes)
    )
    with caplog.at_level(logging.WARNING, logger=storage.log.name):
        first = storage._workspace_decisions(mem, WS, False)
        storage._workspace_decisions(mem, WS, False)
    assert len(first) == len(DECISIONS)
    assert (
        len(
            [
                r
                for r in caplog.records
                if "filters workspaces in Python" in r.getMessage()
            ]
        )
        == 1
    )


# ── real Lite store ─────────────────────────────────────────────────────────

QUERY = "why does recall answer empty during the drain snapshot"
DECISION = (
    "Recall reads use the WAL snapshot so the drain does not block recall answers"
)
MEMORIES = [
    "Recall answers empty when the read pool waits on the graph lock during a drain",
    "Use read_connection for WAL snapshot reads in the recall path",
    "The hook queue drain writes ingest batches under the store write lock",
]


@pytest.fixture
def lite_store(tmp_path, monkeypatch):
    from smartmemory.graph.backends.sqlite import SQLiteBackend

    if not callable(getattr(SQLiteBackend, "flush_deferred_access", None)):
        pytest.skip(
            "needs smartmemory-core with snapshot-safe search (LITE-RECALL-READ-SNAPSHOT-1)"
        )
    from smartmemory.tools.factory import create_lite_memory

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", WS)
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    try:
        mem = create_lite_memory(data_dir=str(tmp_path / "lite"), spawn_worker=False)
    except Exception as exc:  # missing local models: no network in tests
        pytest.skip(f"Lite models unavailable: {type(exc).__name__}")
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    try:
        for text in MEMORIES:
            storage.ingest(
                text,
                memory_type="semantic",
                properties={"workspace_id": WS},
                origin="cli:add",
            )
        mem.add_decision(
            content=DECISION,
            decision_type="choice",
            context_snapshot={"workspace_id": WS},
        )
        storage.recall(
            query=QUERY, workspace_id=WS, include_snapshot=False, top_k=5
        )  # warm the models
        yield mem
    finally:
        mem.close()


class _HeldWriter:
    """A hook-queue drain batch: the graph RLock in an open write transaction, optionally the vector index locks."""

    def __init__(self, mem, hold_vectors):
        backend = mem._graph.backend
        vector_locks = (
            [b._lock for b in mem._vector_backend._backends.values()]
            if hold_vectors
            else []
        )
        assert vector_locks or not hold_vectors  # the warm recall opened the index
        self.entered, self.release = threading.Event(), threading.Event()

        def run():
            with backend.transaction_context():
                backend._conn.execute(
                    "UPDATE nodes SET properties = json_set(properties, '$.held', 1) WHERE rowid = 1"
                )
                for lock in (
                    vector_locks
                ):  # lock order: graph RLock first, then the usearch locks
                    lock.acquire()
                try:
                    self.entered.set()
                    self.release.wait(BLOCK_S)
                finally:
                    for lock in reversed(vector_locks):
                        lock.release()

        self.thread = threading.Thread(target=run)

    def __enter__(self):
        self.thread.start()
        assert self.entered.wait(BLOCK_S)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(BLOCK_S)
        return False


@pytest.mark.parametrize(
    "hold_vectors, partial",
    [(False, False), (True, True)],
    ids=["graph-lock-held", "graph-and-vector-locks-held"],
)
def test_recall_answers_while_the_writer_holds_the_store(
    lite_store, monkeypatch, caplog, hold_vectors, partial
):
    lifecycle = MemoryLifecycle(
        "test_recall_snapshot",
        LifecycleConfig(recall_strategy=RecallStrategy.EVERY_PROMPT),
    )
    app, pool = _app(monkeypatch, lifecycle)
    try:
        with caplog.at_level(logging.WARNING), _HeldWriter(lite_store, hold_vectors):
            response, elapsed = _post_recall(app, QUERY)
    finally:
        pool.shutdown(wait=True)
    context = response.json()["context"]
    messages = [r.getMessage() for r in caplog.records]

    assert response.status_code == 200 and elapsed < CAP_S + 1.0
    assert (
        DECISION in context
    )  # the decision lookup ran on the snapshot, off the writer's lock
    assert any("answered partial" in m for m in messages) is partial
    assert not any("answered empty" in m for m in messages)
    if not partial:
        assert any(m in context for m in MEMORIES)  # the search lanes answered too
    assert not [
        m
        for m in messages
        if "readonly database" in m or "ReadConnectionWriteError" in m
    ]
