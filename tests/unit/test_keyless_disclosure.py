"""Caller disclosure follows core's effective route, not an API-key list."""

import logging
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("sync", [False, True])
def test_storage_discloses_effective_local_route(
    tmp_path, monkeypatch, caplog, available, sync
):
    from smartmemory_app import storage

    core_result = {
        "item_id": "item-id",
        "queued": True,
        "run_id": "run-id",
        "entity_ids": {"Alice": "entity-id"},
    }
    memory = MagicMock()
    memory.ingest.return_value = core_result
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    # A key alone cannot prove a usable route; subscription routes need no key.
    monkeypatch.setenv("OPENAI_API_KEY", "" if available else "unusable-key")
    route = Mock(return_value=(available, "effective core route"))
    monkeypatch.setattr("smartmemory.utils.llm.llm_route_available", route)

    with caplog.at_level(logging.WARNING, logger=storage.__name__):
        result = storage.ingest("Alice leads Atlas", sync=sync)

    memory.ingest.assert_called_once_with(
        "Alice leads Atlas", context={"memory_type": "episodic"}, sync=sync
    )
    if sync:
        assert result == "item-id"  # synchronous MCP/hook return contract
        route.assert_not_called()
    elif available:
        route.assert_called_once_with()
        assert result == core_result
        assert "warning" not in result
    else:
        route.assert_called_once_with()
        assert {k: v for k, v in result.items() if k != "warning"} == core_result
        assert "LLM entity/relation extraction unavailable" in result["warning"]
        assert "ruler extraction and local enrichers still run" in result["warning"]
        assert result["warning"] in caplog.text
    assert "warning" not in core_result  # do not mutate core's result


@pytest.mark.parametrize("available", [False, True])
def test_cli_keeps_uuid_on_stdout_and_disclosure_on_stderr(monkeypatch, available):
    from click.testing import CliRunner
    from smartmemory_app.cli import cli

    item_id = "ba931e6b-83ac-4b4b-97e4-8bcdd03cfb30"
    response = {"item_id": item_id}
    warning = (
        "LLM entity/relation extraction unavailable (no usable provider configured); "
        "ruler extraction and local enrichers still run."
    )
    if not available:
        response["warning"] = warning
    monkeypatch.setattr("smartmemory_app.cli._daemon_request", lambda *a, **k: response)
    result = CliRunner().invoke(cli, ["add", "Alice leads Atlas"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == item_id
    assert result.stderr == ("" if available else f"⚠  {warning}\n")


@pytest.mark.parametrize("available", [False, True])
def test_boot_banner_uses_effective_route(monkeypatch, capsys, available):
    from smartmemory_app import storage, viewer_server

    configured = False

    def configure():
        nonlocal configured
        configured = True

    def route():
        assert configured, "apply local provider/model settings before resolving"
        return available, "effective core route"

    monkeypatch.setattr(storage, "apply_runtime_config", configure)
    monkeypatch.setattr("smartmemory.utils.llm.llm_route_available", route)
    viewer_server._print_llm_extraction_status()

    output = capsys.readouterr().out
    if available:
        assert "LLM extraction: enabled" in output
        assert "unavailable" not in output
    else:
        assert "LLM entity/relation extraction unavailable" in output
        assert "ruler extraction and local enrichers still run" in output
        assert "smartmemory setup" in output
    assert "entity extraction and enrichment are off" not in output


@pytest.mark.parametrize(
    "provider,model,credentials,agent_sdk,available",
    [
        ("none", "gpt-4o-mini", {}, False, False),
        ("openai", "gpt-4o-mini", {"OPENAI_API_KEY": "test-key"}, False, True),
        ("gemini", "", {"OPENAI_API_KEY": "wrong-provider-key"}, False, False),
        ("ollama", "llama3", {}, False, True),
        ("none", "sonnet", {}, True, True),
    ],
    ids=["keyless", "openai", "wrong-provider-key", "local-endpoint", "subscription"],
)
def test_effective_config_disclosure_through_storage_api_and_cli(
    tmp_path, monkeypatch, provider, model, credentials, agent_sdk, available
):
    """Resolve real provider prerequisites and carry the result to the add caller."""
    from click.testing import CliRunner
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from smartmemory.pipeline.config import LITE_LOCAL_ENRICHERS, PipelineConfig
    from smartmemory.pipeline.state import PipelineState
    from smartmemory.pipeline.work_graph.lite_save import save_to_work_graph
    from smartmemory.scope_provider import DefaultScopeProvider
    from smartmemory.utils import llm
    from smartmemory_app import config, storage
    from smartmemory_app.cli import cli
    from smartmemory_app.local_api import api

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[smartmemory]\nmode = "local"\n[local]\n'
        f'llm_provider = "{provider}"\nllm_model = "{model}"\n'
    )
    monkeypatch.setattr(config, "config_path", lambda: cfg_path)
    # Isolate machine-specific core credentials and subscription installation.
    # Keep llm_route_available and its route/model resolution real.
    monkeypatch.setattr(llm, "get_config", lambda _section: {})
    monkeypatch.setattr(llm, "_claude_agent_sdk_available", lambda: agent_sdk)
    item_id = "ba931e6b-83ac-4b4b-97e4-8bcdd03cfb30"
    work_graph = Mock()
    work_graph.add_run.return_value = "run-id"
    runner = Mock()
    runner.run_to.return_value = PipelineState(item_id=item_id)

    def core_ingest(content, *, context, sync):
        # Exercise core's actual route resolution and deferred-work planning.
        # Only persistence and foreground stage execution are stubbed.
        _, result = save_to_work_graph(
            runner,
            text=content,
            config=PipelineConfig.lite(),
            metadata=context,
            work_graph=work_graph,
            data_dir=None,
            scope_provider=DefaultScopeProvider(single_tenant=True),
            enable_ontology=False,
        )
        return result

    memory = MagicMock()
    memory.ingest.side_effect = core_ingest
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    responses = []
    app = FastAPI()
    app.mount("/memory", api)

    def request(method, path, **kwargs):
        assert method == "POST" and path == "/memory/ingest"
        response = TestClient(app).post(path, **kwargs)
        assert response.status_code == 200, response.text
        responses.append(response.json())
        return response.json()

    monkeypatch.setattr("smartmemory_app.cli._daemon_request", request)
    with patch.dict("os.environ", {"BROWSER": "true", **credentials}, clear=True):
        storage.apply_runtime_config()
        result = CliRunner().invoke(cli, ["add", "Alice leads Atlas"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == item_id
    assert memory.ingest.call_args.kwargs["sync"] is False
    boxes = work_graph.add_run.call_args.kwargs["boxes"]
    assert ("extract" in [box.kind for box in boxes]) is available
    enrich = next(box for box in boxes if box.kind == "stage:enrich")
    enricher_names = enrich.input["config"]["enrich"]["enricher_names"]
    assert enricher_names == (None if available else list(LITE_LOCAL_ENRICHERS))
    if available:
        assert responses == [{"item_id": item_id}]
        assert result.stderr == ""
    else:
        warning = responses[0]["warning"]
        assert "LLM entity/relation extraction unavailable" in warning
        assert "ruler extraction and local enrichers still run" in warning
        assert result.stderr == f"⚠  {warning}\n"


@pytest.mark.parametrize("available", [False, True])
def test_stdin_batch_prints_disclosure_once(monkeypatch, available):
    from click.testing import CliRunner
    from smartmemory_app.cli import cli

    response = {"item_id": "item-id"}
    warning = "LLM entity/relation extraction unavailable; local enrichers still run."
    if not available:
        response["warning"] = warning
    monkeypatch.setattr("smartmemory_app.cli._daemon_request", lambda *a, **k: response)

    result = CliRunner().invoke(cli, ["add", "-"], input="first\nsecond\n")

    assert result.exit_code == 0, result.output
    assert result.stdout == "Added 2 memories\nitem-id\nitem-id\n"
    assert result.stderr == ("" if available else f"⚠  {warning}\n")
