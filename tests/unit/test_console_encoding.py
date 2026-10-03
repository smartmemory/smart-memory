"""Exercise actual legacy TextIOWrapper streams and Click output."""

import io
import sys

import click
import pytest

from smartmemory_app.console import configure_console_encoding


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_legacy_streams_print_unicode_without_errors(monkeypatch, encoding):
    out, err = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(out, encoding=encoding)
    stderr = io.TextIOWrapper(err, encoding=encoding)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    configure_console_encoding()
    assert stdout.encoding == stderr.encoding == "utf-8"
    assert stdout.errors == stderr.errors == "replace"
    click.echo("✓ … — 中文")
    click.echo("✓ … — 中文", err=True)
    assert out.getvalue().decode("utf-8") == "✓ … — 中文\n"
    assert err.getvalue().decode("utf-8") == "✓ … — 中文\n"
    click.echo("unpaired surrogate: \ud800")
    assert out.getvalue().decode("utf-8").endswith("unpaired surrogate: ?\n")


def test_utf8_stream_is_untouched(monkeypatch):
    class UTF8Stream(io.TextIOWrapper):
        def reconfigure(self, **kwargs):
            pytest.fail("UTF-8 streams must remain untouched")

    stream = UTF8Stream(io.BytesIO(), encoding="UTF-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    configure_console_encoding()
    assert stream.errors == "strict"


def test_unreconfigurable_streams_never_raise(monkeypatch):
    class BrokenStream:
        encoding = "cp1252"

        def reconfigure(self, **kwargs):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", BrokenStream())
    configure_console_encoding()


@pytest.mark.parametrize("entry", ["cli", "lifecycle"])
def test_entry_point_reconfigures_before_unicode_output(monkeypatch, entry):
    from smartmemory_app.cli import cli, lifecycle_group

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr("smartmemory_app.cli._configure_cli_logging", lambda: None)
    command = cli if entry == "cli" else lifecycle_group
    # Invoke the production callback itself, which also covers direct embedded
    # invocation that bypasses the top-level group's main().
    command.callback()
    click.echo("✓ … — recalled 中文")
    assert raw.getvalue().decode("utf-8") == "✓ … — recalled 中文\n"


def test_group_main_configures_before_click_help(monkeypatch):
    from smartmemory_app.cli import _CLIGroup

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    group = _CLIGroup("test", help="✓ … — 中文")
    group.main(args=["--help"], standalone_mode=False)
    assert "✓ … — 中文" in raw.getvalue().decode("utf-8")
