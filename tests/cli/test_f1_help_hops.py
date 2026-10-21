"""User-visible help, truncation and planner degradation."""

import json
import logging
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

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


@pytest.mark.parametrize("keyed", [False, True])
def test_semantic_keyless_notice_logged_and_visible(monkeypatch, caplog, keyed):
    """Direct execution uses the key state of the CLI process after success."""
    from smartmemory_app.config import LLM_KEY_ENV_VARS

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    for name in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("OPENAI_API_KEY", "test_F1b_unused_key" if keyed else "")
    monkeypatch.setattr(cli, "_memory_request", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kw: None)
    monkeypatch.setattr("smartmemory_app.storage.search", lambda *a, **kw: [])
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
        )
    assert result.exit_code == 0, result.output
    notice = "semantic hop planning needs an LLM key; this search used the heuristic planner."
    assert (f"Note: {notice}" in result.output) is (not keyed)
    assert (notice in caplog.text) is (not keyed)


@contextmanager
def semantic_daemon(keyed, refused=False):
    """Serve the daemon health/search contract over real localhost HTTP."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            requests.append(self.path)
            assert self.path == "/health"
            self.reply(
                200,
                {"service": "smartmemory", "status": "ok", "llm_key_present": keyed},
            )

        def do_POST(self):
            requests.append(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/memory/search"
            assert body["multi_hop"] is True and body["hop_strategy"] == "semantic"
            self.reply(
                400, {"detail": "test_F1b search refused"}
            ) if refused else self.reply(200, {"items": []})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "cli_keyed,daemon_keyed",
    [(False, True), (True, False), (False, False), (True, True)],
)
def test_semantic_notice_uses_daemon_health(
    monkeypatch, caplog, cli_keyed, daemon_keyed
):
    from smartmemory_app.config import LLM_KEY_ENV_VARS

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    for name in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("OPENAI_API_KEY", "test_F1b_unused_key" if cli_keyed else "")
    with semantic_daemon(daemon_keyed) as (port, requests):
        monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(port))
        with caplog.at_level(logging.WARNING):
            result = CliRunner().invoke(
                cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
            )
    assert result.exit_code == 0, result.output
    notice = "semantic hop planning needs an LLM key; this search used the heuristic planner."
    assert (f"Note: {notice}" in result.output) is (not daemon_keyed)
    assert (notice in caplog.text) is (not daemon_keyed)
    assert requests[0] == "/memory/search"
    assert requests[1:] and all(path == "/health" for path in requests[1:])


@pytest.mark.parametrize(
    "options",
    [["--since", "invalid"], ["--prop", "missing-equals"], ["--max-hops", "0"]],
)
def test_invalid_semantic_search_has_no_notice(monkeypatch, caplog, options):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)

    def unexpected_request(*args, **kwargs):
        pytest.fail("Invalid command must refuse before transport")

    monkeypatch.setattr(cli, "_memory_request", unexpected_request)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli,
            ["search", "test", "--multi-hop", "--hop-strategy", "semantic", *options],
        )
    assert result.exit_code != 0
    assert "heuristic planner" not in result.output
    assert "heuristic planner" not in caplog.text


def test_refused_daemon_semantic_search_has_no_notice(monkeypatch, caplog):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)
    with semantic_daemon(False, refused=True) as (port, requests):
        monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(port))
        with caplog.at_level(logging.WARNING):
            result = CliRunner().invoke(
                cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
            )
    assert result.exit_code != 0
    assert "test_F1b search refused" in result.output
    assert "heuristic planner" not in result.output
    assert "heuristic planner" not in caplog.text
    assert requests == ["/memory/search"]


def test_refused_direct_semantic_search_has_no_notice(monkeypatch, caplog):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)
    monkeypatch.setattr(cli, "_memory_request", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kw: None)

    def refuse(*args, **kwargs):
        raise NotImplementedError("test_F1b search refused")

    monkeypatch.setattr("smartmemory_app.storage.search", refuse)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
        )
    assert result.exit_code != 0
    assert "test_F1b search refused" in result.output
    assert "heuristic planner" not in result.output
    assert "heuristic planner" not in caplog.text


@pytest.mark.parametrize("health", [None, {"service": "smartmemory", "status": "ok"}])
def test_semantic_search_missing_daemon_key_status_preserves_results(
    monkeypatch, caplog, health
):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)
    monkeypatch.setattr(
        cli,
        "_memory_request",
        lambda *a, **kw: {"items": [{"content": "test_F1b result"}]},
    )
    monkeypatch.setattr("smartmemory_app.daemon.get_status", lambda: health)
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli, ["search", "test", "--multi-hop", "--hop-strategy", "semantic"]
        )
    assert result.exit_code == 0, result.output
    assert "test_F1b result" in result.output
    assert "daemon LLM availability is unknown" in result.output
    assert "daemon LLM availability is unknown" in caplog.text
    assert "used the heuristic planner" not in result.output
