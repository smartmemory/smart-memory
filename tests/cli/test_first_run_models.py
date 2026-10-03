"""LITE-FIRSTRUN-SPACY-1: a fresh install's first direct `sm add` must not crash.

`pip install smartmemory` cannot bring spaCy's `en_core_web_sm` (it is not on PyPI)
or the default embedding snapshot. `sm add` fetches both once, terminal or not,
through the downloaders `sm setup` uses, with one-line notices on stderr, then saves.
`sm search` and `sm get` fetch the same way. `sm recall` and the hook `lifecycle`
commands, an opt-out, a failed download, or a non-default (remote-code) embedder
give one line naming `smartmemory setup` and a WARNING, never a traceback. Runtime startup (storage,
daemon, worker) stays disk-only.
"""

import logging
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from smartmemory.errors import MissingModelError

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _runner() -> CliRunner:
    """A runner that keeps stdout and stderr apart on every supported click.

    click < 8.2 mixes stderr into ``result.stdout`` unless ``mix_stderr=False``;
    click 8.2 removed the parameter and always separates them.
    """
    import inspect

    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _text(result) -> str:
    """Both streams, for substring checks (click < 8.2's ``output`` is stdout-only).

    A newline separates them so an unterminated stdout fragment cannot merge into
    the first stderr line. Line counts use ``result.stderr`` directly.
    """
    sep = "\n" if result.stdout and not result.stdout.endswith("\n") else ""
    return result.stdout + sep + result.stderr


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

    return _runner().invoke(cli_module.cli, ["add", text])


def _invoke(argv):
    import smartmemory_app.cli as cli_module

    return _runner().invoke(cli_module.cli, argv)


def _invoke_with_input(argv, text):
    import smartmemory_app.cli as cli_module

    return _runner().invoke(cli_module.cli, argv, input=text)


def _assert_one_line_setup_error(result):
    assert result.exit_code == 1, _text(result)
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in _text(result)
    errors = [ln for ln in result.stderr.splitlines() if ln.startswith("Error:")]
    assert len(errors) == 1, _text(result)
    assert "smartmemory setup" in errors[0]
    assert "loading local models" not in _text(result), "banner must follow the check"
    assert result.stdout == "", "errors and notices stay off stdout"
    return errors[0]


def _warned(caplog, text):
    return any(
        r.levelno == logging.WARNING and text in r.getMessage() for r in caplog.records
    )


# ── Golden: the real first-run path, network boundary faked ──────────────────


def test_golden_fresh_install_first_add_fetches_both_models_and_saves(
    machine, _isolated
):
    """Unconfigured install, empty HF cache, no spaCy, no terminal: `sm add` saves.

    Guards the owner-approved contract. A future "never download" change in `sm add`
    fails here. So does moving the download into runtime startup (the factory must
    still be called with auto_download_models=False).
    """
    from smartmemory_app.config import config_path

    assert not config_path().exists()
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as spacy_dl:
        result = _add()

    assert result.exit_code == 0, _text(result)
    assert result.stdout == "item-first-run\n", "scripts piping stdout see only the id"
    notices = result.stderr
    assert (
        "Downloading spaCy language model 'en_core_web_sm' (about 15 MB, one time only)..."
        in notices
    )
    assert "Downloaded spaCy language model 'en_core_web_sm'." in notices
    assert (
        f"Downloading local embedding model '{DEFAULT_MODEL}' (about 100 MB, one time only)..."
        in notices
    )
    assert f"Downloaded local embedding model '{DEFAULT_MODEL}'." in notices
    assert "Traceback" not in _text(result)
    spacy_dl.assert_called_once_with("en_core_web_sm")
    assert machine.spacy and machine.embedding
    assert machine.runtime_download_flags == [False], (
        "runtime startup must stay disk-only"
    )
    assert config_path().exists(), "unconfigured install migrates to local"


def test_golden_second_run_downloads_nothing(machine):
    machine.spacy = True
    machine.install_embedding()
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
    ):
        result = _add()
    assert result.exit_code == 0, _text(result)
    assert "ownload" not in _text(result)
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()


# ── spaCy arm ────────────────────────────────────────────────────────────────


def test_add_fetches_missing_spacy_model_once_and_saves(machine):
    machine.install_embedding()
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as ensure:
        result = _add()

    assert result.exit_code == 0, _text(result)
    assert result.stdout == "item-first-run\n"
    assert "Downloaded spaCy language model 'en_core_web_sm'." in result.stderr
    ensure.assert_called_once_with("en_core_web_sm")


def test_add_via_stdin_fetches_too(machine):
    machine.install_embedding()
    import smartmemory_app.cli as cli_module

    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as ensure:
        result = _runner().invoke(cli_module.cli, ["add", "-"], input="line one\n")
    assert result.exit_code == 0, _text(result)
    ensure.assert_called_once_with("en_core_web_sm")


def test_configured_spacy_model_and_worker_default_both_fetched(machine):
    from smartmemory_app.config import SmartMemoryConfig, save_config

    save_config(SmartMemoryConfig(mode="local", spacy_model="en_core_web_md"))
    machine.install_embedding()
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as ensure:
        result = _add()
    assert result.exit_code == 0, _text(result)
    # The fake installs every spaCy model at once, so setup announces only the first.
    assert [c.args[0] for c in ensure.call_args_list] == [
        "en_core_web_md",
        "en_core_web_sm",
    ]
    assert "'en_core_web_md' (about 40 MB, one time only)" in result.stderr


@pytest.mark.parametrize(
    "failure",
    [
        MissingModelError("Could not install spaCy model 'en_core_web_sm': offline"),
        SystemExit(1),
        KeyError("en_core_web_sm"),
    ],
    ids=["missing-model", "spacy-compat-503-exits", "spacy-compat-malformed"],
)
def test_spacy_download_failure_is_one_line(machine, caplog, failure):
    machine.install_embedding()
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()

    line = _assert_one_line_setup_error(result)
    assert line == (
        "Error: Could not download spaCy language model 'en_core_web_sm'. "
        "Run: smartmemory setup"
    )
    assert _warned(caplog, "en_core_web_sm"), (
        "no-silent-degradation: name the lost model"
    )
    cause = (
        "offline" if isinstance(failure, MissingModelError) else type(failure).__name__
    )
    assert _warned(caplog, cause), "the real cause goes to the log"


# ── embedding arm ────────────────────────────────────────────────────────────


def test_embedding_download_failure_is_one_line(machine, caplog):
    machine.spacy = True
    with (
        patch(
            "smartmemory_app.setup._ensure_embedding_model",
            side_effect=click.ClickException("hub down"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    line = _assert_one_line_setup_error(result)
    assert line == (
        f"Error: Could not download local embedding model '{DEFAULT_MODEL}'. "
        "Run: smartmemory setup"
    )
    assert _warned(caplog, "hub down")


def test_non_default_embedder_is_never_auto_fetched(machine, monkeypatch, caplog):
    """Remote-code embedders (nomic) are prepared only by an explicit setup."""
    machine.spacy = True
    nomic = "nomic-ai/nomic-embed-text-v1.5"
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_LOCAL_MODEL", nomic)
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "torch")
    with (
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        patch(
            "smartmemory.tools.factory._require_embedding_model",
            side_effect=MissingModelError("nomic not cached"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    line = _assert_one_line_setup_error(result)
    embed_dl.assert_not_called()
    assert nomic in line
    assert "runs code from its model repository" in line, "say why setup is required"
    assert _warned(caplog, "nomic not cached")


def test_non_default_embedder_without_torch_is_one_line(machine, monkeypatch, caplog):
    """Backend resolution fails before the prerequisite's own error wrapping.

    Core's real resolver runs (torch reported missing); the line names the
    configured nomic model and why setup owns it, and core's advice goes to the log.
    """
    import importlib

    machine.spacy = True
    nomic = "nomic-ai/nomic-embed-text-v1.5"
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_LOCAL_MODEL", nomic)
    real_import = importlib.import_module

    def no_torch(name, *args, **kwargs):
        if name in {"sentence_transformers", "torch"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    with (
        patch("importlib.import_module", side_effect=no_torch),
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    line = _assert_one_line_setup_error(result)
    assert nomic in line
    assert "runs code from its model repository" in line
    embed_dl.assert_not_called()


def test_default_embedder_without_runtime_shows_core_advice(machine, caplog):
    from smartmemory.plugins.embedding import EmbeddingService

    machine.spacy = True
    advice = 'MiniLM needs onnxruntime; pip install "smartmemory-core[onnx]"'
    with (
        patch.object(
            EmbeddingService, "_resolve_backend", side_effect=RuntimeError(advice)
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    line = _assert_one_line_setup_error(result)
    assert line == f"Error: {advice}. Then run: smartmemory setup"


# ── who may download ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "FALSE"])
def test_opt_out_env_blocks_download(machine, monkeypatch, caplog, value):
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", value)
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    line = _assert_one_line_setup_error(result)
    assert "SMARTMEMORY_AUTO_DOWNLOAD_MODELS=0" in line
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()


def test_recall_never_downloads(machine, caplog):
    """`sm recall` runs inside the SessionStart hook with a timeout: setup line only."""
    with (
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        patch(
            "smartmemory_app.storage.recall",
            side_effect=AssertionError("reached storage before the check"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = _invoke(["recall", "--cwd", "/tmp"])
    line = _assert_one_line_setup_error(result)
    assert line.startswith(
        "Error: Not installed: spaCy language model 'en_core_web_sm'"
    )
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()
    assert _warned(caplog, "never downloads")


def test_hook_lifecycle_recall_never_downloads(machine, caplog):
    """Hooks call `smartmemory lifecycle ...`; the real path never reaches a downloader.

    The lifecycle degrades by design (a hook must not fail the session): exit 0 and
    a WARNING that carries core's own "Run sm setup" instruction.
    """
    import json

    with (
        patch("smartmemory_app.cli._lifecycle_via_daemon", return_value=None),
        patch("smartmemory.tools.factory._ensure_spacy_model") as spacy_dl,
        patch("smartmemory_app.setup._ensure_embedding_model") as embed_dl,
        patch("smartmemory_app.setup._ensure_spacy") as setup_spacy,
        caplog.at_level(logging.WARNING),
    ):
        result = _invoke_with_input(
            ["lifecycle", "recall"],
            json.dumps({"session_id": "s1", "cwd": "/tmp", "prompt": "hello"}),
        )
    assert result.exit_code == 0, _text(result)
    assert "Traceback" not in _text(result)
    spacy_dl.assert_not_called()
    embed_dl.assert_not_called()
    setup_spacy.assert_not_called()
    # Core's wording varies by release ("Run sm setup ..." in 1.5.x, "... then
    # rerun sm setup" in 1.4.x); both name the fix.
    assert _warned(caplog, "sm setup"), "the hook still names the fix"
    assert not machine.spacy and not machine.embedding


@pytest.mark.parametrize(
    "argv, fallback, value",
    [
        (["search", "hello"], "smartmemory_app.storage.search", []),
        (["get", "item-1"], "smartmemory_app.storage.get", {"item_id": "item-1"}),
    ],
    ids=["search", "get"],
)
def test_search_and_get_download_missing_models(machine, argv, fallback, value):
    """Direct-storage user commands fetch the models like `sm add`, terminal or not."""
    with (
        patch(
            "smartmemory.tools.factory._ensure_spacy_model",
            side_effect=machine.install_spacy,
        ) as spacy_dl,
        patch(fallback, return_value=value),
    ):
        result = _invoke(argv)
    assert result.exit_code == 0, _text(result)
    spacy_dl.assert_called_once_with("en_core_web_sm")
    assert machine.embedding
    assert "Downloaded spaCy language model 'en_core_web_sm'." in result.stderr
    assert "ownload" not in result.stdout


# ── other surfaces ───────────────────────────────────────────────────────────


def test_remote_mode_skips_local_model_check(_isolated, monkeypatch):
    import smartmemory_app.cli as cli_module

    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    with patch(
        "spacy.util.is_package",
        side_effect=AssertionError("local check in remote mode"),
    ):
        cli_module._ensure_first_run_models(download=True)


def test_remote_mode_direct_access_has_no_local_banner(_isolated, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    with (
        patch(
            "smartmemory_app.warm.is_warm",
            side_effect=AssertionError("local warm probe"),
        ),
        patch("spacy.util.is_package", side_effect=AssertionError("local model check")),
        patch("smartmemory_app.storage.get", return_value={"item_id": "remote-1"}),
    ):
        result = _invoke(["get", "remote-1"])
    assert result.exit_code == 0, _text(result)
    assert "loading local models" not in _text(result)


@pytest.mark.parametrize("error", ["missing", "hf"])
def test_model_missing_later_is_one_line_not_traceback(machine, caplog, error):
    """Safety net: a model error past the check still reports one line."""
    from smartmemory.utils.hf_models import HFModelUnavailable

    machine.spacy = True
    machine.install_embedding()
    failure = (
        MissingModelError("spaCy model gone")
        if error == "missing"
        else HFModelUnavailable("MiniLM not cached")
    )
    with (
        patch("smartmemory_app.storage.ingest", side_effect=failure),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    assert result.exit_code == 1
    assert "Traceback" not in _text(result)
    errors = [ln for ln in result.stderr.splitlines() if ln.startswith("Error:")]
    assert result.stdout == "", "errors stay off stdout"
    assert errors == [
        "Error: A required local model is not installed. Run: smartmemory setup"
    ]
    assert _warned(caplog, str(failure))


# ── sm setup uses the same messages ──────────────────────────────────────────


def test_setup_spacy_messages_only_for_missing_models(machine, capsys):
    from smartmemory_app import setup

    def install(model):
        print(f"core chatter for {model}")  # core's own lines go to the debug log
        machine.install_spacy(model)

    with patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=install):
        setup._ensure_spacy("en_core_web_sm")
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "Downloading spaCy language model 'en_core_web_sm' (about 15 MB, one time only)...",
            "Downloaded spaCy language model 'en_core_web_sm'.",
        ]
        setup._ensure_spacy("en_core_web_sm")
        assert capsys.readouterr().out == "", "installed models are silent"


def test_setup_embedding_messages(machine, capsys, monkeypatch):
    from smartmemory_app import setup

    monkeypatch.delenv("SMARTMEMORY_EMBEDDING_PROVIDER", raising=False)
    setup._ensure_embedding_model("local")
    assert capsys.readouterr().out.splitlines() == [
        f"Preparing local embedding model '{DEFAULT_MODEL}' "
        "(downloads missing files, about 100 MB, one time only)...",
        f"Local embedding model '{DEFAULT_MODEL}' ready.",
    ]
    assert machine.embedding


@pytest.mark.parametrize("value", ["1", "no", "off", "yes"])
def test_only_zero_or_false_turns_download_off(machine, monkeypatch, value):
    monkeypatch.setenv("SMARTMEMORY_AUTO_DOWNLOAD_MODELS", value)
    machine.install_embedding()
    with patch(
        "smartmemory.tools.factory._ensure_spacy_model",
        side_effect=machine.install_spacy,
    ) as spacy_dl:
        result = _add()
    assert result.exit_code == 0, _text(result)
    spacy_dl.assert_called_once()


def test_download_note_sizes_the_default_model_alias():
    from smartmemory_app import setup

    assert setup._download_note("all-MiniLM-L6-v2") == "about 100 MB, one time only"
    assert setup._download_note(DEFAULT_MODEL) == "about 100 MB, one time only"
    assert setup._download_note("some/other-model") == "one time only"


def test_wrapped_model_error_is_one_line(machine, caplog):
    """Core's store stage wraps embed failures (VectorWriteError); unwrap the cause."""
    from smartmemory.utils.hf_models import HFModelUnavailable

    machine.spacy = True
    machine.install_embedding()

    class VectorWriteError(RuntimeError):
        pass

    def ingest(*args, **kwargs):
        try:
            raise HFModelUnavailable("MiniLM vanished")
        except HFModelUnavailable as inner:
            raise VectorWriteError("vector write failed") from inner

    with (
        patch("smartmemory_app.storage.ingest", side_effect=ingest),
        caplog.at_level(logging.WARNING),
    ):
        result = _add()
    assert result.exit_code == 1
    assert "Traceback" not in _text(result)
    errors = [ln for ln in result.stderr.splitlines() if ln.startswith("Error:")]
    assert result.stdout == "", "errors stay off stdout"
    assert errors == [
        "Error: A required local model is not installed. Run: smartmemory setup"
    ]
    assert _warned(caplog, "vector write failed")


def test_unrelated_error_still_raises(machine):
    machine.spacy = True
    machine.install_embedding()
    with patch("smartmemory_app.storage.ingest", side_effect=ValueError("boom")):
        result = _add()
    assert result.exit_code == 1
    assert result.stdout == "", "errors stay off stdout"
    assert "ValueError: boom" in result.stderr
    assert "smartmemory report --zip" in result.stderr
    assert "Traceback" not in _text(result)
    from smartmemory_app.bug_report import debug_log_path

    crash_log = debug_log_path().read_text()
    assert "Traceback" in crash_log and "ValueError:" in crash_log


@pytest.mark.parametrize(
    "failure", [SystemExit(1), KeyError("x")], ids=["exit", "keyerror"]
)
def test_setup_spacy_non_missing_failures_are_click_errors(machine, capsys, failure):
    from smartmemory_app import setup

    def install(model):
        print("spaCy says: compatibility table unavailable")
        raise failure

    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=install),
        pytest.raises(click.ClickException) as caught,
    ):
        setup._ensure_spacy("en_core_web_sm")
    assert "Could not install spaCy model 'en_core_web_sm'" in caught.value.message
    assert "Rerun sm setup" in caught.value.message
    assert "compatibility table unavailable" in capsys.readouterr().err


def test_add_failure_keeps_installer_chatter_off_the_console(
    machine, caplog, monkeypatch
):
    """sm add's failure is the notice, the WARNING and one Error line, nothing else."""
    machine.install_embedding()
    # The CLI resets console handler levels; DEBUG lets caplog see the debug record.
    monkeypatch.setenv("SMARTMEMORY_LOG_LEVEL", "DEBUG")

    def install(model):
        print(f"Downloading spaCy model '{model}' (first run only)...")
        raise MissingModelError(f"Could not install spaCy model {model!r}: offline")

    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=install),
        caplog.at_level(logging.DEBUG),
    ):
        result = _add()
    assert result.exit_code == 1
    errors = [ln for ln in result.stderr.splitlines() if ln.startswith("Error:")]
    assert result.stdout == "", "errors stay off stdout"
    assert len(errors) == 1 and "smartmemory setup" in errors[0]
    assert "Traceback" not in _text(result)
    assert "(first run only)" not in _text(result)
    assert any(
        r.levelno == logging.DEBUG and "(first run only)" in r.getMessage()
        for r in caplog.records
    ), "the installer output is kept in the debug log"


def test_suppressed_context_is_not_mislabelled(machine):
    """`raise X from None` after a handled model error keeps X."""
    machine.spacy = True
    machine.install_embedding()

    def ingest(*args, **kwargs):
        try:
            raise MissingModelError("recovered")
        except MissingModelError:
            raise ValueError("invalid unrelated field") from None

    with patch("smartmemory_app.storage.ingest", side_effect=ingest):
        result = _add()
    assert result.exit_code == 1
    assert result.stdout == "", "errors stay off stdout"
    assert "ValueError: invalid unrelated field" in result.stderr
    assert "smartmemory report --zip" in result.stderr
    assert "Traceback" not in _text(result)
    from smartmemory_app.bug_report import debug_log_path

    crash_log = debug_log_path().read_text()
    assert "Traceback" in crash_log and "ValueError:" in crash_log
    assert "MissingModelError" not in _text(result) + crash_log


def test_add_embedding_download_shows_no_progress_bars(machine):
    """sm add reports the embedding download with its two lines, not HF bars."""
    import sys

    from huggingface_hub.utils import are_progress_bars_disabled

    machine.spacy = True
    before = are_progress_bars_disabled()

    def download(provider, *, missing=False):
        print("Downloading local embedding model 'x' (about 100 MB, one time only)...")
        sys.stderr.write("Fetching 2 files:  50%|#####     | 1/2\r")
        machine.install_embedding()
        print("Downloaded local embedding model 'x'.")

    with patch("smartmemory_app.setup._ensure_embedding_model", side_effect=download):
        result = _add()
    assert result.exit_code == 0, _text(result)
    assert "Fetching 2 files" not in _text(result)
    assert "Downloaded local embedding model 'x'." in result.stderr
    assert result.stdout == "item-first-run\n"
    assert are_progress_bars_disabled() == before, "HF global state untouched"
