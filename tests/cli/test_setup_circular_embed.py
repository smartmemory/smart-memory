"""CLI-SETUP-CIRCULAR-EMBED-1: a failed model load keeps its cause, and setup never tells a user in setup to run setup."""

import logging
import sys

import click
import pytest
from smartmemory.plugins.embedding import EmbeddingService
from smartmemory.tools import factory
from smartmemory_app import hook_failures
from smartmemory_app.setup import _ensure_embedding_model
from smartmemory_app.warm import warm_models

CAUSE = "Unable to load weights from pytorch checkpoint file"


@pytest.fixture
def broken_local_embedder(monkeypatch):
    """Files fetch fine (the tester's torch-installed-after-ONNX setup), then the loader fails."""
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(factory, "_require_embedding_model", lambda **kwargs: None)

    def install(error, backend):
        def broken(self, text):
            raise error

        monkeypatch.setattr(EmbeddingService, "_embed_local", broken)
        monkeypatch.setattr(EmbeddingService, "_resolve_backend", lambda self: backend)

    return install


# One row per loader failure x runtime the failure can come from; the hint depends on the runtime.
SETUP_ROWS = [
    (
        "torch_os_error",
        OSError(CAUSE),
        "torch/cpu",
        "SMARTMEMORY_EMBEDDING_BACKEND=onnx",
    ),
    (
        "torch_cuda_runtime_error",
        RuntimeError(CAUSE),
        "torch/cuda:0",
        "SMARTMEMORY_EMBEDDING_BACKEND=onnx",
    ),
    ("onnx_value_error", ValueError(CAUSE), "onnxruntime/cpu", "sm doctor"),
    ("onnx_import_error", ImportError(CAUSE), "onnxruntime/cpu", "sm doctor"),
]


@pytest.mark.parametrize(
    "error,backend,hint",
    [row[1:] for row in SETUP_ROWS],
    ids=[row[0] for row in SETUP_ROWS],
)
def test_setup_failure_names_the_cause_and_is_not_circular(
    broken_local_embedder, error, backend, hint
):
    broken_local_embedder(error, backend)
    with pytest.raises(click.ClickException) as raised:
        _ensure_embedding_model("local")
    message = raised.value.format_message()
    assert message.startswith("Embedding model could not be loaded: ")
    assert CAUSE in message
    assert hint in message
    assert "run smartmemory setup" not in message.lower()


def test_sm_warm_keeps_the_cause_and_its_repair_hint(broken_local_embedder):
    broken_local_embedder(OSError(CAUSE), "onnxruntime/cpu")
    with pytest.raises(RuntimeError) as raised:
        warm_models(reranker=False, strict=True)
    assert CAUSE in str(raised.value)
    assert "Run smartmemory setup" in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)


def test_background_warm_failure_logs_the_cause_at_warning(
    broken_local_embedder, caplog
):
    broken_local_embedder(OSError(CAUSE), "onnxruntime/cpu")
    with caplog.at_level(logging.DEBUG):
        warm_models(reranker=False)
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Embedder warm failed" in r.getMessage()
    ]
    assert warnings and CAUSE in warnings[0]


def test_warm_without_a_reported_cause_is_still_not_circular_in_setup(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(EmbeddingService, "warm", lambda self, **kwargs: False)
    with pytest.raises(RuntimeError) as raised:
        warm_models(reranker=False, strict=True, repair_hint=None)
    assert "Run smartmemory setup" not in str(raised.value)
    assert str(raised.value).startswith("Embedding model could not be loaded")


# Git Bash exists on Windows only; every other platform gets PATH advice without it.
HINT_ROWS = [
    ("windows", "win32", True),
    ("macos", "darwin", False),
    ("linux", "linux", False),
]


@pytest.mark.parametrize(
    "platform,names_git_bash",
    [row[1:] for row in HINT_ROWS],
    ids=[row[0] for row in HINT_ROWS],
)
def test_hook_hint_names_git_bash_only_on_windows(platform, names_git_bash):
    assert ("Git Bash" in hook_failures.hint(platform)) is names_git_bash
    assert "smartmemory setup" in hook_failures.hint(platform)


@pytest.mark.parametrize(
    "platform,names_git_bash",
    [row[1:] for row in HINT_ROWS],
    ids=[row[0] for row in HINT_ROWS],
)
def test_doctor_hook_row_uses_the_platform_hint(
    monkeypatch, tmp_path, platform, names_git_bash
):
    """The tester's Mac: `persist exit 1` printed the Windows-only Git Bash PATH advice."""
    monkeypatch.setattr(sys, "platform", platform)
    (tmp_path / "hook-failures.tsv").write_text("persist\t1\t1\n")
    rows = hook_failures.recent_failures(tmp_path)
    assert any("persist exit 1" in row for row in rows)
    assert any(("Git Bash" in row) is names_git_bash for row in rows)
