"""U20.S1-HOOK-STORE-WORKSPACE-REFUSAL-1: hook write phases through the daemon route on a real Lite store.

One table over the three hook write phases (observe, learn, distill) and two outcomes:

- ``lands``: the golden row. ``POST /lifecycle/<phase>`` with a project ``cwd`` on a real SQLite Lite
  store (no mocks of the save arm) answers 200 and leaves one memory carrying the hook's origin and the
  project tag ``derive_workspace_id(cwd)``. Needs core 2.0 with ruling (a) (a Lite owner effect scope does
  not bind by row stamp, ``workspace_bind.lite_owner_scope``); before it every project-tagged row was refused.
- ``lost``: a store that fails is never ``{"status": "ok"}``: the route answers 500, names the phase and
  what was lost, and logs one WARNING naming it.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import smartmemory_app.config as config
import smartmemory_app.storage as storage
from smartmemory_app.recall_format import derive_workspace_id

# (phase, origin the stored memory carries, what the route says was lost)
PHASES = {
    "observe": ("hook:observe", "tool observation"),
    "learn": ("hook:learn", "error memory"),
    "distill": ("lifecycle:distill", "turn pair"),
}

ROWS = [
    pytest.param("observe", "lands", id="observe-lands"),
    pytest.param("learn", "lands", id="learn-lands"),
    pytest.param("distill", "lands", id="distill-lands"),
    pytest.param("observe", "lost", id="observe-lost"),
    pytest.param("learn", "lost", id="learn-lost"),
    pytest.param("distill", "lost", id="distill-lost"),
]


@pytest.fixture(scope="module")
def lite_store(tmp_path_factory):
    """One real Lite SmartMemory for the module, bound to its own data dir; shut down after."""
    root = tmp_path_factory.mktemp("test_hookws")
    data = root / "data"
    data.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "config_path", lambda: root / "config.toml")
    mp.setenv("SMARTMEMORY_MODE", "local")
    mp.setenv("SMARTMEMORY_DATA_DIR", str(data))
    mp.setenv("SMARTMEMORY_NO_WARM", "1")
    mp.setenv("SMARTMEMORY_HOOK_TRACE", str(root / "trace.jsonl"))
    mp.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    mp.setattr(storage, "_memory", None)
    mp.setattr(storage, "_data_path", None)
    try:
        storage.get_memory(data_dir=str(data))
        yield data
    finally:
        storage._shutdown()
        storage._memory = None
        storage._data_path = None
        mp.undo()


def _client(monkeypatch) -> TestClient:
    from smartmemory_app.lifecycle_api import lifecycle_router

    monkeypatch.setattr(
        "smartmemory_app.lifecycle_api._load_lifecycle_config", lambda: {}
    )
    app = FastAPI()
    app.include_router(lifecycle_router, prefix="/lifecycle")
    return TestClient(app)


def _tagged(data: Path, origin: str, tag: str) -> int:
    with sqlite3.connect(data / "memory.db") as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE json_extract(properties, '$.origin') = ? "
            "AND json_extract(properties, '$.workspace_id') = ?",
            (origin, tag),
        ).fetchone()[0]


@pytest.mark.parametrize(("phase", "outcome"), ROWS)
def test_hook_write_phase_route(
    lite_store, tmp_path, monkeypatch, caplog, phase, outcome
):
    origin, lost = PHASES[phase]
    cwd = tmp_path / f"test_hookws_{phase}_{outcome}"
    cwd.mkdir()
    tag = derive_workspace_id(str(cwd))
    body = {
        "session_id": f"test_hookws_{phase}_{outcome}",
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": f"echo test_hookws {phase}"},
        "tool_response": f"test_hookws {phase} result",
        "error": f"test_hookws {phase} failure",
        "last_assistant_message": f"test_hookws {phase} answer",
        "smartmemory_user_turn": f"test_hookws {phase} prompt",
    }
    before = _tagged(lite_store, origin, tag)
    if outcome == "lost":

        def refuse(*_args, **_kwargs):
            raise RuntimeError("test_hookws store refused")

        monkeypatch.setattr(storage, "ingest", refuse)
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.lifecycle_api"):
        response = _client(monkeypatch).post(f"/lifecycle/{phase}", json=body)
    route_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "smartmemory_app.lifecycle_api"
    ]
    if outcome == "lands":
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}
        assert _tagged(lite_store, origin, tag) == before + 1
        assert route_warnings == []
    else:
        assert response.status_code == 500
        assert response.json() == {
            "status": "error",
            "phase": phase,
            "lost": lost,
            "error": "RuntimeError: test_hookws store refused",
        }
        assert route_warnings == [
            f"Lifecycle {phase} write failed; {lost} not stored: RuntimeError: test_hookws store refused"
        ]
        assert _tagged(lite_store, origin, tag) == before


@pytest.mark.parametrize("phase", ["orient", "recall"])
def test_read_deadline_stops_before_sqlite_fetch(
    lite_store, monkeypatch, caplog, phase
):
    """A deadline spent after dispatch must reach storage, with one visible partial warning."""
    from smartmemory.search.results import PartialText
    from smartmemory_app import lifecycle_api
    from smartmemory_app.lifecycle import MemoryLifecycle

    observed = []
    original = storage.recall

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result

    def forbidden_fetch(*args, **kwargs):
        raise AssertionError("a daemon read started a store fetch after its deadline")

    # Keep the actual lifecycle and SQLite store. Advance only storage's clock to
    # represent time spent between dispatch and the first lookup, without sleeping.
    monkeypatch.setattr(storage, "recall", capture)
    monkeypatch.setattr(storage, "_recall_clock", lambda: float("inf"))
    monkeypatch.setattr(storage.get_memory(), "search", forbidden_fetch)
    monkeypatch.setattr(MemoryLifecycle, "_should_recall", lambda self, prompt: True)
    monkeypatch.setattr(
        lifecycle_api, "_load_lifecycle_config", lambda: {"rules_card_enabled": False}
    )
    app = FastAPI()
    app.include_router(lifecycle_api.lifecycle_router, prefix="/lifecycle")
    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            response = client.post(
                f"/lifecycle/{phase}",
                json={
                    "session_id": f"test_deadline_twin_{phase}",
                    "prompt": "Explain the deadline",
                },
            )
    assert response.status_code == 200
    assert response.json() == {"context": ""}
    assert len(observed) == 1
    assert isinstance(observed[0], PartialText)
    assert observed[0].partial == ("fetch_bound",)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "before the first fetch (deadline)" in warnings[0]
    assert "candidates past what core examined are not shown" in warnings[0]
