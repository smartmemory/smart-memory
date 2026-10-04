"""R3 regressions through the real wrapper boundaries."""

import json
from unittest.mock import patch

import pytest
from smartmemory_app.hook_failures import marker_directory as real_marker_directory

from smartmemory_app import crash_reporter, support_diagnostics, config


def test_captured_config_doctor_privacy(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("USERNAME", "Current User")
    monkeypatch.setenv("HTTPS_PROXY", "https://alice-laptop.corp.internal")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.delenv("SMARTMEMORY_API_URL", raising=False)
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        '[smartmemory]\nmode="remote"\n[remote]\napi_url="https://customer-private.internal"\n'
    )
    assert config.load_config().api_url == "https://customer-private.internal"
    rows = support_diagnostics.network_checks() + [support_diagnostics.proxy_summary()]
    rows += [
        r"C:\Users\Jane Doe\cache",
        "/Users/OtherPerson/cache",
        "/home/Some Person/cache",
        "Current User",
        "test_Current User_suffix",
        "http://localhost:9001",
        "https://api.smartmemory.ai",
        "http://127.0.0.1",
        "http://[::1]:9001",
    ]
    with patch.object(crash_reporter, "_post", return_value=True) as post:
        result = crash_reporter.report_exception(
            RuntimeError("setup failed"),
            source="install",
            args=["setup"],
            data_dir=tmp_path,
            handled=True,
            context={"doctor": rows},
        )
    assert result.status == "sent"
    body = json.dumps(post.call_args.args[0])
    for forbidden in (
        "alice-laptop.corp.internal",
        "customer-private.internal",
        "OtherPerson",
        "Jane",
        "Doe",
        "Some Person",
        "Current User",
        "C:\\\\Users",
        "/Users/",
        "/home/",
    ):
        assert forbidden not in body, forbidden
    assert "https://<custom-host>" in body
    for allowed in ("localhost", "api.smartmemory.ai", "127.0.0.1", "[::1]"):
        assert allowed in body


def test_lifecycle_real_startup_skips_pending_diagnostics(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    driver = tmp_path / "startup.py"
    driver.write_text("""import os,time
from pathlib import Path
from smartmemory_app import install_check, crash_reporter, install_troubleshooting
trace=Path(os.environ['R3_TRACE'])
def native():
 trace.write_text('native called')
 if os.environ.get('R3_SLOW'):
  time.sleep(6)
  return ['Native usearch: FAIL: DLL. Fix: reinstall']
 return ['Native libraries: OK']
def post(payload):
 trace.write_text('transport called')
 time.sleep(10)
 return True
install_check.run_native_checks=native
crash_reporter._post=post
install_troubleshooting.doctor_summary=lambda **kw: []
from smartmemory_app.cli import cli
cli.main(['lifecycle','recall'])
""")
    env = os.environ.copy()
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        APPDATA=str(tmp_path / "config"),
        SMARTMEMORY_CRASH_REPORTS="1",
        SMARTMEMORY_MODE="local",
        SMARTMEMORY_DATA_DIR=str(home / ".smartmemory"),
        R3_TRACE=str(tmp_path / "calls"),
    )
    samples = []
    for pending in (False, True):
        data = home / ".smartmemory"
        data.mkdir(exist_ok=True)
        for marker in data.glob(".install-check*"):
            marker.unlink()
        if pending:
            (data / "hook-failures.tsv").write_text("recall\t127\t0\n")
            env["R3_SLOW"] = "1"
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(driver)],
            input="",
            text=True,
            capture_output=True,
            env=env,
            timeout=25,
        )
        samples.append(time.monotonic() - started)
        assert result.returncode == 0, result.stderr
    assert samples[1] < samples[0] + 0.5, samples
    assert (data / "hook-failures.tsv").read_text() == "recall\t127\t0\n"
    assert not (data / ".hook-failure-state.json").exists()
    assert not Path(env["R3_TRACE"]).exists()


def test_interactive_report_wait_is_bounded(monkeypatch, tmp_path):
    import threading
    import time
    from smartmemory_app import install_troubleshooting

    done = threading.Event()
    release = threading.Event()

    def post(payload):
        release.wait(10)
        done.set()
        return True

    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setattr(crash_reporter, "_post", post)
    monkeypatch.setattr(install_troubleshooting, "doctor_summary", lambda **kw: [])
    try:
        started = time.monotonic()
        install_troubleshooting.handled_install_failure(
            RuntimeError("setup failed"), ["setup"]
        )
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        assert done.wait(2)


def test_warm_real_core_swallowed_embedding_failure(monkeypatch):
    import pytest
    from smartmemory.plugins.embedding import EmbeddingService
    from smartmemory_app.warm import warm_models

    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")

    def broken_loader(self, text):
        raise OSError("DLL load failed below real warm")

    monkeypatch.setattr(EmbeddingService, "_embed_local", broken_loader)
    assert EmbeddingService().warm() is False
    with pytest.raises(RuntimeError, match="Embedding model could not be loaded"):
        warm_models(reranker=False, strict=True)


def test_warm_real_core_swallowed_reranker_failure(monkeypatch):
    import pytest
    from smartmemory.plugins.embedding import EmbeddingService
    from smartmemory.search.rerank import CrossEncoderReranker
    from smartmemory_app.warm import warm_models

    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")

    def loaded(self, text):
        self._backend_name = "test-local"
        return [0.1]

    def broken_loader(name):
        raise OSError("DLL load failed below real get_model")

    monkeypatch.setattr(EmbeddingService, "_embed_local", loaded)
    monkeypatch.setattr(CrossEncoderReranker, "_models", {})
    monkeypatch.setattr(CrossEncoderReranker, "_attempted", set())
    monkeypatch.setattr(CrossEncoderReranker, "_construct", broken_loader)
    with pytest.raises(RuntimeError, match="Reranker model could not be loaded"):
        warm_models(strict=True)


def test_api_embedding_warm_is_intentional_noop(monkeypatch):
    from smartmemory_app.warm import warm_models

    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "ollama")
    warm_models(reranker=False, strict=True)


def test_doctor_never_acquires_store_lock(monkeypatch, tmp_path):
    import os
    import pytest
    from smartmemory_app.store_diagnostics import lock_row

    (tmp_path / ".write.lock").touch()

    def forbidden(*args):
        pytest.fail("doctor attempted to acquire store lock")

    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", forbidden)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", forbidden)
    assert "ownership unverified" in lock_row(tmp_path)
    (tmp_path / ".worker.pid").write_text(str(os.getpid()))
    assert "live store owner" in lock_row(tmp_path)


def test_setup_warm_step_checks_real_loader(monkeypatch):
    import click
    import pytest
    from smartmemory.plugins.embedding import EmbeddingService
    from smartmemory.tools import factory
    from smartmemory_app.setup import _ensure_embedding_model

    def broken_loader(self, text):
        raise OSError("DLL load failed in setup warm")

    monkeypatch.setattr(EmbeddingService, "_embed_local", broken_loader)
    monkeypatch.setattr(
        EmbeddingService, "_resolve_backend", lambda self: "onnxruntime/cpu"
    )
    monkeypatch.setattr(factory, "_require_embedding_model", lambda **kwargs: None)
    with pytest.raises(
        click.ClickException, match="Embedding model could not be loaded"
    ):
        _ensure_embedding_model("local")


def test_toml_only_data_dir_hook_marker_agreement(monkeypatch, tmp_path):
    import os
    import subprocess
    from pathlib import Path
    from click.testing import CliRunner
    from smartmemory_app.cli import cli
    from smartmemory_app import bug_report
    from smartmemory_app import hook_failures

    monkeypatch.setattr(hook_failures, "marker_directory", real_marker_directory)

    home = tmp_path / "profile"
    home.mkdir()
    custom = tmp_path / "SM Data With Spaces"
    custom.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.delenv("SMARTMEMORY_DATA_DIR", raising=False)
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        f'[smartmemory]\nmode="local"\n[local]\ndata_dir={json.dumps(str(custom))}\n'
    )
    assert config.load_config().data_dir == str(custom)
    # Override only the unrelated fixture's fake log path with config's real path.
    monkeypatch.setattr(
        bug_report,
        "debug_log_path",
        lambda: Path(config.load_config().data_dir) / "cli-debug.log",
    )
    hook = Path(__import__("smartmemory_app").__file__).parent / "hooks/recall.sh"
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        ["/bin/bash", str(hook)], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 0
    marker = home / ".smartmemory/hook-failures.tsv"
    assert "recall\t127\t" in marker.read_text()
    with patch.object(crash_reporter, "_post", return_value=True) as post:
        CliRunner().invoke(cli, ["status"])
        CliRunner().invoke(cli, ["status"])
    assert post.call_count == 1
    assert not (custom / "hook-failures.tsv").exists()
    state = json.loads((marker.parent / ".hook-failure-state.json").read_text())
    assert state["offset"] == marker.stat().st_size


@pytest.mark.parametrize(
    "phase", ["orient", "recall", "observe", "learn", "distill", "persist"]
)
@pytest.mark.parametrize("kind", ["handled", "model", "unexpected"])
def test_hook_failure_never_sends_or_auto_diagnoses(monkeypatch, phase, kind):
    import click
    from click.testing import CliRunner
    from smartmemory.errors import MissingModelError
    from smartmemory_app import (
        cli as cli_mod,
        install_check,
        install_troubleshooting,
        hook_failures,
    )

    errors = {
        "handled": click.ClickException("setup failed"),
        "model": MissingModelError("missing model"),
        "unexpected": RuntimeError("unexpected hook failure"),
    }

    def fail():
        raise errors[kind]

    def forbidden(*args, **kwargs):
        raise AssertionError("hook attempted diagnostics or reporting")

    monkeypatch.setattr(
        cli_mod.cli.commands["lifecycle"].commands[phase], "callback", fail
    )
    monkeypatch.setattr(install_check, "first_run_check", forbidden)
    monkeypatch.setattr(hook_failures, "consume_in_background", forbidden)
    monkeypatch.setattr(install_troubleshooting, "doctor_summary", forbidden)
    monkeypatch.setattr(
        install_troubleshooting, "is_install_failure", lambda *args: True
    )
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(crash_reporter, "_post", side_effect=forbidden) as post:
        result = CliRunner().invoke(cli_mod.cli, ["lifecycle", phase])
    assert result.exit_code == 1
    assert "AssertionError" not in result.output
    post.assert_not_called()


def test_background_markers_commit_after_attempt_under_lock(monkeypatch, tmp_path):
    import threading
    import time
    from smartmemory_app import hook_failures

    marker = tmp_path / "hook-failures.tsv"
    marker.write_text("recall\t127\t0\n")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = hook_failures.consume_failures

    def post(payload):
        entered.set()
        release.wait(10)
        return True

    def consume(data):
        try:
            original(data)
        finally:
            finished.set()

    monkeypatch.setattr(hook_failures, "consume_failures", consume)
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(crash_reporter, "_post", side_effect=post) as transport:
        try:
            started = time.monotonic()
            hook_failures.consume_in_background(tmp_path)
            assert time.monotonic() - started < 0.5
            assert entered.wait(1)
            assert not (tmp_path / ".hook-failure-state.json").exists()
            # Concurrent consumer cannot take the first worker's interprocess lock.
            original(tmp_path)
            assert not (tmp_path / ".hook-failure-state.json").exists()
        finally:
            release.set()
            assert finished.wait(2)
        original(tmp_path)
        assert transport.call_count == 1
    assert (
        json.loads((tmp_path / ".hook-failure-state.json").read_text())["offset"]
        == marker.stat().st_size
    )
