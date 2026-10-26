"""HOOK-CONSUMER-SHUTDOWN-RACE-1: the daemon never closes the store under running work,
and a daemon stop or a lost write lock does not use up a hook job's failure budget.

Two row tables, one test each. The shutdown rows run the daemon's real cleanup path
(``_stop_hook_consumer_then_close_store``; before this unit, ``_cleanup`` ran
``_stop_hook_consumer()`` then ``storage._shutdown()``, which the fallback reproduces)
against a real ``DaemonConsumer`` and the real ``/lifecycle`` phase pool, with a fake
store whose ``close()`` records what was still running on it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from smartmemory_app import capture_queue, hook_consumer, lifecycle_api, storage
from smartmemory_app import viewer_server as vs


def _queue_job(root: Path, name: str, **fields) -> Path:
    subdir = fields.pop("_dir", "queue")
    payload = root / "payloads" / f"{name}.payload.json"
    payload.write_text(json.dumps({"session_id": name}), encoding="utf-8")
    record = {
        "phase": "observe",
        "session_id": name,
        "payload_path": str(payload),
        **fields,
    }
    path = root / subdir / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SMARTMEMORY_HOOK_MAX_FREE_RETRIES", raising=False)
    yield capture_queue.hook_queue_dir()
    if hasattr(storage, "open_store_gate"):
        storage.open_store_gate()
    vs._shutdown_requested.clear()
    vs._hook_consumer = None


# ── shutdown: close only after the work on the store has ended ──────────────────────

# name, consumer job ends after (s, None = held past the check), lifecycle phase ends
# after (s, None = held, "-" = no phase running), expect closed, expect running/ left
SHUTDOWN_ROWS = [
    (
        "consumer and phase end inside the bounds: close after both",
        0.3,
        0.8,
        True,
        False,
    ),
    ("consumer job outlives its bound: exit without closing", None, "-", False, True),
    (
        "phase pool work outlives the wait: exit without closing",
        "-",
        None,
        False,
        False,
    ),
]


def test_store_closes_only_after_running_work_ends(root, monkeypatch):
    for name, job_s, phase_s, closed_expected, left_expected in SHUTDOWN_ROWS:
        for leftover in (root / "running").glob("*.json"):
            leftover.unlink()
        if hasattr(storage, "open_store_gate"):
            storage.open_store_gate()
        vs._shutdown_requested.clear()
        active: set[str] = set()
        closes: list[set[str]] = []
        release = {"job": threading.Event(), "phase": threading.Event()}
        started = {"job": threading.Event(), "phase": threading.Event()}

        class FakeMemory:
            def close(self):
                closes.append(set(active))

        def held(kind):
            active.add(kind)
            started[kind].set()
            release[kind].wait(10)
            active.discard(kind)

        monkeypatch.setattr(storage, "_memory", FakeMemory())
        monkeypatch.setattr(
            "smartmemory_app.lifecycle.run_phase", lambda lc, phase, body: held("job")
        )
        monkeypatch.setattr(lifecycle_api, "_get_lifecycle", lambda body: None)
        monkeypatch.setattr(vs, "_STORE_IDLE_WAIT_S", 2.0, raising=False)
        timers = []
        for kind, seconds in (("job", job_s), ("phase", phase_s)):
            if isinstance(seconds, float):
                timers.append(threading.Timer(seconds, release[kind].set))
        consumer = phase_thread = None
        if job_s != "-":
            _queue_job(root, "s1")
            consumer = hook_consumer.DaemonConsumer(root, poll=0.01).start()
            vs._hook_consumer = consumer
            assert started["job"].wait(5), name
        if phase_s != "-":
            phase_thread = threading.Thread(
                target=lambda: asyncio.run(lifecycle_api._off_loop(held, "phase")),
                daemon=True,
            )
            phase_thread.start()
            assert started["phase"].wait(5), name
        for timer in timers:
            timer.start()

        cleanup = getattr(vs, "_stop_hook_consumer_then_close_store", None)
        if cleanup is None:  # the cleanup this unit replaced, for the before/after run

            def cleanup():
                vs._stop_hook_consumer()
                storage._shutdown()

        cleanup()
        left = sorted(p.name for p in (root / "running").glob("*.json"))
        marker = root / "interrupted.json"
        marked = json.loads(marker.read_text())["jobs"] if marker.exists() else []
        for event in release.values():
            event.set()
        if consumer is not None:
            consumer.thread.join(5)
        if phase_thread is not None:
            phase_thread.join(5)

        assert all(not still for still in closes), (
            f"{name}: store closed under {closes}"
        )
        assert bool(closes) is closed_expected, f"{name}: closes={closes}"
        assert bool(left) is left_expected, f"{name}: running/={left}"
        if left_expected:
            assert marked == left, (
                f"{name}: the stop marker must name the jobs left running"
            )


# ── attempt budget: a stop or a lost write lock is not a failure ─────────────────────


def _busy():
    return storage.StoreBusyError("Store busy")


def _closing():
    return storage.StoreClosingError("shutting down")  # AttributeError before this unit


def _busy_wrapped():
    try:
        raise _busy()
    except storage.StoreBusyError as exc:
        try:
            raise RuntimeError("ingest failed") from exc
        except RuntimeError as wrapped:
            return wrapped


# name, where the job starts, job fields, error the job raises (None = recovery only),
# marker names the job, expect dir, expect attempts, expect free_retries
BUDGET_ROWS = [
    ("lost write lock is a free retry", "queue", {}, _busy, False, "queue", 0, 1),
    (
        "stop before the job touched the store",
        "queue",
        {},
        _closing,
        False,
        "queue",
        0,
        1,
    ),
    (
        "lost write lock under another error",
        "queue",
        {"attempts": 2},
        _busy_wrapped,
        False,
        "queue",
        2,
        1,
    ),
    (
        "stopped mid-job, recovered from running/",
        "running",
        {"attempts": 4},
        None,
        True,
        "queue",
        4,
        1,
    ),
    (
        "guard: crash without the marker counts",
        "running",
        {"attempts": 1},
        None,
        False,
        "queue",
        2,
        0,
    ),
    (
        "guard: ordinary failure counts",
        "queue",
        {},
        lambda: ValueError("bad"),
        False,
        "queue",
        1,
        0,
    ),
    (
        "guard: free retries spent, lost lock counts",
        "queue",
        {"attempts": 4, "free_retries": 40},
        _busy,
        False,
        "dead",
        5,
        40,
    ),
]


def test_stop_and_lost_lock_do_not_use_the_failure_budget(root):
    for (
        name,
        start,
        fields,
        error,
        marked,
        want_dir,
        want_attempts,
        want_free,
    ) in BUDGET_ROWS:
        for sub in ("queue", "running", "dead"):
            for leftover in (root / sub).glob("*.json"):
                leftover.unlink()
        (root / "interrupted.json").unlink(missing_ok=True)
        _queue_job(root, "job1", _dir=start, **fields)
        if marked:
            (root / "interrupted.json").write_text(
                json.dumps({"pid": 1, "jobs": ["job1.json"]})
            )
        stop = threading.Event()

        def execute(job):
            stop.set()
            raise error()

        if error is None:
            stop.set()  # recovery only: consume recovers running/ and returns
        hook_consumer.consume(
            root, execute=execute, stop=stop, idle_exit=None, poll=0.01
        )

        found = list((root / want_dir).glob("job1.json"))
        assert found, (
            f"{name}: job not in {want_dir}/ ({[p.parent.name for p in root.rglob('job1.json')]})"
        )
        record = json.loads(found[0].read_text())
        assert record.get("attempts", 0) == want_attempts, f"{name}: {record}"
        assert record.get("free_retries", 0) == want_free, f"{name}: {record}"
        assert not (root / "interrupted.json").exists(), f"{name}: marker left behind"


# ── F-A: sync routes hold the store; a request during the close is refused ──────────

# name, app ("local" = local_api mounted app, "health" = the daemon app), method, path,
# route ends after (s; None = held past the close's wait; "closing" = sent once the
# close has begun), expect store closed by the wait, expect status
ROUTE_ROWS = [
    ("list mid-read: close waits for it", "local", "get", "/list", 0.3, True, 503),
    (
        "item delete mid-write outlives the wait: store left open",
        "local",
        "delete",
        "/m1",
        None,
        False,
        503,
    ),
    (
        "item read outlives the wait: store left open",
        "local",
        "get",
        "/m1",
        None,
        False,
        503,
    ),
    (
        "health count mid-read: close waits for it",
        "health",
        "get",
        "/health",
        0.3,
        True,
        200,
    ),
    (
        "request during the close: refused, store untouched",
        "local",
        "get",
        "/list",
        "closing",
        True,
        503,
    ),
]


def test_sync_routes_hold_the_store_until_they_end(root, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from smartmemory_app import local_api

    for (
        name,
        app_kind,
        method,
        path,
        ends_after,
        closed_expected,
        status_expected,
    ) in ROUTE_ROWS:
        storage.open_store_gate()
        active: set[str] = set()
        touched: list[str] = []
        closes: list[set[str]] = []
        release = threading.Event()
        started = threading.Event()

        class FakeMemory:
            _graph = SimpleNamespace(backend=SimpleNamespace())

            def close(self):
                closes.append(set(active))

        def held(*args, **kwargs):
            touched.append(path)
            active.add("route")
            started.set()
            release.wait(10)
            active.discard("route")
            if app_kind == "local":
                raise HTTPException(status_code=503, detail="fake store")
            return 0

        monkeypatch.setattr(storage, "_memory", FakeMemory())
        monkeypatch.setattr(local_api, "_get_mem", held)
        if app_kind == "health":
            config = SimpleNamespace(
                mode="local", llm_provider="none", embedding_provider="local"
            )
            monkeypatch.setattr(
                "smartmemory_app.config.load_config", lambda *a, **k: config
            )
            monkeypatch.setattr(
                "smartmemory_app.config.llm_key_present", lambda *a, **k: False
            )
            monkeypatch.setattr(storage, "get_memory", lambda: FakeMemory())
            monkeypatch.setattr(vs, "_memory_count", held)
            monkeypatch.setattr(
                "smartmemory_app.work_graph.get_work_status", lambda: {}
            )
            monkeypatch.setattr(
                "smartmemory_app.work_graph.get_work_graph", lambda: None
            )
            monkeypatch.setattr(
                "smartmemory.pipeline.work_graph.reextract.reextract_offer",
                lambda *a: None,
            )
            client = TestClient(vs._build_app(), base_url="http://127.0.0.1:19008")
        else:
            client = TestClient(local_api.api, raise_server_exceptions=False)
        responses = []

        def send():
            responses.append(client.request(method, path))

        if ends_after == "closing":
            release.set()
            assert storage.close_store_when_idle(0.0), name  # nothing running: closes
            send()
            closed = bool(closes)
        else:
            request = threading.Thread(target=send, daemon=True)
            request.start()
            assert started.wait(5), name
            if isinstance(ends_after, float):
                threading.Timer(ends_after, release.set).start()
            closed = storage.close_store_when_idle(1.5 if ends_after else 0.3)
            release.set()
            request.join(5)

        assert all(not still for still in closes), (
            f"{name}: store closed under {closes}"
        )
        assert closed is closed_expected, f"{name}: closed={closed} closes={closes}"
        assert responses and responses[0].status_code == status_expected, (
            f"{name}: {[(r.status_code, r.text[:120]) for r in responses]}"
        )
        if ends_after == "closing":
            assert touched == [], f"{name}: the route reached the store: {touched}"
        if not closed_expected:
            storage._shutdown()  # the request has ended: close the fake for the next row


# ── stop marker: a consumer stuck past the bound still names its jobs ────────────────

# name, worker.pid names this process, expect the marker to name the running job
STOP_MARKER_ROWS = [
    ("consumer thread busy past the bound: stop() names its running job", True, True),
    ("worker.pid names another consumer: its jobs are not marked", False, False),
]


def test_stop_names_running_jobs_when_the_consumer_is_stuck(root, monkeypatch):
    import os

    for name, ours, marked_expected in STOP_MARKER_ROWS:
        for leftover in [*(root / "running").glob("*.json"), root / "interrupted.json"]:
            leftover.unlink(missing_ok=True)
        release = threading.Event()
        started = threading.Event()
        scanning = threading.Event()
        unblock_scan = threading.Event()
        real_adopt = capture_queue.adopt_spill

        def execute(job):
            started.set()
            release.wait(10)

        def slow_scan(r):
            if started.is_set():  # a long backlog scan, after the job started
                scanning.set()
                unblock_scan.wait(10)
            return real_adopt(r)

        monkeypatch.setattr(capture_queue, "adopt_spill", slow_scan)
        _queue_job(root, "s1")
        consumer = hook_consumer.DaemonConsumer(
            root, poll=0.01, parallel=2, execute=execute
        ).start()
        assert started.wait(5) and scanning.wait(5), name
        if not ours:
            (root / "worker.pid").write_text(f"{os.getpid() + 1}\n", encoding="utf-8")
        stopped = consumer.stop(timeout=0.2)
        marker = root / "interrupted.json"
        marked = json.loads(marker.read_text())["jobs"] if marker.exists() else []
        running = sorted(p.name for p in (root / "running").glob("*.json"))
        release.set()
        unblock_scan.set()
        consumer.thread.join(5)
        monkeypatch.setattr(capture_queue, "adopt_spill", real_adopt)

        assert stopped is False, (
            f"{name}: the consumer must still be running at the bound"
        )
        assert running == ["s1.json"], f"{name}: running/={running}"
        assert (marked == running) is marked_expected, f"{name}: marker={marked}"
