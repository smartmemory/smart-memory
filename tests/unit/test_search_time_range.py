from unittest.mock import Mock, patch
from click.testing import CliRunner
from smartmemory_app.cli import cli
from smartmemory_app import storage


def test_cli_both_paths_resolve_relative_windows():
    for daemon_result in ({"items": []}, None):
        with (
            patch(
                "smartmemory_app.cli._daemon_request", return_value=daemon_result
            ) as daemon,
            patch.object(storage, "search", return_value=[]) as search,
        ):
            result = CliRunner().invoke(
                cli, ["search", "atlas", "--since", "7d", "--until", "24h"]
            )
            assert result.exit_code == 0, result.output
            body = daemon.call_args.kwargs["json"]
            assert body["since"].endswith("+00:00") and body["since"] < body["until"]
            if daemon_result is None:
                assert search.call_args.kwargs["since"] == body["since"]
                assert search.call_args.kwargs["until"] == body["until"]


def test_storage_wildcard_reaches_core_for_range():
    memory = Mock()
    memory.search.return_value = []
    with patch.object(storage, "get_memory", return_value=memory):
        storage.search("*", since="2026-09-01", until="2026-09-03")
    assert memory.search.call_args.kwargs["since"] == "2026-09-01"
