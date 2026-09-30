"""Exercise setup and runtime through core's real model prerequisite path."""

import logging
import os
from unittest.mock import Mock, patch

import click
import huggingface_hub
import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory.errors import MissingModelError
from smartmemory.plugins.embedding import DEFAULT_LOCAL_MODEL
from smartmemory_app import setup, storage
from smartmemory_app.config import SmartMemoryConfig, load_config, save_config


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.delenv("SMARTMEMORY_HF_ALLOW_DOWNLOAD", raising=False)
    cache = tmp_path / "hf"
    cache.mkdir()
    monkeypatch.setenv("HF_HOME", str(cache))
    # Hub constants may have been imported before this fixture's environment.
    monkeypatch.setattr("huggingface_hub.constants.HF_HOME", str(cache))
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(cache / "hub"))
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    save_config(SmartMemoryConfig(mode="local", data_dir=str(tmp_path / "data")))
    return tmp_path


@pytest.mark.parametrize("progress", [False, True])
def test_runtime_empty_cache_never_downloads(local_config, progress):
    network = Mock(side_effect=AssertionError("runtime must not download"))
    with (
        patch("spacy.util.is_package", return_value=True),
        patch("huggingface_hub.hf_hub_download", network),
        patch("huggingface_hub._snapshot_download.hf_hub_download", network),
        patch("huggingface_hub.list_repo_files", network),
        patch("socket.socket.connect", network),
        patch(
            "huggingface_hub.snapshot_download",
            wraps=huggingface_hub.snapshot_download,
        ) as snapshot,
    ):
        with pytest.raises(MissingModelError, match="Run sm setup"):
            storage.get_memory(on_progress=(lambda _: None) if progress else None)
    network.assert_not_called()
    snapshot.assert_called_once()
    assert snapshot.call_args.kwargs["local_files_only"] is True
    assert storage._memory is None


def test_setup_local_uses_real_core_prerequisite(local_config):
    fetched = local_config / "snapshot"
    (fetched / "onnx").mkdir(parents=True)
    (fetched / "onnx/model.onnx").touch()
    (fetched / "tokenizer.json").write_text("{}")

    def resolve(model_id, **kwargs):
        assert model_id == DEFAULT_LOCAL_MODEL
        if kwargs.get("local_files_only"):
            raise OSError("empty cache")
        return str(fetched)

    with (
        patch("huggingface_hub.snapshot_download", side_effect=resolve) as snapshot,
        patch("smartmemory.utils.hf_models.load_sentence_transformer") as construct,
    ):
        setup._ensure_embedding_model("local")
    assert snapshot.call_count == 2
    assert snapshot.call_args_list[0].kwargs["local_files_only"] is True
    assert snapshot.call_args_list[1].kwargs["local_files_only"] is False
    construct.assert_not_called()


@pytest.mark.parametrize("previous", [None, "false", "true"])
def test_setup_pinned_embedder_fetches_transitive_code(
    local_config, monkeypatch, previous
):
    from smartmemory.utils import hf_models

    model_id = "nomic-ai/nomic-embed-text-v1.5"
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_LOCAL_MODEL", model_id)
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "torch")
    if previous is not None:
        monkeypatch.setenv(hf_models.ALLOW_DOWNLOAD_ENV, previous)
    fetched = local_config / "snapshot"
    fetched.mkdir()
    (fetched / "model.safetensors").touch()
    (fetched / "config.json").write_text("{}")
    events = []

    def resolve(model_id, **kwargs):
        events.append("snapshot")
        return str(fetched)

    def construct(path, **kwargs):
        events.append("construct")
        assert path == str(fetched)
        assert kwargs["trust_remote_code"] is True
        assert hf_models.downloads_allowed() is True

    with (
        patch("huggingface_hub.snapshot_download", side_effect=resolve),
        patch(
            "sentence_transformers.SentenceTransformer", side_effect=construct
        ) as loader,
    ):
        setup._ensure_embedding_model("local")
    loader.assert_called_once()
    assert events == ["snapshot", "snapshot", "construct"]
    assert os.environ.get(hf_models.ALLOW_DOWNLOAD_ENV) == previous


@pytest.mark.parametrize("model", ["en_core_web_md", "en_core_web_lg"])
@pytest.mark.parametrize("progress", [False, True])
def test_configured_spacy_required_at_startup(local_config, model, progress):
    save_config(SmartMemoryConfig(mode="local", spacy_model=model))
    assert load_config().spacy_model == model
    with (
        patch("spacy.util.is_package", return_value=False) as installed,
        patch("smartmemory.tools.factory._require_embedding_model"),
    ):
        with pytest.raises(MissingModelError, match=model):
            storage.get_memory(on_progress=(lambda _: None) if progress else None)
    installed.assert_called_once_with(model)


@pytest.mark.parametrize("model", ["en_core_web_md", "en_core_web_lg"])
def test_setup_installs_persisted_spacy_choice(local_config, model):
    with (
        patch("smartmemory_app.setup._ensure_spacy") as install,
        patch("smartmemory_app.setup._ensure_embedding_model"),
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
    ):
        setup._apply_setup_result(setup.SetupResult(spacy_model=model))
    install.assert_called_once_with(model)
    assert load_config().spacy_model == model


def test_viewer_and_cli_surface_missing_model(local_config, caplog):
    from smartmemory_app.cli import _daemon_request
    from smartmemory_app.local_api import api

    network = Mock(side_effect=AssertionError("runtime must not download"))
    with (
        patch("spacy.util.is_package", return_value=True),
        patch("huggingface_hub._snapshot_download.hf_hub_download", network),
        patch("huggingface_hub.list_repo_files", network),
        patch("socket.socket.connect", network),
        caplog.at_level(logging.WARNING),
    ):
        response = TestClient(api, raise_server_exceptions=False).get("/graph/full")
    assert response.status_code == 503
    message = response.json()["detail"]
    assert "Run sm setup" in message
    assert DEFAULT_LOCAL_MODEL in message
    assert any(
        r.levelno == logging.WARNING and message in r.message for r in caplog.records
    )
    network.assert_not_called()

    @click.command()
    def command():
        _daemon_request("GET", "/memory/graph/full")

    with patch("httpx.Client") as client:
        # Starlette uses httpx2 here; the CLI uses httpx. Preserve the HTTP payload
        # in the CLI's response type so its normal HTTPStatusError handler runs.
        client.return_value.__enter__.return_value.request.return_value = (
            httpx.Response(
                response.status_code,
                json=response.json(),
                request=httpx.Request("GET", "http://localhost/memory/graph/full"),
            )
        )
        result = CliRunner().invoke(command)
    assert result.exit_code == 1
    assert message in result.output
