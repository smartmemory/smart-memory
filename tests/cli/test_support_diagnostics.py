"""Exercise persisted crash evidence and one-file support diagnostics offline."""

import logging
import sys
import threading
import time
import zipfile
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import (
    bug_report,
    cli as cli_module,
    setup,
    support_diagnostics as support,
)
from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

_REAL_DAEMON_STATUS = support._daemon_status

KEY = "gsk_" + "TestSupportSecret" * 3


@pytest.fixture(autouse=True)
def restore_logging_and_hooks():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    formats = [(h, h.formatter, h.level) for h in handlers]
    process_hook, thread_hook = sys.excepthook, threading.excepthook
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for handler, formatter, handler_level in formats:
        handler.setFormatter(formatter)
        handler.setLevel(handler_level)
    sys.excepthook, threading.excepthook = process_hook, thread_hook


@pytest.mark.parametrize("command", ["setup", "start"])
def test_uncaught_failure_records_trace_and_short_message(
    monkeypatch, tmp_path, command
):
    def fail(**kwargs):
        raise httpx.ReadTimeout(
            f"download failed {KEY} https://example.test/model?signature=secret"
        )

    monkeypatch.setattr(cli_module.cli.commands[command], "callback", fail)
    result = CliRunner().invoke(
        cli_module.cli, [command], env={"SMARTMEMORY_DEBUG": "0"}
    )
    assert result.exit_code == 1
    assert "ReadTimeout: download failed" in result.output
    assert "Traceback" not in result.output
    assert "smartmemory report --zip" in result.output
    log = (tmp_path / "cli-debug.log").read_text()
    assert "Traceback (most recent call last)" in log
    assert "test_support_diagnostics.py" in log and "in fail" in log
    assert command in log and "command=" in log
    assert "python=" in log and "wrapper=" in log
    assert KEY not in log + result.output
    assert "signature=secret" not in log + result.output


def test_debug_reraises_and_still_records_crash(monkeypatch, tmp_path):
    def fail(**kwargs):
        raise RuntimeError("debug failure")

    monkeypatch.setattr(cli_module.cli.commands["start"], "callback", fail)
    result = CliRunner().invoke(
        cli_module.cli, ["start"], env={"SMARTMEMORY_DEBUG": "1"}
    )
    assert isinstance(result.exception, RuntimeError)
    assert "debug failure" in (tmp_path / "cli-debug.log").read_text()


@pytest.mark.parametrize(
    "failure",
    [
        click.ClickException("usual error"),
        click.UsageError("usage"),
        click.Abort(),
        SystemExit(7),
    ],
)
def test_click_control_flow_is_preserved(monkeypatch, tmp_path, failure):
    def fail(**kwargs):
        raise failure

    monkeypatch.setattr(cli_module.cli.commands["start"], "callback", fail)
    result = CliRunner().invoke(cli_module.cli, ["start"])
    assert result.exit_code == (
        7
        if isinstance(failure, SystemExit)
        else 2
        if isinstance(failure, click.UsageError)
        else 1
    )
    assert "Uncaught CLI failure" not in (tmp_path / "cli-debug.log").read_text()


def test_failed_logging_does_not_replace_command_failure(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("original failure")

    def broken_path():
        raise OSError("disk unavailable")

    monkeypatch.setattr(cli_module.cli.commands["start"], "callback", fail)
    monkeypatch.setattr(bug_report, "debug_log_path", broken_path)
    result = CliRunner().invoke(cli_module.cli, ["start"])
    assert "RuntimeError: original failure" in result.output


def test_bundle_contains_redacted_evidence(monkeypatch, tmp_path):
    from smartmemory_app import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = config.config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f'[remote]\napi_key = "{KEY}"\napi_url = "https://user:password@example.test/?signed=value"\n'
    )
    for name in ("cli-debug.log", "daemon.log"):
        (tmp_path / name).write_text(f"Traceback detail {KEY}\n")
    target = tmp_path / "Windows path with spaces" / "support.zip"
    result = CliRunner().invoke(cli_module.cli, ["report", "--zip", str(target)])
    assert result.exit_code == 0, result.output
    assert str(target) in result.output
    assert "Email this file to support@smartmemory.ai." in result.output
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {
            "doctor.txt",
            "environment.txt",
            "config.txt",
            "cli-debug.log",
            "daemon.log",
        }
        texts = {name: archive.read(name).decode() for name in archive.namelist()}
    assert "Network huggingface.co: reachable" in texts["doctor.txt"]
    assert "Traceback detail" in texts["cli-debug.log"]
    assert "Traceback detail" in texts["daemon.log"]
    all_text = "\n".join(texts.values())
    assert KEY not in all_text
    assert "user:password" not in all_text and "signed=value" not in all_text
    assert "https://<custom-host>" in texts["config.txt"]
    assert "example.test" not in texts["config.txt"]


@pytest.mark.parametrize("timeout", [False, True])
def test_doctor_network_results_are_warnings(monkeypatch, timeout):
    def probe(host):
        if timeout:
            raise httpx.ReadTimeout(f"slow network {KEY}")
        return "reachable (3 ms, HTTP 200)"

    monkeypatch.setattr(support, "_probe", probe)
    monkeypatch.setattr(
        cli_module,
        "check_installation",
        lambda: SimpleNamespace(
            python_version=(3, 12, 0),
            python_ok=True,
            core_version="1.5.16",
            core_ok=True,
            socks_proxy_configured=False,
            doctor_ok=True,
        ),
    )
    result = CliRunner().invoke(cli_module.cli, ["doctor"])
    assert result.exit_code == 0
    for host in ("huggingface.co", "pypi.org"):
        assert f"Network {host}:" in result.output
    assert ("ReadTimeout" if timeout else "3 ms") in result.output
    assert KEY not in result.output


def test_network_wall_deadline(monkeypatch):
    completed = threading.Event()

    def probe(host):
        completed.wait(1)
        return "late"

    monkeypatch.setattr(support, "_probe", probe)
    try:
        started = time.monotonic()
        rows = support.network_checks(budget=0.02)
        assert time.monotonic() - started < 0.3
        assert all("timed out" in row for row in rows if "skipped:" not in row)
    finally:
        completed.set()


def test_proxy_environment_and_registry_host_only(monkeypatch):
    for name in support.PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "HTTPS_PROXY", "http://user:password@env.test:8080/private?key=value"
    )
    monkeypatch.setattr(
        support.urllib.request,
        "getproxies",
        lambda: {"https": "http://u:p@registry.test:1234/path"},
    )
    text = support.proxy_summary()
    assert "env HTTPS_PROXY=http://env.test" in text
    assert "system https=http://registry.test" in text
    assert "password" not in text and "private" not in text and "u:p" not in text


def test_download_timeout_original_trace_and_guidance(monkeypatch, tmp_path):
    import huggingface_hub

    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    cli_module._configure_cli_logging()

    def download(*args, **kwargs):
        raise httpx.ReadTimeout(f"download timeout {KEY}")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    with pytest.raises(click.ClickException) as failure:
        setup._ensure_embedding_model("local")
    assert "HF_HUB_DOWNLOAD_TIMEOUT" in str(failure.value)
    assert "HF_ENDPOINT" in str(failure.value)
    text = (tmp_path / "cli-debug.log").read_text()
    assert "ReadTimeout" in text and "Traceback" in text
    assert (
        "Model preparation start:" in text and "cache=" in text and "backend=" in text
    )
    assert KEY not in text + str(failure.value)


def test_daemon_hooks_record_redacted_process_and_thread_tracebacks(tmp_path):
    install_daemon_diagnostics(tmp_path)
    try:
        raise RuntimeError(f"background failure {KEY}")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
        threading.excepthook(
            SimpleNamespace(
                exc_type=type(exc), exc_value=exc, exc_traceback=exc.__traceback__
            )
        )
    text = (tmp_path / "daemon.log").read_text()
    assert text.count("Uncaught daemon failure") == 2
    assert "Traceback" in text and "RuntimeError: background failure" in text
    assert KEY not in text


def test_redacts_command_flags_and_hub_tokens():
    from smartmemory_app.diagnostics import redact_credentials

    text = redact_credentials(
        "--api-key secret-value --token=other-value hf_" + "a" * 30
    )
    assert (
        "secret-value" not in text
        and "other-value" not in text
        and "hf_" + "a" * 30 not in text
    )


def test_raw_daemon_output_is_redacted_before_write():
    import io
    from smartmemory_app.runtime_diagnostics import RedactingStream

    output = io.StringIO()
    stream = RedactingStream(output)
    stream.write("API_KEY=")
    stream.write("split-secret\n")
    stream.write(f"download failed {KEY}\n")
    stream.flush()
    assert "split-secret" not in output.getvalue()
    assert KEY not in output.getvalue()
    assert "download failed" in output.getvalue()


def test_cache_checks_both_default_backends(monkeypatch, tmp_path):
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    assert "missing or incomplete" in support._cache_status()
    for model in support.DEFAULT_MODELS:
        snapshot = (
            tmp_path
            / ("models--" + model.replace("/", "--"))
            / "snapshots"
            / "test_support_revision"
        )
        (snapshot / "onnx").mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
        (snapshot / "model.safetensors").touch()
        (snapshot / "onnx/model.onnx").touch()
        (snapshot / "tokenizer.json").write_text("{}")
    text = support._cache_status()
    assert text.count("present (torch config and weights)") == 2
    assert "Default embedding ONNX files: present" in text


@pytest.mark.parametrize("health", ["ok", "warming", "degraded", None])
def test_daemon_health_check(monkeypatch, tmp_path, health):
    from smartmemory_app import daemon

    # Use the real checker saved before the offline suite fixture runs.
    real_status = _REAL_DAEMON_STATUS

    def probe(limit):
        assert limit == 1.0
        if health is None:
            raise httpx.ReadTimeout("local health timeout")
        return httpx.Response(
            200,
            json={"status": health},
            request=httpx.Request("GET", "http://localhost/health"),
        )

    monkeypatch.setattr(daemon, "_health_response", probe)
    monkeypatch.setattr(daemon, "_pid_file", lambda: tmp_path / "daemon.pid")
    text = real_status()
    assert (f"health={health}" if health else "unhealthy or stopped") in text


def test_bundle_survives_unreadable_log(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bug_report,
        "read_log_tail",
        lambda path, **kwargs: (_ for _ in ()).throw(PermissionError("blocked")),
    )
    monkeypatch.setattr(
        "smartmemory_app.config.config_path", lambda: tmp_path / "absent.toml"
    )
    path = support.write_support_bundle(tmp_path / "support.zip", "doctor result")
    with zipfile.ZipFile(path) as archive:
        assert "PermissionError" in archive.read("cli-debug.log").decode()
        assert "PermissionError" in archive.read("daemon.log").decode()


def test_crash_command_line_redacts_arbitrary_key(monkeypatch, tmp_path):
    def fail(**kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(cli_module.cli.commands["setup"], "callback", fail)
    result = CliRunner().invoke(
        cli_module.cli, ["setup", "--api-key", "arbitrary-secret-value"]
    )
    assert result.exit_code == 1
    text = (tmp_path / "cli-debug.log").read_text()
    assert "--api-key=<redacted>" in text
    assert "arbitrary-secret-value" not in text


def test_uncaught_background_thread_uses_daemon_hook(tmp_path):
    install_daemon_diagnostics(tmp_path)

    def fail():
        raise RuntimeError(f"worker thread failed {KEY}")

    thread = threading.Thread(target=fail, name="test_support_worker")
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    text = (tmp_path / "daemon.log").read_text()
    assert "Traceback" in text and "RuntimeError: worker thread failed" in text
    assert KEY not in text


def test_warmup_handled_failure_keeps_traceback(monkeypatch, tmp_path):
    from smartmemory_app import viewer_server

    for name in ("_startup_status", "_startup_reason", "_last_warmup_failure"):
        monkeypatch.setattr(viewer_server, name, getattr(viewer_server, name))
    install_daemon_diagnostics(tmp_path)

    def fail():
        raise RuntimeError(f"model startup failure {KEY}")

    monkeypatch.setattr(viewer_server, "_warm_backend", fail)
    thread = viewer_server._start_background_warmup()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert viewer_server._get_startup_state()[0] == "degraded"
    text = (tmp_path / "daemon.log").read_text()
    assert "Background startup failed" in text and "Traceback" in text
    assert KEY not in text
