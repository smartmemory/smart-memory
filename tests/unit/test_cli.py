"""Tests for the Click CLI commands.

DIST-DAEMON-1: CLI commands try daemon HTTP API first (_daemon_request), then
fall back to storage.ingest/search/recall via lazy import inside function bodies.
Patches target storage module functions (the fallback path) and _daemon_request
(to simulate daemon-down).
"""

import pytest
from click.testing import CliRunner
from unittest.mock import patch


@pytest.fixture
def runner():
    return CliRunner()


def test_persist_cmd_fallback(runner):
    """add command falls back to storage.ingest when daemon is down."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.ingest", return_value="item-abc") as mock_ingest,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["add", "test memory text"])

    assert result.exit_code == 0
    assert "item-abc" in result.output
    mock_ingest.assert_called_once_with("test memory text", "episodic", properties={}, origin="cli:add")


def test_add_cmd_daemon_path(runner):
    """add command uses daemon response when available."""
    with patch("smartmemory_app.cli._daemon_request", return_value={"item_id": "daemon-id"}):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["add", "test memory text"])

    assert result.exit_code == 0
    assert "daemon-id" in result.output


def test_recall_cmd_fallback(runner):
    """recall command falls back to storage.recall when daemon is down."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.recall", return_value="## SmartMemory Context\n- hello") as mock_recall,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["recall", "--cwd", "/my/project"])

    assert result.exit_code == 0
    assert "SmartMemory" in result.output
    mock_recall.assert_called_once_with(
        "/my/project", 10, query=None, workspace_id=None, include_snapshot=True, strict=False
    )


def test_recall_cmd_no_cwd_fallback(runner):
    """recall command passes None for cwd when --cwd not provided (fallback path)."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.recall", return_value="") as mock_recall,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["recall"])

    assert result.exit_code == 0
    mock_recall.assert_called_once_with(
        None, 10, query=None, workspace_id=None, include_snapshot=True, strict=False
    )


def test_recall_cmd_daemon_path(runner):
    """recall command uses daemon response when available."""
    with patch("smartmemory_app.cli._daemon_request", return_value={"context": "daemon context"}):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["recall"])

    assert result.exit_code == 0
    assert "daemon context" in result.output


def test_search_cmd_fallback(runner):
    """search command falls back to storage.search when daemon is down."""
    mock_results = [{"item_id": "abc12345", "content": "test result", "memory_type": "semantic"}]
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.search", return_value=mock_results) as mock_search,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["search", "test query"])

    assert result.exit_code == 0
    assert "abc12345" in result.output
    mock_search.assert_called_once_with("test query", 5, filters={}, include_reference=False)


def test_search_cmd_no_results(runner):
    """search command prints 'No results.' when empty."""
    with patch("smartmemory_app.cli._daemon_request", return_value=[]):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["search", "nonexistent"])

    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_cmd_daemon_items_contract(runner):
    """Daemon returns the CORE-CRUD-LIST contract shape {'items': [...]}.
    The CLI must unwrap it, not iterate the dict directly (regression:
    `for r in results` over the dict yielded the key 'items' -> str ->
    AttributeError: 'str' object has no attribute 'get')."""
    payload = {"items": [
        {"item_id": "deadbeef-0000", "content": "Acme Corp in Berlin", "memory_type": "episodic"},
    ]}
    with patch("smartmemory_app.cli._daemon_request", return_value=payload):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["search", "berlin"])

    assert result.exit_code == 0, result.output
    assert "deadbeef" in result.output
    assert "Acme Corp in Berlin" in result.output


def test_get_cmd_fallback(runner):
    """get command falls back to storage.get when daemon is unavailable."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.get", return_value={"item_id": "abc", "content": "hello", "memory_type": "episodic"}) as mock_get,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["get", "abc"])

    assert result.exit_code == 0
    assert '"item_id": "abc"' in result.output
    mock_get.assert_called_once_with("abc")


def test_get_cmd_daemon_path(runner):
    """get command prints daemon response when available."""
    with patch("smartmemory_app.cli._daemon_request", return_value={"item_id": "daemon-id", "content": "from daemon"}):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["get", "daemon-id"])

    assert result.exit_code == 0
    assert '"item_id": "daemon-id"' in result.output


def test_get_cmd_not_found_exits_nonzero(runner):
    """get command exits with an error when the memory is missing."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.get", return_value={}),
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["get", "missing-id"])

    assert result.exit_code == 1
    assert "Memory not found" in result.output


def test_search_cmd_filters_unsupported_exits_nonzero(runner):
    """search with filters raises ClickException when storage raises NotImplementedError."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.search", side_effect=NotImplementedError("filters not supported")),
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["search", "test", "--project", "atlas"])

    assert result.exit_code != 0
    assert "filters not supported" in result.output


def test_get_cmd_daemon_http_error_surfaces(runner):
    """get command surfaces ClickException from daemon HTTP errors instead of swallowing it."""
    import click

    with patch("smartmemory_app.cli._daemon_request", side_effect=click.ClickException("501: Not Implemented")):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["get", "some-id"])

    assert result.exit_code != 0
    assert "Not Implemented" in result.output


def test_add_cmd_with_properties(runner):
    """add command passes extra --key value flags as properties."""
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.ingest", return_value="prop-id") as mock_ingest,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["add", "test text", "--project", "atlas", "--domain", "legal"])

    assert result.exit_code == 0
    assert "prop-id" in result.output
    mock_ingest.assert_called_once_with(
        "test text", "episodic", properties={"project": "atlas", "domain": "legal"}, origin="cli:add"
    )


def test_search_cmd_with_filters(runner):
    """search command passes extra --key value flags as filters."""
    mock_results = [{"item_id": "abc12345", "content": "filtered result", "memory_type": "semantic"}]
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage.search", return_value=mock_results) as mock_search,
    ):
        from smartmemory_app.cli import cli
        result = runner.invoke(cli, ["search", "test", "--project", "atlas"])

    assert result.exit_code == 0
    assert "abc12345" in result.output
    mock_search.assert_called_once_with("test", 5, filters={"project": "atlas"}, include_reference=False)


class TestCliLoggingPolicy:
    """DIST-CLI-QUIET-1: the CLI installs a root logging policy on every invocation.

    Default WARNING keeps pipeline INFO chatter out of user-facing commands and
    neutralizes import-time logging.basicConfig() in deps (fastcoref). Warnings
    stay visible (no-silent-degradation). SMARTMEMORY_LOG_LEVEL overrides.
    """

    @pytest.fixture(autouse=True)
    def _restore_root_logger(self):
        """Snapshot/restore the root logger so these tests don't leak global state."""
        import logging

        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        yield
        root.handlers[:] = handlers
        root.setLevel(level)

    def _reset_root(self):
        import logging

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.setLevel(logging.WARNING)

    def test_default_level_is_warning(self, runner, monkeypatch):
        import logging

        monkeypatch.delenv("SMARTMEMORY_LOG_LEVEL", raising=False)
        self._reset_root()
        with patch("smartmemory_app.cli._daemon_request", return_value={"item_id": "x"}):
            from smartmemory_app.cli import cli
            result = runner.invoke(cli, ["add", "t"])

        assert result.exit_code == 0
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert root.handlers, "CLI must install a root handler (neutralizes dep basicConfig)"

    def test_env_override_raises_verbosity(self, runner, monkeypatch):
        import logging

        monkeypatch.setenv("SMARTMEMORY_LOG_LEVEL", "DEBUG")
        self._reset_root()
        with patch("smartmemory_app.cli._daemon_request", return_value={"item_id": "x"}):
            from smartmemory_app.cli import cli
            result = runner.invoke(cli, ["add", "t"])

        assert result.exit_code == 0
        assert logging.getLogger().level == logging.DEBUG

    def test_debug_command_emits_bounded_redacted_wire_trace(
        self, runner, monkeypatch
    ):
        import httpx

        import smartmemory_app.cli as cli_module

        class FakeClient:
            def __init__(self, **kwargs):
                assert kwargs == {"trust_env": False}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def request(self, method, url, **kwargs):
                return httpx.Response(
                    200,
                    json={
                        "item_id": "daemon-id",
                        "access_token": "response-token-must-not-leak",
                        "details": "x" * 5000,
                        "tail": "response-tail-must-be-truncated",
                    },
                    request=httpx.Request(method, url),
                )

        monkeypatch.setenv("SMARTMEMORY_LOG_LEVEL", "DEBUG")
        self._reset_root()
        with (
            patch.object(
                cli_module, "_daemon_url", return_value="http://127.0.0.1:9876"
            ),
            patch("httpx.Client", FakeClient),
        ):
            result = runner.invoke(
                cli_module.cli,
                [
                    "add",
                    "diagnostic payload",
                    "--Authorization",
                    "Bearer request-token-must-not-leak",
                ],
            )

        assert result.exit_code == 0, result.output
        # This Click version merges stderr into Result.output; the production
        # logger still uses its default stderr stream.
        assert result.output.endswith("daemon-id\n")
        trace = result.output
        assert trace.count("startup diagnostics:") == 1
        assert "smartmemory=" in trace
        assert "smartmemory-core=" in trace
        assert "python=" in trace
        assert "platform=" in trace
        assert "daemon_url=http://127.0.0.1:9876" in trace
        assert "daemon request: method=POST" in trace
        assert "url=http://127.0.0.1:9876/memory/ingest" in trace
        assert "daemon response: status=200" in trace
        assert "latency_ms=" in trace
        assert "<redacted>" in trace
        assert "<truncated" in trace
        assert "request-token-must-not-leak" not in trace
        assert "response-token-must-not-leak" not in trace
        assert "response-tail-must-be-truncated" not in trace

    def test_daemon_request_redacts_authorization_header(self, monkeypatch, caplog):
        import logging

        import httpx

        import smartmemory_app.cli as cli_module

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def request(self, method, url, **kwargs):
                return httpx.Response(
                    200,
                    json={"ok": True},
                    request=httpx.Request(method, url),
                )

        caplog.set_level(logging.DEBUG, logger="smartmemory_app.cli")
        monkeypatch.setattr(cli_module, "_daemon_url", lambda: "http://daemon.test")
        with patch("httpx.Client", FakeClient):
            assert cli_module._daemon_request(
                "GET",
                "/memory/test",
                headers={
                    "Authorization": "Bearer literal-authorization-token",
                    "X-Api-Key": "literal-api-key",
                },
            ) == {"ok": True}

        assert "method=GET" in caplog.text
        assert "url=http://daemon.test/memory/test" in caplog.text
        assert "<redacted>" in caplog.text
        assert "literal-authorization-token" not in caplog.text
        assert "literal-api-key" not in caplog.text

    def test_debug_command_names_in_process_fallback(self, runner, monkeypatch):
        import smartmemory_app.cli as cli_module

        monkeypatch.setenv("SMARTMEMORY_LOG_LEVEL", "DEBUG")
        self._reset_root()
        with (
            patch.object(cli_module, "_daemon_url", return_value="http://daemon.test"),
            patch.object(cli_module, "_daemon_request", return_value=None),
            patch("smartmemory_app.storage.ingest", return_value="fallback-id"),
        ):
            result = runner.invoke(cli_module.cli, ["add", "fallback payload"])

        assert result.exit_code == 0, result.output
        assert (
            "daemon unreachable; using in-process fallback: ingest" in result.output
        )

    def test_invalid_env_falls_back_to_warning(self, runner, monkeypatch):
        import logging

        monkeypatch.setenv("SMARTMEMORY_LOG_LEVEL", "not-a-level")
        self._reset_root()
        with patch("smartmemory_app.cli._daemon_request", return_value={"item_id": "x"}):
            from smartmemory_app.cli import cli
            result = runner.invoke(cli, ["add", "t"])

        assert result.exit_code == 0
        assert logging.getLogger().level == logging.WARNING
