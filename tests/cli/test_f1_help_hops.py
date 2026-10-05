"""User-visible help, truncation and planner degradation."""

import logging

from click.testing import CliRunner
from smartmemory_app import cli


def test_help_forwards_click_help():
    runner = CliRunner()
    assert (
        runner.invoke(cli.cli, ["help"]).output
        == runner.invoke(cli.cli, ["--help"]).output
    )
    assert (
        runner.invoke(cli.cli, ["help", "add"]).output
        == runner.invoke(cli.cli, ["add", "--help"]).output
    )
    assert runner.invoke(cli.cli, ["help", "unknown"]).exit_code != 0


def test_search_preview_marks_truncation_and_full_keeps_body(monkeypatch):
    content = "x" * 200 + "END-F1"
    monkeypatch.setattr(
        cli,
        "_memory_request",
        lambda *a, **k: {"items": [{"content": content, "item_id": "full-id"}]},
    )
    runner = CliRunner()
    preview = runner.invoke(cli.cli, ["search", "test"])
    assert "x" * 200 + "…" in preview.output and "END-F1" not in preview.output
    whole = runner.invoke(cli.cli, ["search", "test", "--full"])
    assert content in whole.output and "…" not in whole.output


def test_semantic_keyless_notice_logged_and_visible(monkeypatch, caplog):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)
    monkeypatch.setattr(cli, "_memory_request", lambda *a, **k: {"items": []})
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
        )
    assert result.exit_code == 0
    assert "Note: semantic hop planning needs an LLM key" in result.output
    assert "Used the heuristic planner" in caplog.text
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: True)
    result = CliRunner().invoke(
        cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
    )
    assert "Note:" not in result.output
