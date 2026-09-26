"""CLI -> daemon HTTP -> real lite ingest regressions; no external services."""

import socket
from unittest.mock import Mock

import httpx
import pytest
import requests
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def lite_daemon(tmp_path, monkeypatch):
    from smartmemory import (
        SmartMemory,  # noqa: F401 - load HTTP client subclasses before patching
    )
    from smartmemory.tools import factory
    from smartmemory_app import config, storage
    from smartmemory_app.local_api import api

    for name in config.LLM_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SMARTMEMORY_OBSERVABILITY", "false")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available",
        lambda: (bool(__import__("os").environ.get("GROQ_API_KEY")), "test"),
    )
    cfg = config.SmartMemoryConfig(mode="local", data_dir=str(tmp_path))
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(storage, "load_config", lambda: cfg)
    monkeypatch.setattr(storage, "is_configured", lambda: True)
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    # No model downloads or embeddings: independent of grounding/dispatch.
    monkeypatch.setattr(factory, "_require_embedding_model", lambda **kw: None)
    real_factory = factory.create_lite_memory

    def make_memory(**kwargs):
        kwargs["pipeline_profile"].store.embed = False
        kwargs["pipeline_profile"].supersede.enabled = False
        kwargs["spawn_worker"] = False
        return real_factory(**kwargs)

    monkeypatch.setattr(factory, "create_lite_memory", make_memory)
    outbound = Mock(side_effect=AssertionError("outbound HTTP forbidden"))
    monkeypatch.setattr(requests.sessions.Session, "request", outbound)
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden"))
    )
    app = FastAPI()
    app.mount("/memory", api)
    client = TestClient(app, raise_server_exceptions=False)
    real_client = httpx.Client

    class DaemonClient(real_client):
        def __enter__(self):
            return client

    monkeypatch.setattr(httpx, "Client", DaemonClient)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", outbound)
    yield client, outbound
    storage._shutdown()


@pytest.mark.parametrize("has_llm", [False, True])
def test_sm_add_never_grounds_over_http_and_uses_sqlite_dispatch(
    lite_daemon, monkeypatch, has_llm
):
    from smartmemory.observability.events import RedisStreamQueue
    from smartmemory_app import storage
    from smartmemory_app.cli import cli

    _client, outbound = lite_daemon
    if has_llm:
        monkeypatch.setenv("GROQ_API_KEY", "test-only-no-network")
    redis = Mock(side_effect=AssertionError("lite must not attempt Redis dispatch"))
    monkeypatch.setattr(RedisStreamQueue, "for_extract", redis)
    result = CliRunner().invoke(cli, ["add", "Elon Musk founded SpaceX in California."])
    if result.exception:
        raise result.exception
    assert result.exit_code == 0, result.output
    mem = storage.get_memory()
    nodes = mem._graph.backend.get_all_nodes()
    assert any(n.get("memory_type") == "entity" for n in nodes), nodes
    outbound.assert_not_called()
    redis.assert_not_called()
    import sqlite3

    with sqlite3.connect(storage._data_path / "memory.db") as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM work_nodes")]
    assert sorted(kinds) == (
        ["extract", "stage:enrich"] if has_llm else ["stage:enrich"]
    )


def test_reextract_empty_store_returns_200(lite_daemon):
    from smartmemory_app import storage

    client, outbound = lite_daemon
    storage.get_memory()
    response = client.post("/memory/reextract")
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    outbound.assert_not_called()
