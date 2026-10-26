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
import time
from pathlib import Path

import pytest

from smartmemory_app import capture_queue, hook_consumer, lifecycle_api, storage
from smartmemory_app import viewer_server as vs


def _queue_job(root: Path, name: str, **fields) -> Path:
    subdir = fields.pop("_dir", "queue")
    payload = root / "payloads" / f"{name}.payload.json"
    payload.write_text(json.dumps({"session_id": name}), encoding="utf-8")
    record = {"phase": "observe", "session_id": name, "payload_path": str(payload), **fields}
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
    ("consumer and phase end inside the bounds: close after both", 0.3, 0.8, True, False),
    ("consumer job outlives its bound: exit without closing", None, "-", False, True),
    ("phase pool work outlives the wait: exit without closing", "-", None, False, False),
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
        monkeypatch.setattr("smartmemory_app.lifecycle.run_phase", lambda lc, phase, body: held("job"))
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
                target=lambda: asyncio.run(lifecycle_api._off_loop(held, "phase")), daemon=True
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

        assert all(not still for still in closes), f"{name}: store closed under {closes}"
        assert bool(closes) is closed_expected, f"{name}: closes={closes}"
        assert bool(left) is left_expected, f"{name}: running/={left}"
        if left_expected:
            assert marked == left, f"{name}: the stop marker must name the jobs left running"


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
    ("stop before the job touched the store", "queue", {}, _closing, False, "queue", 0, 1),
    ("lost write lock under another error", "queue", {"attempts": 2}, _busy_wrapped, False, "queue", 2, 1),
    ("stopped mid-job, recovered from running/", "running", {"attempts": 4}, None, True, "queue", 4, 1),
    ("guard: crash without the marker counts", "running", {"attempts": 1}, None, False, "queue", 2, 0),
    ("guard: ordinary failure counts", "queue", {}, lambda: ValueError("bad"), False, "queue", 1, 0),
    ("guard: free retries spent, lost lock counts", "queue", {"attempts": 4, "free_retries": 40}, _busy, False, "dead", 5, 40),
]


def test_stop_and_lost_lock_do_not_use_the_failure_budget(root):
    for name, start, fields, error, marked, want_dir, want_attempts, want_free in BUDGET_ROWS:
        for sub in ("queue", "running", "dead"):
            for leftover in (root / sub).glob("*.json"):
                leftover.unlink()
        (root / "interrupted.json").unlink(missing_ok=True)
        _queue_job(root, "job1", _dir=start, **fields)
        if marked:
            (root / "interrupted.json").write_text(json.dumps({"pid": 1, "jobs": ["job1.json"]}))
        stop = threading.Event()

        def execute(job):
            stop.set()
            raise error()

        if error is None:
            stop.set()  # recovery only: consume recovers running/ and returns
        hook_consumer.consume(root, execute=execute, stop=stop, idle_exit=None, poll=0.01)

        found = list((root / want_dir).glob("job1.json"))
        assert found, f"{name}: job not in {want_dir}/ ({[p.parent.name for p in root.rglob('job1.json')]})"
        record = json.loads(found[0].read_text())
        assert record.get("attempts", 0) == want_attempts, f"{name}: {record}"
        assert record.get("free_retries", 0) == want_free, f"{name}: {record}"
        assert not (root / "interrupted.json").exists(), f"{name}: marker left behind"
