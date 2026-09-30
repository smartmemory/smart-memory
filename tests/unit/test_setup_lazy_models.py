"""Setup prefetches default-on lazy consumers without constructing models."""

import logging
from unittest.mock import Mock

import pytest

from smartmemory.utils import hf_models
from smartmemory_app import setup


@pytest.mark.parametrize("provider", ["local", "openai", "ollama"])
@pytest.mark.parametrize("backend", ["onnx", "torch"])
def test_prefetch_torch_consumers(monkeypatch, provider, backend):
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", provider)
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", backend)
    download = Mock()
    monkeypatch.setattr(hf_models, "resolve_local_path", download)
    setup._ensure_lazy_models()
    assert [call.args[0] for call in download.call_args_list] == [
        "sentence-transformers/all-MiniLM-L6-v2",
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
    ]
    assert all(
        call.kwargs == {"allow_download": True, "backend": "torch"}
        for call in download.call_args_list
    )


@pytest.mark.parametrize("failed_index", [0, 1])
def test_prefetch_failure_warns_and_continues(monkeypatch, caplog, failed_index):
    results = ["cached", "cached"]
    results[failed_index] = hf_models.HFModelUnavailable("download failed")
    download = Mock(side_effect=results)
    monkeypatch.setattr(hf_models, "resolve_local_path", download)
    with caplog.at_level(logging.WARNING):
        setup._ensure_lazy_models()
    assert download.call_count == 2
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert download.call_args_list[failed_index].args[0] in caplog.text
    assert "download failed" in caplog.text
