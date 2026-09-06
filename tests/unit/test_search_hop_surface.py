"""Enumerating guard: every explicit core search kwarg reaches the engine."""

import inspect
from unittest.mock import Mock, patch
from click.testing import CliRunner
from smartmemory import SmartMemory
from smartmemory_app import storage
from smartmemory_app.cli import cli


def test_enumerating_core_search_allowlist():
    explicit = {
        name
        for name, param in inspect.signature(SmartMemory.search).parameters.items()
        if name != "self"
        and param.kind not in (param.VAR_KEYWORD, param.VAR_POSITIONAL)
    }
    bound_by_storage = {"query", "top_k", "memory_type"}
    assert explicit - bound_by_storage <= storage._ALLOWED, (
        explicit - bound_by_storage - storage._ALLOWED
    )
    memory = Mock()
    memory.search.return_value = []
    with patch.object(storage, "get_memory", return_value=memory):
        for key in explicit - bound_by_storage:
            marker = object()
            storage.search("bridge question", **{key: marker, "future_unknown": "drop"})
            assert memory.search.call_args.kwargs[key] is marker, key
            assert "future_unknown" not in memory.search.call_args.kwargs


def test_cli_hop_flags_both_paths_and_invalid_combinations():
    for response in ({"items": []}, None):
        with (
            patch(
                "smartmemory_app.cli._daemon_request", return_value=response
            ) as daemon,
            patch.object(storage, "search", return_value=[]) as search,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "search",
                    "bridge",
                    "--multi-hop",
                    "--max-hops",
                    "2",
                    "--hop-strategy",
                    "relevance",
                ],
            )
            assert result.exit_code == 0, result.output
            assert daemon.call_args.kwargs["json"]["hop_strategy"] == "relevance"
            if response is None:
                assert search.call_args.kwargs["hop_strategy"] == "relevance"
                assert search.call_args.kwargs["multi_hop"] is True
                assert search.call_args.kwargs["max_hops"] == 2
    result = CliRunner().invoke(
        cli, ["search", "bridge", "--hop-strategy", "relevance"]
    )
    assert result.exit_code != 0 and "requires --multi-hop" in result.output
    assert (
        CliRunner().invoke(cli, ["search", "bridge", "--max-hops", "11"]).exit_code != 0
    )


def test_local_daemon_forwards_hop_options():
    from smartmemory_app.local_api import SearchRequest, search_endpoint

    with patch.object(storage, "search", return_value=[]) as search:
        search_endpoint(
            SearchRequest(
                query="bridge", multi_hop=True, max_hops=2, hop_strategy="relevance"
            )
        )
    assert search.call_args.kwargs["hop_strategy"] == "relevance"
    assert search.call_args.kwargs["max_hops"] == 2
