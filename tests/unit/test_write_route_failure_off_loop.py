"""HOOK-WRITE-QUEUE-1 land on 2.0: HOOK-STORE's 500-on-lost-write meets DAEMON-HEALTH-STARVE-1's pool.

One table over the three hook write routes. Through the real FastAPI route, a failing store must
(a) still answer HTTP 500 with ``phase`` and ``lost`` in the JSON (never ``{"status": "ok"}``), and
(b) have run the write on a ``_phase_pool`` thread, not on the event-loop thread that took the request.
"""

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import smartmemory_app.lifecycle_api as api

# (route phase, what the 500 body says was lost)
ROWS = [
    pytest.param("observe", "tool observation", id="observe"),
    pytest.param("distill", "turn pair", id="distill"),
    pytest.param("learn", "error memory", id="learn"),
]


@pytest.mark.parametrize(("phase", "lost"), ROWS)
def test_failed_write_is_500_and_runs_on_the_phase_pool(monkeypatch, phase, lost):
    loop_threads = []
    write_threads = []

    def refuse(_lc, _phase, _body):
        write_threads.append(threading.current_thread())
        raise RuntimeError("test_hwq store refused")

    monkeypatch.setattr(api, "_load_lifecycle_config", lambda: {})
    monkeypatch.setattr(api, "run_phase", refuse)
    app = FastAPI()

    @app.middleware("http")
    async def note_loop_thread(request, call_next):
        loop_threads.append(threading.current_thread())
        return await call_next(request)

    app.include_router(api.lifecycle_router, prefix="/lifecycle")
    response = TestClient(app).post(
        f"/lifecycle/{phase}", json={"session_id": "test_hwq", "cwd": "/tmp"}
    )

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "error"
    assert body["phase"] == phase
    assert body["lost"] == lost
    assert len(write_threads) == 1 and len(loop_threads) == 1
    assert write_threads[0].name.startswith("smartmemory-lifecycle")
    assert write_threads[0] is not loop_threads[0]
