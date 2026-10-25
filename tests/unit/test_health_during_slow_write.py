"""DAEMON-HEALTH-STARVE-1: /health answers while a slow lifecycle write is in flight.

The lifecycle routes used to run synchronous store work on the event loop, so one
hook call stalled every request and the daemon looked dead. Each row starts one slow
route call and, on the same loop, a /health probe; the probe must finish while the
write is still running.
"""

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx

SLOW_S = 1.5
HEALTH_BUDGET_S = 0.75

# (route, json body): every /lifecycle write or read route that touches the store.
ROWS = [
    ("/lifecycle/orient", {"session_id": "s", "cwd": "/tmp"}),
    ("/lifecycle/recall", {"session_id": "s", "prompt": "p", "cwd": "/tmp"}),
    ("/lifecycle/observe", {"session_id": "s", "tool_name": "Bash"}),
    ("/lifecycle/distill", {"session_id": "s", "last_assistant_message": "m"}),
    ("/lifecycle/learn", {"session_id": "s", "tool_name": "Bash", "error": "e"}),
    ("/lifecycle/persist", {"session_id": "s", "cwd": "/tmp"}),
]


class _SlowLifecycle:
    def __init__(self, started: threading.Event):
        self._started = started

    def _slow(self, *args, **kwargs):
        self._started.set()
        time.sleep(SLOW_S)
        return ""

    orient = recall = persist = _slow


def _health_patches():
    config = SimpleNamespace(
        mode="local", llm_provider="none", embedding_provider="local"
    )
    backend = SimpleNamespace(serialize=lambda: {"nodes": []})
    memory = SimpleNamespace(_graph=SimpleNamespace(backend=backend))
    return [
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", return_value=memory),
        patch("smartmemory_app.work_graph.get_work_status", return_value={}),
        patch("smartmemory_app.work_graph.get_work_graph", return_value=None),
        patch(
            "smartmemory.pipeline.work_graph.reextract.reextract_offer",
            return_value=None,
        ),
    ]


async def _probe(app, route, body, started):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://daemon"
    ) as client:
        write = asyncio.create_task(client.post(route, json=body))
        while not started.is_set():  # the slow body is now running
            await asyncio.sleep(0.01)
        begin = time.monotonic()
        health = await client.get("/health")
        health_s = time.monotonic() - begin
        write_done_first = write.done()
        response = await write
    return health, health_s, write_done_first, response


def test_health_answers_while_each_lifecycle_route_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    from smartmemory_app import lifecycle_api
    from smartmemory_app.viewer_server import _build_app

    app = _build_app()
    failures = []
    for route, body in ROWS:
        started = threading.Event()
        slow = _SlowLifecycle(started)

        def slow_phase(lc, phase, payload, _slow=slow):
            _slow._slow()

        patches = _health_patches() + [
            patch.object(lifecycle_api, "_get_lifecycle", return_value=slow),
            patch.object(lifecycle_api, "run_phase", side_effect=slow_phase),
        ]
        for p in patches:
            p.start()
        try:
            health, health_s, write_done_first, response = asyncio.run(
                _probe(app, route, body, started)
            )
        finally:
            for p in reversed(patches):
                p.stop()
        row = (
            route,
            health.status_code,
            round(health_s, 2),
            write_done_first,
            response.status_code,
        )
        if (
            health.status_code != 200
            or health_s > HEALTH_BUDGET_S
            or write_done_first
            or response.status_code != 200
        ):
            failures.append(row)
    assert failures == [], (
        f"(route, health status, health s, write finished first, write status): {failures}"
    )


def test_health_counts_sqlite_memories_without_serializing():
    """The Lite backend is counted with one SQL COUNT; Version nodes are excluded."""
    from smartmemory_app.viewer_server import _memory_count

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE nodes (item_id TEXT PRIMARY KEY, memory_type TEXT)")
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?)",
        [("a", "episodic"), ("b", "semantic"), ("c", "Version"), ("d", None)],
    )

    def serialize():
        raise AssertionError("health must not serialize the whole store")

    backend = SimpleNamespace(_conn=conn, _lock=threading.RLock(), serialize=serialize)
    assert _memory_count(backend) == 3
