"""LITE-FIRSTRUN-SPACY-1: a fresh install's first direct `sm add` must not crash.

`pip install smartmemory` cannot bring spaCy's `en_core_web_sm` (it is not on PyPI),
so the first command that needs it fetches it once through the same downloader
`sm setup` uses, then continues. When that download fails, the user gets one line
naming `sm setup`, never a traceback.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from smartmemory.errors import MissingModelError


@pytest.fixture(autouse=True)
def _reset_storage():
    import smartmemory_app.cli as cli_module
    import smartmemory_app.storage as storage

    storage._memory = None
    storage._remote_memory = None
    cli_module._warm_notice_shown = False
    yield
    storage._memory = None
    storage._remote_memory = None


@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """A configured local install whose spaCy model is not installed yet."""
    from smartmemory.tools import factory
    from smartmemory_app.config import SmartMemoryConfig

    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    state = {"spacy_installed": False}
    memory = MagicMock()
    memory.ingest.return_value = "item-first-run"

    def fake_create_lite_memory(**kwargs):
        # Same prerequisite check the real factory runs before building anything.
        factory._require_spacy_model(
            kwargs["pipeline_profile"].extraction.entity_ruler.spacy_model
        )
        return memory

    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.is_configured", return_value=True),
        patch(
            "smartmemory_app.storage.load_config",
            return_value=SmartMemoryConfig(mode="local"),
        ),
        patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path),
        patch(
            "spacy.util.is_package", side_effect=lambda name: state["spacy_installed"]
        ),
        patch("smartmemory.tools.factory._require_embedding_model"),
        patch(
            "smartmemory.tools.factory.create_lite_memory",
            side_effect=fake_create_lite_memory,
        ),
        patch("atexit.register"),
    ):
        yield state


def _invoke_add():
    import smartmemory_app.cli as cli_module

    return CliRunner().invoke(cli_module.cli, ["add", "hello world test"])


def test_first_add_fetches_missing_spacy_model_once_and_saves(fresh_install):
    def download(model="en_core_web_sm"):
        fresh_install["spacy_installed"] = True

    with patch(
        "smartmemory.tools.factory._ensure_spacy_model", side_effect=download
    ) as ensure:
        result = _invoke_add()

    assert result.exit_code == 0, result.output
    assert "item-first-run" in result.output
    assert "en_core_web_sm" in result.output, "the one-time download must be announced"
    assert "Traceback" not in result.output
    ensure.assert_called()
    assert {
        c.args[0] if c.args else c.kwargs.get("model", "en_core_web_sm")
        for c in ensure.call_args_list
    } == {"en_core_web_sm"}


def test_first_add_download_failure_prints_setup_instruction(fresh_install, caplog):
    failure = MissingModelError(
        "Could not install spaCy model 'en_core_web_sm': network unreachable"
    )

    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = _invoke_add()

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), (
        "a traceback, not a clean CLI error"
    )
    assert "Traceback" not in result.output
    assert "sm setup" in result.output
    error_lines = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert len(error_lines) == 1
    assert any(
        r.levelno == logging.WARNING and "en_core_web_sm" in r.getMessage()
        for r in caplog.records
    ), "no-silent-degradation: the lost model must be named at WARNING"
