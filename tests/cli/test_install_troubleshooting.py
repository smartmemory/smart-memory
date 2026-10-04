"""Handled installation failures keep Click semantics and anonymous evidence."""

import json
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_mod, crash_reporter


@pytest.mark.parametrize("standalone", [True, False])
def test_setup_handled_failure_reports_once(monkeypatch, tmp_path, standalone):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    secret = "wrong-install-key-123456789"

    def fail(**kwargs):
        raise click.ClickException(f"Setup key validation failed: {secret}")

    monkeypatch.setattr(cli_mod.cli.commands["setup"], "callback", fail)
    with patch.object(crash_reporter, "_post", return_value=True) as post:
        first = CliRunner().invoke(
            cli_mod.cli, ["setup", "--api-key", secret], standalone_mode=standalone
        )
        second = CliRunner().invoke(cli_mod.cli, ["setup", "--api-key", secret])
    assert first.exit_code == second.exit_code == 1
    assert post.call_count == 1
    payload = post.call_args.args[0]
    assert payload["properties"]["$exception_list"][0]["mechanism"]["handled"]
    assert payload["properties"]["source"] == "install"
    assert "doctor" in payload["properties"]
    assert secret not in json.dumps(payload)
    assert "Report ID " in first.output
    assert "Setup key validation failed" in first.output or str(first.exception)


@pytest.mark.parametrize(
    "command,error",
    [
        ("setup", click.Abort()),
        ("setup", click.UsageError("bad flag")),
        ("search", click.ClickException("ordinary user error")),
    ],
)
def test_user_abort_usage_and_non_install_errors_never_report(
    monkeypatch, command, error
):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(cli_mod.cli.commands[command], "callback", fail)
    with patch.object(crash_reporter, "_post") as post:
        result = CliRunner().invoke(
            cli_mod.cli, [command] + (["query"] if command == "search" else [])
        )
    assert result.exit_code != 0
    post.assert_not_called()


def test_opt_out_still_guides_setup_error(monkeypatch):
    def fail(**kwargs):
        raise click.ClickException("setup failed")

    monkeypatch.setattr(cli_mod.cli.commands["setup"], "callback", fail)
    with patch.object(crash_reporter, "_post") as post:
        result = CliRunner().invoke(cli_mod.cli, ["setup"])
    assert "setup failed" in result.output
    assert "Automatic crash reporting disabled" in result.output
    assert "Fix:" in result.output
    post.assert_not_called()


def test_hook_missing_command_marker_and_consumed_once(monkeypatch, tmp_path):
    import os
    import subprocess
    from pathlib import Path
    from smartmemory_app import hook_failures

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "profile"))
    marker_dir = tmp_path / "profile" / ".smartmemory"
    env = os.environ.copy()
    # Empty PATH for smartmemory, but provide the shell utilities used by the hook.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mkdir", "cat"):
        import shutil

        (bindir / name).symlink_to(shutil.which(name))
    env["PATH"] = str(bindir)
    hook = Path(cli_mod.__file__).parent / "hooks/recall.sh"
    result = subprocess.run(
        ["/bin/bash", str(hook)], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 0
    assert "recall\t127\t" in (marker_dir / "hook-failures.tsv").read_text()
    with patch.object(crash_reporter, "_post", return_value=True) as post:
        hook_failures.consume_failures(marker_dir)
        hook_failures.consume_failures(marker_dir)
        with (marker_dir / "hook-failures.tsv").open("a") as f:
            f.write("recall\t127\t0\nrecall\t5\t0\n")
        hook_failures.consume_failures(marker_dir)
    assert post.call_count == 2
    assert "recall exit 127" in " ".join(hook_failures.recent_failures(marker_dir))


def test_auto_doctor_hard_deadline_and_cp1252(monkeypatch):
    import time
    from smartmemory_app import (
        install_troubleshooting as troubleshooting,
        support_diagnostics,
    )

    monkeypatch.setattr(
        support_diagnostics, "local_checks", lambda **kwargs: time.sleep(0.15)
    )
    started = time.monotonic()
    rows = troubleshooting.doctor_summary(timeout=0.01)
    assert time.monotonic() - started < 0.1
    assert "timed out" in rows[0]
    troubleshooting.relevant_hint(RuntimeError("model unavailable"), rows).encode(
        "cp1252"
    )


def test_native_import_failure_is_cached_only_on_success(monkeypatch, tmp_path):
    from smartmemory_app import install_check

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(
        install_check,
        "run_native_checks",
        return_value=[
            "Native usearch: FAIL: OSError DLL load failed. Fix: python -m pip install --force-reinstall usearch"
        ],
    ) as probe:
        with patch.object(crash_reporter, "_post", return_value=True) as post:
            install_check.first_run_check(tmp_path)
            install_check.first_run_check(tmp_path)
        assert probe.call_count == 2
        assert post.call_count == 1
    assert not (tmp_path / ".install-check-pass.json").exists()
    with patch.object(
        install_check, "run_native_checks", return_value=["Native usearch: OK"]
    ) as probe:
        install_check.first_run_check(tmp_path)
        install_check.first_run_check(tmp_path)
        assert probe.call_count == 1
    assert (tmp_path / ".install-check-pass.json").is_file()


def test_python_support_window_matches_package_metadata():
    import tomllib
    from pathlib import Path
    from packaging.specifiers import SpecifierSet
    from smartmemory_app.install_check import python_requirement

    expected = tomllib.loads(
        (Path(cli_mod.__file__).parent.parent / "pyproject.toml").read_text()
    )["project"]["requires-python"]
    assert SpecifierSet(python_requirement()) == SpecifierSet(expected)


def test_foreground_warm_failure_does_not_announce_success(monkeypatch):
    from smartmemory.plugins.embedding import EmbeddingService

    monkeypatch.setattr(
        EmbeddingService,
        "warm",
        lambda self: (_ for _ in ()).throw(OSError("DLL load failed")),
    )
    result = CliRunner().invoke(
        cli_mod.cli, ["warm", "--no-reranker"], env={"SMARTMEMORY_MODE": "local"}
    )
    assert result.exit_code == 1
    assert "Model warmup failed" in result.output
    assert "Models warm in" not in result.output
    assert "Fix:" in result.output


def test_windows_and_bare_username_privacy(monkeypatch):
    from smartmemory_app.report_privacy import private_text

    monkeypatch.setenv("USERNAME", "NativeTestUser")
    text = private_text(
        r"DLL failure for NativeTestUser at C:\Users\NativeTestUser\data and C:/Users/OtherPerson/cache /tmp/pytest-of-NativeTestUser/test_F"
    )
    assert "NativeTestUser" not in text and "OtherPerson" not in text
    assert "C:\\Users" not in text and "C:/Users" not in text


def test_missing_model_conversion_is_a_handled_install_report(monkeypatch):
    from smartmemory.errors import MissingModelError

    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")

    def fail(**kwargs):
        raise MissingModelError("configured model is missing")

    monkeypatch.setattr(cli_mod.cli.commands["search"], "callback", fail)
    with patch.object(crash_reporter, "_post", return_value=True) as post:
        result = CliRunner().invoke(cli_mod.cli, ["search", "query"])
    assert result.exit_code == 1
    assert post.call_count == 1, result.output
    properties = post.call_args.args[0]["properties"]
    assert properties["source"] == "install"
    assert properties["$exception_list"][0]["mechanism"]["handled"]
    assert "configured model is missing" in result.output
    assert "Report ID " in result.output


@pytest.mark.parametrize("command", ["warm", "clear"])
def test_local_mode_usage_guard_is_not_an_install_report(monkeypatch, command):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(crash_reporter, "_post") as post:
        result = CliRunner().invoke(
            cli_mod.cli, [command], env={"SMARTMEMORY_MODE": "remote"}
        )
    assert result.exit_code == 1
    assert "only available in local mode" in result.output
    post.assert_not_called()
