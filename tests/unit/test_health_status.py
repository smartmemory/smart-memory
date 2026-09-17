"""Health-reporting regressions for a backend that cannot initialize."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    return CliRunner()


def _health_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    from smartmemory_app.viewer_server import _build_app

    return TestClient(_build_app())


def test_health_returns_safe_degraded_reason_and_logs_warning(
    tmp_path, monkeypatch, caplog
):
    client = _health_client(tmp_path, monkeypatch)
    config = SimpleNamespace(llm_provider="none", embedding_provider="local")
    error = RuntimeError(
        "database is locked at /Users/beta/.smartmemory/store.db; api_key=secret"
    )

    with (
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", side_effect=error),
        patch("smartmemory_app.enrichment_queue.stats", return_value={}),
        caplog.at_level(logging.WARNING, logger="smartmemory_app.viewer_server"),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["degraded_reason"].startswith("RuntimeError: database is locked")
    assert "/Users/beta" not in body["degraded_reason"]
    assert "secret" not in body["degraded_reason"]
    assert "memory count is unavailable" in caplog.text
    assert body["degraded_reason"] in caplog.text


def test_health_is_ok_without_degraded_reason_when_backend_succeeds(
    tmp_path, monkeypatch, caplog
):
    client = _health_client(tmp_path, monkeypatch)
    config = SimpleNamespace(llm_provider="none", embedding_provider="local")
    memory = MagicMock()
    memory._graph.backend.serialize.return_value = {
        "nodes": [{"memory_type": "Episodic"}, {"memory_type": "Version"}]
    }

    with (
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", return_value=memory),
        patch("smartmemory_app.enrichment_queue.stats", return_value={}),
        caplog.at_level(logging.WARNING, logger="smartmemory_app.viewer_server"),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["memories"] == 1
    assert "degraded_reason" not in body
    assert "memory count is unavailable" not in caplog.text


def test_status_prints_reason_and_next_step_when_degraded(runner):
    from smartmemory_app.cli import cli

    info = {
        "status": "degraded",
        "degraded_reason": "RuntimeError: database is locked",
        "mode": "lite",
        "memories": -1,
        "llm_provider": "none",
        "llm_key_present": False,
        "embedding_provider": "local",
        "pid": 1234,
        "async_enrichment": {"enabled": False},
    }
    with patch("smartmemory_app.daemon.get_status", return_value=info):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert (
        "SmartMemory daemon: degraded\n"
        "  Problem:    SmartMemory could not open your saved memories: "
        "RuntimeError: database is locked\n"
        "  Next step:  Run: sm doctor\n"
    ) in result.output


def test_socks_incident_chain_health_to_status_to_doctor(runner, tmp_path, monkeypatch):
    """The reported backend failure reaches status, then doctor names the fix."""
    from smartmemory_app.cli import cli
    from smartmemory_app.install_check import MIN_CORE_VERSION, PROXY_ENV_VARS

    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.example:1080")

    client = _health_client(tmp_path, monkeypatch)
    config = SimpleNamespace(llm_provider="none", embedding_provider="local")
    error = RuntimeError(
        "Embedding model 'sentence-transformers/all-MiniLM-L6-v2' for backend "
        "'onnxruntime/cpu' is unavailable. Details: Using SOCKS proxy, but the "
        "'socksio' package is not installed."
    )
    with (
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", side_effect=error),
        patch("smartmemory_app.enrichment_queue.stats", return_value={}),
    ):
        health = client.get("/health").json()

    assert health["status"] == "degraded"
    assert "Using SOCKS proxy" in health["degraded_reason"]
    assert "socksio" in health["degraded_reason"]

    with patch("smartmemory_app.daemon.get_status", return_value=health):
        status_result = runner.invoke(cli, ["status"])

    assert status_result.exit_code == 0, status_result.output
    assert "Problem:" in status_result.output
    assert "Using SOCKS proxy" in status_result.output
    assert "Next step:  Run: sm doctor" in status_result.output

    def package_version(name):
        if name == "smartmemory-core":
            return MIN_CORE_VERSION
        return "1.4.101"

    with (
        patch("importlib.metadata.version", package_version),
        patch(
            "smartmemory_app.install_check._socksio_is_importable",
            return_value=False,
        ),
    ):
        doctor_result = runner.invoke(cli, ["doctor"])

    assert doctor_result.exit_code == 1
    assert "SmartMemory cannot use your network's SOCKS proxy" in doctor_result.output
    assert "pip install httpx[socks]" in doctor_result.output


def test_status_omits_reason_lines_when_healthy(runner):
    from smartmemory_app.cli import cli

    info = {
        "status": "ok",
        "mode": "lite",
        "memories": 3,
        "llm_provider": "none",
        "llm_key_present": False,
        "embedding_provider": "local",
        "pid": 1234,
        "async_enrichment": {"enabled": False},
    }
    with patch("smartmemory_app.daemon.get_status", return_value=info):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory daemon: ok" in result.output
    assert "Problem:" not in result.output
    assert "Next step:" not in result.output
