"""SM-LIFECYCLE-POOL-SPLIT-1: reads and writes no longer starve each other in the daemon.

DAEMON-RECALL-STARVE-1 (2026-10-30): orient, recall, observe, distill, learn and
persist shared one 4-thread pool. Recalls that ran for minutes held every slot, so
observe, persist and every later recall queued behind them, and a request whose
client had long given up still ran when it reached a slot. Each row drives the real
routes through ASGI with fake lifecycle work (no store, no daemon, no network).
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI

from smartmemory_app import cli, lifecycle_api

BLOCK_S = 10  # a "stuck" fake never waits longer than this, released at row end
FAST_S = 1.0  # a request that must not wait for the stuck ones finishes inside this


class _Fake:
    """Lifecycle stand-in: records calls; blocks while ``gate`` is clear."""

    def __init__(self, gate: threading.Event, block: set):
        self.gate, self.block = gate, block
        self.calls: list[str] = []
        self.started = threading.Semaphore(0)

    def _work(self, phase, value):
        self.calls.append(phase)
        self.started.release()
        if phase in self.block:
            self.gate.wait(BLOCK_S)
        return value

    def recall(self, prompt, cwd=None):
        return self._work("recall", f"test_ctx:{prompt}")

    def orient(self, cwd=None):
        return self._work("orient", "test_orient_ctx")

    def persist(self, cwd=None, transcript_path=None):
        return self._work("persist", None)


@pytest.fixture
def daemon(monkeypatch):
    gate = threading.Event()
    state = {"block": set()}
    fake = _Fake(gate, state["block"])
    monkeypatch.setattr(lifecycle_api, "_get_lifecycle", lambda body: fake)
    monkeypatch.setattr(
        lifecycle_api, "run_phase", lambda lc, phase, body: fake._work(phase, None)
    )
    read_pool = ThreadPoolExecutor(2, thread_name_prefix="test_pool_split_read")
    write_pool = ThreadPoolExecutor(2, thread_name_prefix="test_pool_split_write")
    monkeypatch.setattr(lifecycle_api, "_read_pool", read_pool)
    monkeypatch.setattr(lifecycle_api, "_phase_pool", write_pool)
    app = FastAPI()
    app.include_router(lifecycle_api.lifecycle_router, prefix="/lifecycle")
    yield app, fake
    gate.set()  # release every stuck fake on pass and fail alike
    read_pool.shutdown(wait=True)
    write_pool.shutdown(wait=True)


async def _post(client, path, body):
    begin = time.monotonic()
    response = await client.post(path, json=body)
    return response, time.monotonic() - begin


async def _wait_started(fake, n):
    for _ in range(n):
        assert await asyncio.to_thread(fake.started.acquire, True, 5)


def _recall(prompt="p", **extra):
    return {"session_id": "test_pool_split", "prompt": prompt, "cwd": "/tmp", **extra}


# Each row: (name, phases that block, cap seconds, scenario). A scenario fills a pool
# with stuck work in the background, then times the request under test.


async def stuck_recall_vs_writes(client, fake):
    held = [
        asyncio.create_task(_post(client, "/lifecycle/recall", _recall(f"s{i}")))
        for i in range(2)
    ]
    await _wait_started(fake, 2)
    observe, observe_s = await _post(client, "/lifecycle/observe", {"session_id": "s"})
    persist, persist_s = await _post(client, "/lifecycle/persist", {"session_id": "s"})
    return {
        "observe": (observe.status_code, observe.json(), observe_s < FAST_S),
        "persist": (persist.status_code, persist.json(), persist_s < FAST_S),
        "held_pending": not any(t.done() for t in held),
    }


async def observe_flood_vs_recall(client, fake):
    flood = [
        asyncio.create_task(
            _post(client, "/lifecycle/observe", {"session_id": f"s{i}"})
        )
        for i in range(6)
    ]
    await _wait_started(fake, 2)
    recall, recall_s = await _post(client, "/lifecycle/recall", _recall("fresh"))
    return {
        "recall": (recall.status_code, recall.json(), recall_s < FAST_S),
        "flood_pending": not any(t.done() for t in flood),
    }


async def expired_deadline_dropped(client, fake):
    body = _recall("late", **{lifecycle_api.CLIENT_DEADLINE_KEY: time.time() - 1})
    recall, recall_s = await _post(client, "/lifecycle/recall", body)
    return {
        "recall": (recall.status_code, recall.json(), recall_s < FAST_S),
        "calls": list(fake.calls),
    }


async def recall_past_cap(client, fake):
    recall, recall_s = await _post(client, "/lifecycle/recall", _recall("slow"))
    return {
        "recall": (recall.status_code, recall.json(), recall_s < FAST_S),
        "calls": list(fake.calls),
    }


async def queued_past_cap_never_runs(client, fake):
    held = [
        asyncio.create_task(_post(client, "/lifecycle/recall", _recall(f"s{i}")))
        for i in range(2)
    ]
    await _wait_started(fake, 2)
    queued, queued_s = await _post(client, "/lifecycle/recall", _recall("queued"))
    await asyncio.gather(*held)  # the cap answers them; their workers stay stuck
    fake.gate.set()
    await asyncio.sleep(0.3)  # time for a wrongly kept queued job to reach a slot
    return {
        "recall": (queued.status_code, queued.json(), queued_s < FAST_S),
        "calls": list(fake.calls),
    }


async def normal_recall(client, fake):
    recall, recall_s = await _post(
        client,
        "/lifecycle/recall",
        _recall("ok", **{lifecycle_api.CLIENT_DEADLINE_KEY: time.time() + 5}),
    )
    orient, _ = await _post(
        client, "/lifecycle/orient", {"session_id": "s", "cwd": "/tmp"}
    )
    return {
        "recall": (recall.status_code, recall.json(), recall_s < FAST_S),
        "orient": (orient.status_code, orient.json()),
        "calls": list(fake.calls),
    }


EMPTY = {"context": ""}
ROWS = [
    (
        "stuck recall does not delay observe or persist",
        {"recall"},
        30.0,
        stuck_recall_vs_writes,
        {
            "observe": (200, {"status": "ok"}, True),
            "persist": (200, {"status": "ok"}, True),
            "held_pending": True,
        },
    ),
    (
        "observe flood does not delay recall",
        {"observe"},
        30.0,
        observe_flood_vs_recall,
        {"recall": (200, {"context": "test_ctx:fresh"}, True), "flood_pending": True},
    ),
    (
        "expired client deadline is dropped without calling lc.recall",
        set(),
        30.0,
        expired_deadline_dropped,
        {"recall": (200, EMPTY, True), "calls": []},
    ),
    (
        "recall past the cap answers the empty shape in bounded time",
        {"recall"},
        0.3,
        recall_past_cap,
        {"recall": (200, EMPTY, True), "calls": ["recall"]},
    ),
    (
        "recall queued past the cap never runs",
        {"recall"},
        0.3,
        queued_past_cap_never_runs,
        {"recall": (200, EMPTY, True), "calls": ["recall", "recall"]},
    ),
    (
        "normal recall and orient unchanged",
        set(),
        4.5,
        normal_recall,
        {
            "recall": (200, {"context": "test_ctx:ok"}, True),
            "orient": (200, {"context": "test_orient_ctx"}),
            "calls": ["recall", "orient"],
        },
    ),
]


def test_lifecycle_pools_and_read_deadlines(daemon, monkeypatch):
    app, fake = daemon
    failures = []
    for name, block, cap, scenario, expected in ROWS:
        fake.calls.clear()
        fake.block.clear()
        fake.block.update(block)
        fake.gate.clear()
        fake.started = threading.Semaphore(0)
        monkeypatch.setattr(lifecycle_api, "_RECALL_CAP_S", cap)

        async def run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://daemon"
            ) as client:
                try:
                    return await scenario(client, fake)
                finally:
                    fake.gate.set()
                    stragglers = asyncio.all_tasks() - {asyncio.current_task()}
                    await asyncio.gather(*stragglers, return_exceptions=True)

        got = asyncio.run(run())
        time.sleep(0.05)  # let released fakes leave their slots before the next row
        if got != expected:
            failures.append((name, got))
    assert [name for name, _ in failures] == [], failures


def test_client_stamps_deadline_on_reads_only(monkeypatch):
    sent = {}

    def handler(request):
        sent[request.url.path] = request.read()
        return httpx.Response(200, json={"context": ""}, request=request)

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: original(
            *a, **{**k, "transport": httpx.MockTransport(handler)}
        ),
    )
    monkeypatch.setattr(cli, "_daemon_marker_live", lambda: True)
    assert cli._DAEMON_DEADLINE_KEY == lifecycle_api.CLIENT_DEADLINE_KEY
    before = time.time()
    for path in ("/lifecycle/recall", "/lifecycle/orient", "/lifecycle/observe"):
        cli._lifecycle_via_daemon(path, {"session_id": "s"}, timeout=5.0)
    import json

    stamped = {
        path: json.loads(raw).get(cli._DAEMON_DEADLINE_KEY)
        for path, raw in sent.items()
    }
    assert stamped["/lifecycle/observe"] is None
    for path in ("/lifecycle/recall", "/lifecycle/orient"):
        assert before + 5.0 <= stamped[path] <= time.time() + 5.0, path
