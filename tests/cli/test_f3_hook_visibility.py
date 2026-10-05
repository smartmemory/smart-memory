"""F3 direct and daemon CLI search against real SQLite, usearch and HTTP routes."""

import json
import shutil

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from smartmemory.models.memory_item import MemoryItem
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import cli, local_api, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS

TEXT = "Tool `Bash` called. Input: test_F3 auth.py. Result: saved transcript"


@pytest.fixture
def memory(tmp_path, monkeypatch):
    root = tmp_path / "test_F3_isolated"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    for key in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(key, "")
    mem = None
    try:
        mem = create_lite_memory(
            str(root / "test_F3_data"),
            pipeline_profile=PipelineConfig.lite_hermetic(llm_enabled=False),
            spawn_worker=False,
        )
        for name, origin in (
            ("observe", "hook:observe"),
            ("learn", "hook:learn"),
            ("user", "cli:add"),
        ):
            mem.add(
                MemoryItem(
                    item_id=f"test_F3_{name}",
                    content=TEXT,
                    memory_type="episodic",
                    origin=origin,
                )
            )
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        monkeypatch.setattr(local_api, "get_memory", lambda: mem)
        # Only bypass startup diagnostics, retrieval and HTTP routing remain real.
        monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kwargs: None)
        yield mem
    finally:
        if mem is not None:
            mem.close()
        shutil.rmtree(root)


@pytest.mark.parametrize("mode", ["direct", "daemon"])
@pytest.mark.parametrize("query", ["*", "test_F3 auth.py"])
def test_cli_default_and_explicit_origin(memory, monkeypatch, mode, query):
    with TestClient(local_api.api) as client:

        def daemon_request(method, path, **kwargs):
            response = client.request(method, path.removeprefix("/memory"), **kwargs)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(
            cli,
            "_daemon_request",
            daemon_request if mode == "daemon" else lambda *a, **k: None,
        )
        default = CliRunner().invoke(
            cli.cli, ["search", query, "--top-k", "10", "--json"]
        )
        assert default.exit_code == 0, default.output
        default_ids = {row["item_id"] for row in json.loads(default.stdout)}
        assert default_ids == {"test_F3_user"}
        for origin, expected in (
            ("hook:observe", "test_F3_observe"),
            ("hook:learn", "test_F3_learn"),
        ):
            result = CliRunner().invoke(
                cli.cli, ["search", query, "--origin", origin, "--json"]
            )
            assert result.exit_code == 0, result.output
            assert [row["item_id"] for row in json.loads(result.stdout)] == [expected]
        print(
            f"{mode} query={query!r} default={sorted(default_ids)} explicit=observe,learn"
        )


@pytest.mark.parametrize("query", ["*", "test_F3 auth.py"])
def test_remote_origin_refuses_before_connecting(monkeypatch, query):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")

    def forbidden_request(*args, **kwargs):
        pytest.fail("unsupported remote origin must refuse before making a request")

    monkeypatch.setattr(cli, "_memory_request", forbidden_request)
    result = CliRunner().invoke(cli.cli, ["search", query, "--origin", "hook:observe"])
    assert result.exit_code != 0
    assert "--origin is not supported in remote mode" in result.output

    # A remote-configured daemon must refuse the same filter rather than forward it.
    from smartmemory_app import remote_backend

    monkeypatch.setattr(remote_backend, "get_api_key", lambda: "")
    remote = remote_backend.RemoteMemory(api_url="http://127.0.0.1:1")
    monkeypatch.setattr(remote, "_request", forbidden_request)
    monkeypatch.setattr(storage, "get_memory", lambda: remote)
    with TestClient(local_api.api) as client:
        response = client.post(
            "/search", json={"query": query, "origin": "hook:observe"}
        )
        assert response.status_code == 501, response.text
        assert "--origin is not supported in remote mode" in response.json()["detail"]


def test_origin_help():
    result = CliRunner().invoke(cli.cli, ["search", "--help"])
    assert result.exit_code == 0
    assert (
        "show only memories whose origin starts with PREFIX (e.g. hook:observe)"
        in " ".join(result.output.split())
    )
