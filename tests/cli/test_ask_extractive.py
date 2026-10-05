"""F2 keyless ask through real Lite storage, HTTP and CLI rendering."""

import logging

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import cli as cli_module
from smartmemory_app import local_api, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS


@pytest.fixture()
def local_ask(tmp_path, monkeypatch):
    from smartmemory.tools.factory import create_lite_memory

    for key in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    memory = create_lite_memory(str(tmp_path / "test_F2_store"), spawn_worker=False)
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    monkeypatch.setattr(local_api, "get_memory", lambda: memory)

    def forbidden_llm(**kwargs):
        pytest.fail("no-key ask must never call an LLM")

    monkeypatch.setattr("smartmemory.utils.llm.call_llm", forbidden_llm)
    with TestClient(local_api.api) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path.removeprefix("/memory"), **kwargs)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(cli_module, "_memory_request", request)
        try:
            yield memory, client
        finally:
            memory.close()


def test_no_key_route_and_cli_return_real_evidence(local_ask, caplog):
    from smartmemory.models.memory_item import MemoryItem

    memory, client = local_ask
    item = MemoryItem(
        item_id="test_F2_23456789-abcd-4567-8901-234567890123",
        content="F2 MochiOS boot timing ≈ 4.19x faster with the boot cache.",
        memory_type="semantic",
    )
    memory.add(item)
    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/ask", json={"question": "MochiOS boot timing", "limit": 1}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "extractive"
    assert body["synthesized"] is False
    assert body["evidence"][0]["item_id"] == item.item_id
    assert body["evidence"][0]["content"] == item.content
    assert body["evidence"][0]["memory_type"] == "semantic"
    assert body["evidence"][0]["created_at"]
    assert "No LLM configured" in caplog.text
    for flags in ([], ["--reasoning"]):
        result = CliRunner().invoke(
            cli_module.cli, ["ask", "MochiOS boot timing", "--limit", "1", *flags]
        )
        assert result.exit_code == 0, result.output
        assert "Extractive" in result.output
        assert "not a written answer" in result.output
        assert f"1. [{item.memory_type}] {item.item_id}" in result.output
        assert body["evidence"][0]["created_at"] in result.output
        assert "≈ 4.19x" in result.output
        assert "smartmemory setup" in result.output
        assert "add --reasoning" not in result.output


def test_no_key_empty_store_exits_zero(local_ask):
    _, client = local_ask
    response = client.post("/ask", json={"question": "MochiOS boot timing"})
    assert response.status_code == 200, response.text
    assert response.json()["evidence"] == []
    result = CliRunner().invoke(cli_module.cli, ["ask", "MochiOS boot timing"])
    assert result.exit_code == 0, result.output
    assert "Nothing relevant found." in result.output
    assert "smartmemory setup" in result.output


@pytest.mark.parametrize(
    "body",
    [{"question": " "}, {"question": "q", "limit": 0}, {"question": "q", "limit": 51}],
)
def test_no_key_invalid_request_is_a_clear_error(local_ask, body):
    _, client = local_ask
    response = client.post("/ask", json=body)
    assert response.status_code == 400, response.text
