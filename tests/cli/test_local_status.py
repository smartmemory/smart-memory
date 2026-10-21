"""Local disclosure through the CLI with a real, disposable SQLite graph."""

import json
import logging
import shutil
import sqlite3
from contextlib import closing

import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, daemon
from smartmemory_app.local_status import local_memory_count


@pytest.fixture
def local_status(monkeypatch, tmp_path):
    data = tmp_path / "test_a1_local_status"
    data.mkdir()
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "should_be_running", lambda: False)
    try:
        yield data
    finally:
        shutil.rmtree(data)


@pytest.fixture
def saved_memories(local_status):
    from smartmemory.graph.backends.sqlite import SQLiteBackend

    backend = SQLiteBackend(str(local_status / "memory.db"))
    try:
        for kind in ("semantic", "episodic", "Version"):
            backend.add_node(
                f"test_a1_status_{kind}",
                {"content": f"test_a1 saved {kind}"},
                memory_type=kind,
            )
        yield backend
    finally:
        backend.close()


def invoke(*args):
    return CliRunner().invoke(cli_module.cli, ["status", *args])


@pytest.mark.parametrize(
    "state", ["stopped", "not_responding", "ok", "warming", "degraded"]
)
@pytest.mark.parametrize("as_json", [False, True])
def test_local_status_discloses_persisted_count(
    saved_memories, monkeypatch, state, as_json
):
    health = (
        None
        if state in {"stopped", "not_responding"}
        else {
            "service": "smartmemory",
            "status": state,
            "memories": 2 if state == "ok" else -1,
            "mode": "lite",
            "llm_provider": "none",
            "llm_key_present": False,
            "embedding_provider": "local",
            "pid": 123,
            "capabilities": {"local": True},
            "future_field": {"preserve": "unchanged"},
        }
    )
    monkeypatch.setattr(daemon, "get_status", lambda: health)
    monkeypatch.setattr(daemon, "should_be_running", lambda: state == "not_responding")
    result = invoke(*(["--json"] if as_json else []))
    assert result.exit_code == 0, result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["local_only"] is True
        assert payload["local_memory_count"] == 2
        if health is not None:
            assert {key: payload[key] for key in health} == health
            assert list(payload)[: len(health)] == list(health)
        else:
            assert payload == {
                "service": "smartmemory",
                "status": state,
                "memories": 2,
                "mode": "lite",
                "local_only": True,
                "local_memory_count": 2,
            }
    else:
        assert "lite (local-only)" in result.output
        assert "not in your cloud account" in result.output
        assert "Memories:   2" in result.output
        assert result.output.count("not in your cloud account") == 1
        assert "sm push" not in result.output and "sm sync" not in result.output
        if state == "stopped":
            assert "daemon is not running" in result.output
        elif state == "not_responding":
            assert "should be running, but it is not responding" in result.output
        elif state == "warming":
            assert "Models are still loading" in result.output
        elif state == "degraded":
            assert "Run: sm doctor" in result.output
        else:
            assert "SmartMemory daemon: ok" in result.output
    assert len(saved_memories.serialize()["nodes"]) == 3


def test_local_status_count_updates_with_real_store_mutations(saved_memories):
    assert json.loads(invoke("--json").stdout)["local_memory_count"] == 2
    saved_memories.add_node(
        "test_a1_status_new", {"content": "new"}, memory_type="semantic"
    )
    assert json.loads(invoke("--json").stdout)["local_memory_count"] == 3
    saved_memories.remove_node("test_a1_status_new")
    assert json.loads(invoke("--json").stdout)["local_memory_count"] == 2


@pytest.mark.parametrize("store", ["missing", "empty", "work_only"])
@pytest.mark.parametrize("as_json", [False, True])
def test_empty_local_status_is_explained(local_status, store, as_json):
    if store == "empty":
        from smartmemory.graph.backends.sqlite import SQLiteBackend

        with closing(SQLiteBackend(str(local_status / "memory.db"))):
            pass
    elif store == "work_only":
        from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph

        SQLiteWorkGraph(str(local_status / "memory.db"))
    result = invoke(*(["--json"] if as_json else []))
    assert result.exit_code == 0, result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["local_only"] is True
        assert payload["local_memory_count"] == 0
    else:
        assert "Memories:   0" in result.output
        assert "not in your cloud account" in result.output


def test_local_status_count_does_not_create_store(local_status):
    assert local_memory_count() == 0
    assert not (local_status / "memory.db").exists()
    assert json.loads(invoke("--json").stdout)["local_memory_count"] == 0
    assert not (local_status / "memory.db").exists()


@pytest.mark.parametrize("as_json", [False, True])
def test_local_status_unreadable_count_is_not_silent_zero(
    local_status, caplog, as_json
):
    (local_status / "memory.db").write_bytes(b"test_a1_invalid_sqlite")
    with caplog.at_level(logging.WARNING):
        result = invoke(*(["--json"] if as_json else []))
    assert result.exit_code == 0, result.output
    assert "Local memory count unavailable" in caplog.text
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["local_only"] is True
        assert payload["local_memory_count"] is None
        assert payload["memories"] is None
    else:
        assert "Memories:   unavailable" in result.output
        assert "Memories:   0" not in result.output
        assert "not in your cloud account" in result.output


def test_local_status_incompatible_schema_is_not_empty(local_status, caplog):
    with closing(sqlite3.connect(local_status / "memory.db")) as db:
        db.execute("CREATE TABLE nodes (item_id TEXT)")
    result = invoke("--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["local_memory_count"] is None
    assert "Local memory count unavailable" in caplog.text
