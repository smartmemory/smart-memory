"""LITE-FIRSTRUN-SPACY-1: a fresh install's first direct `sm add` must not crash.

`pip install smartmemory` cannot bring spaCy's `en_core_web_sm` (it is not on PyPI)
or the default embedding snapshot. The interactive CLI fetches both once through the
downloaders `sm setup` uses, then continues. Without a terminal, when opted out, when
the download fails, or for a non-default (remote-code) embedder, the user gets one
line naming `smartmemory setup` and a WARNING, never a traceback. Runtime startup
(storage, daemon, worker) stays disk-only.
"""

import logging
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from smartmemory.errors import MissingModelError

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh HOME, config, data dir and HF cache; no daemon; process-level caches reset."""
    import huggingface_hub.constants

    import smartmemory_app.cli as cli_module
    import smartmemory_app.storage as storage

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    for name in (
        "SMARTMEMORY_MODE",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "SMARTMEMORY_EMBEDDING_LOCAL_MODEL",
        "SMARTMEMORY_HF_ALLOW_DOWNLOAD",
        "SMARTMEMORY_AUTO_DOWNLOAD_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)
    cache = tmp_path / "hf"
    cache.mkdir()
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setattr(huggingface_hub.constants, "HF_HOME", str(cache))
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(cache / "hub"))
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    monkeypatch.setattr(cli_module, "_warm_notice_shown", False)
    monkeypatch.setattr(cli_module, "_first_run_models_ready", False, raising=False)
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("atexit.register"),
    ):
        yield tmp_path
    storage._memory = None


class _Machine:
    """What is installed locally. Only the network boundary flips it."""

    def __init__(self, tmp_path, *, spacy=False, embedding=False):
        self.spacy = spacy
        self.embedding = embedding
        self.snapshot = tmp_path / "snapshot"
        self.memory = MagicMock()
        self.memory.ingest.return_value = "item-first-run"
        self.runtime_download_flags = []

    # spaCy: core's `_ensure_spacy_model` is the network step `sm setup` reuses.
    def is_package(self, name):
        return self.spacy

    def install_spacy(self, model="en_core_web_sm"):
        self.spacy = True

    # HF: a real `resolve_local_path` runs; only snapshot_download is faked.
    def snapshot_download(self, repo_id, **kwargs):
        assert repo_id == DEFAULT_MODEL
        if kwargs.get("local_files_only"):
            if not self.embedding:
                raise OSError("empty cache")
        else:
            self.install_embedding()
        return str(self.snapshot)

    def install_embedding(self):
        (self.snapshot / "onnx").mkdir(parents=True, exist_ok=True)
        (self.snapshot / "onnx/model.onnx").touch()
        (self.snapshot / "tokenizer.json").write_text("{}")
        self.embedding = True

    def create_lite_memory(self, **kwargs):
        """Real core prerequisite checks, then a stand-in memory (no real spaCy load)."""
        from smartmemory.tools import factory

        self.runtime_download_flags.append(kwargs["auto_download_models"])
        model = kwargs["pipeline_profile"].extraction.entity_ruler.spacy_model
        factory._require_spacy_model(model)
        factory._require_embedding_model(allow_download=kwargs["auto_download_models"])
        return self.memory


@pytest.fixture
def machine(_isolated):
    m = _Machine(_isolated)
    with (
        patch("spacy.util.is_package", side_effect=m.is_package),
        patch("huggingface_hub.snapshot_download", side_effect=m.snapshot_download),
        patch(
            "smartmemory.tools.factory.create_lite_memory",
            side_effect=m.create_lite_memory,
        ),
    ):
        yield m


def _add(text="hello world test"):
    import smartmemory_app.cli as cli_module

    return CliRunner().invoke(cli_module.cli, ["add", text])


def _assert_one_line_setup_error(result):
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.output
    errors = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert len(errors) == 1, result.output
    assert "smartmemory setup" in errors[0]
    assert "loading local models" not in result.output, "banner must follow the check"


def _warned(caplog, text):
    return any(
        r.levelno == logging.WARNING and text in r.getMessage() for r in caplog.records
    )


# ── Golden: the real first-run path, network boundary faked ──────────────────


def test_golden_fresh_install_first_add_fetches_both_models_and_saves(
    machine, monkeypatch, _isolated
):
    """Unconfigured install, empty HF cache, no spaCy: first `sm add` fetches and saves.

    Guards the owner-approved contract. A future "never download" change in the CLI
    fails here. So does moving the download into runtime startup (the factory must
    still be called with auto_download_models=False).
    """
    from smartmemory_app.config import config_path

    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    assert not config_path().exists()
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as spacy_dl:
        result = _add()

    assert result.exit_code == 0, result.output
    assert "item-first-run" in result.output
    assert (
        "downloading spaCy language model 'en_core_web_sm' (about 15 MB, one time only)"
        in result.output
    )
    assert (
        f"downloading local embedding model '{DEFAULT_MODEL}' (about 100 MB, one time only)"
        in result.output
    )
    assert "Traceback" not in result.output
    spacy_dl.assert_called_once_with("en_core_web_sm")
    assert machine.spacy and machine.embedding
    assert machine.runtime_download_flags == [False], (
        "runtime startup must stay disk-only"
    )
    assert config_path().exists(), "unconfigured install migrates to local"


def test_golden_second_run_downloads_nothing(machine, monkeypatch):
    machine.spacy = True
    machine.install_embedding()
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
    ):
        result = _add()
    assert result.exit_code == 0, result.output
    assert "downloading" not in result.output
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()


# ── spaCy arm ────────────────────────────────────────────────────────────────


def test_first_add_fetches_missing_spacy_model_once_and_saves(machine, monkeypatch):
    machine.install_embedding()
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as ensure:
        result = _add()

    assert result.exit_code == 0, result.output
    assert "item-first-run" in result.output
    assert "en_core_web_sm" in result.output, "the one-time download must be announced"
    ensure.assert_called_once_with("en_core_web_sm")


def test_configured_spacy_model_and_worker_default_both_fetched(machine, monkeypatch):
    from smartmemory_app.config import SmartMemoryConfig, save_config

    save_config(SmartMemoryConfig(mode="local", spacy_model="en_core_web_md"))
    machine.install_embedding()
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as ensure:
        result = _add()
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in ensure.call_args_list] == [
        "en_core_web_md",
        "en_core_web_sm",
    ]
    assert "'en_core_web_md' (about 40 MB, one time only)" in result.output


def test_first_add_download_failure_prints_setup_instruction(
    machine, monkeypatch, caplog
):
    machine.install_embedding()
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    failure = MissingModelError(
        "Could not install spaCy model 'en_core_web_sm': network unreachable"
    )
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()

    _assert_one_line_setup_error(result)
    assert _warned(caplog, "en_core_web_sm"), (
        "no-silent-degradation: name the lost model"
    )
    assert _warned(caplog, "network unreachable"), "the real cause goes to the log"


# ── embedding arm ────────────────────────────────────────────────────────────


def test_embedding_download_failure_prints_setup_instruction(
    machine, monkeypatch, caplog
):
    machine.spacy = True
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    with (
        patch(
            "smartmemory_app.setup._ensure_embedding_model",
            side_effect=click.ClickException("hub down"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    _assert_one_line_setup_error(result)
    assert _warned(caplog, DEFAULT_MODEL)


def test_non_default_embedder_is_never_auto_fetched(machine, monkeypatch, caplog):
    """Remote-code embedders (nomic) are prepared only by an explicit setup."""
    machine.spacy = True
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    monkeypatch.setenv(
        "SMARTMEMORY_EMBEDDING_LOCAL_MODEL", "nomic-ai/nomic-embed-text-v1.5"
    )
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "torch")
    with (
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        patch(
            "smartmemory.tools.factory._require_embedding_model",
            side_effect=MissingModelError("nomic not cached"),
        ),
        patch("smartmemory.plugins.embedding.EmbeddingService") as service,
        caplog.at_level(logging.WARNING),
    ):
        service.return_value.local_model_name.return_value = (
            "nomic-ai/nomic-embed-text-v1.5"
        )
        result = _add()
    _assert_one_line_setup_error(result)
    embed_dl.assert_not_called()
    assert "nomic-ai/nomic-embed-text-v1.5" in result.output
    assert _warned(caplog, "nomic-ai/nomic-embed-text-v1.5")


# ── interactive gating (Q3/Q4) ───────────────────────────────────────────────


def test_non_tty_default_never_downloads(machine, caplog):
    """Hooks (sm recall in SessionStart), CI and scripts get the setup line."""
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    _assert_one_line_setup_error(result)
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()
    assert _warned(caplog, "SMARTMEMORY_AUTO_DOWNLOAD_MODELS=1")


def test_hook_recall_non_tty_never_downloads(machine):
    import smartmemory_app.cli as cli_module

    with patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl:
        result = CliRunner().invoke(cli_module.cli, ["recall", "--cwd", "/tmp"])
    _assert_one_line_setup_error(result)
    spacy_dl.assert_not_called()


def test_interactive_terminal_downloads_by_default(machine):
    machine.install_embedding()
    with (
        patch("smartmemory_app.cli._stderr_is_tty", return_value=True),
        patch(
            "smartmemory.tools.factory._ensure_spacy_model",
            side_effect=machine.install_spacy,
        ) as spacy_dl,
    ):
        result = _add()
    assert result.exit_code == 0, result.output
    spacy_dl.assert_called_once()


def test_opt_out_env_blocks_download_on_terminal(machine, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "0")
    with (
        patch("smartmemory_app.cli._stderr_is_tty", return_value=True),
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
    ):
        result = _add()
    _assert_one_line_setup_error(result)
    spacy_dl.assert_not_called()


# ── other surfaces ───────────────────────────────────────────────────────────


def test_remote_mode_skips_local_model_check(_isolated, monkeypatch):
    import smartmemory_app.cli as cli_module

    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    with patch(
        "spacy.util.is_package",
        side_effect=AssertionError("local check in remote mode"),
    ):
        cli_module._ensure_first_run_models()


def test_missing_model_elsewhere_is_one_line_not_traceback(machine, caplog):
    """Safety net: any command that still meets MissingModelError reports one line."""
    machine.spacy = True
    machine.install_embedding()
    with (
        patch(
            "smartmemory_app.storage.ingest",
            side_effect=MissingModelError("spaCy model gone"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    errors = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert errors == [
        "Error: Not installed: a required local model. Run: smartmemory setup"
    ]
    assert _warned(caplog, "spaCy model gone")


# ── review findings (Codex sol, 2026-09-30) ──────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [SystemExit(1), KeyError("en_core_web_sm")],
    ids=["spacy-compat-503-exits", "spacy-compat-malformed"],
)
def test_spacy_downloader_non_missing_model_failures_are_one_line(
    machine, monkeypatch, caplog, failure
):
    """spaCy's compatibility lookup can SystemExit or KeyError, not only MissingModelError."""
    machine.install_embedding()
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    _assert_one_line_setup_error(result)
    assert _warned(caplog, "en_core_web_sm")


def test_non_default_embedder_without_torch_is_one_line(machine, monkeypatch, caplog):
    """Backend resolution fails before the prerequisite's own error wrapping."""
    from smartmemory.plugins.embedding import EmbeddingService

    machine.spacy = True
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", "1")
    monkeypatch.setenv(
        "SMARTMEMORY_EMBEDDING_LOCAL_MODEL", "nomic-ai/nomic-embed-text-v1.5"
    )
    advice = 'nomic needs torch; run pip install "smartmemory-core[torch]"'
    with (
        patch.object(
            EmbeddingService, "_resolve_backend", side_effect=RuntimeError(advice)
        ),
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    errors = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert errors == [f"Error: {advice}. Then run: smartmemory setup"]
    embed_dl.assert_not_called()
    assert _warned(caplog, advice)


def test_remote_mode_direct_access_has_no_local_banner(_isolated, monkeypatch):
    import smartmemory_app.cli as cli_module

    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    with (
        patch(
            "smartmemory_app.warm.is_warm",
            side_effect=AssertionError("local warm probe"),
        ),
        patch("spacy.util.is_package", side_effect=AssertionError("local model check")),
        patch("smartmemory_app.storage.get", return_value={"item_id": "remote-1"}),
    ):
        result = CliRunner().invoke(cli_module.cli, ["get", "remote-1"])
    assert result.exit_code == 0, result.output
    assert "loading local models" not in result.output


@pytest.mark.parametrize(
    "argv, fallback",
    [
        (["search", "hello"], "smartmemory_app.storage.search"),
        (["get", "item-1"], "smartmemory_app.storage.get"),
    ],
    ids=["search", "get"],
)
def test_other_direct_commands_run_the_first_run_check(machine, argv, fallback):
    """search and get are routed through the same gate as add and recall."""
    import smartmemory_app.cli as cli_module

    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch(fallback, side_effect=AssertionError("reached storage before the check")),
    ):
        result = CliRunner().invoke(cli_module.cli, argv)
    _assert_one_line_setup_error(result)
    spacy_dl.assert_not_called()
