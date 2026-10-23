"""Property flags parse in any position on every property-taking command (CLI-SEARCH-PROP-ORDER-1)."""

import pytest
from click.testing import CliRunner

from smartmemory_app.cli import _parse_props, add_cmd, cli, search_cmd

ATLAS = {"project": "atlas"}

# (command, args, positional param, expected positional, known-option params, expected props)
# Rows cover every property position against the positional, with known options mixed in.
ORDER_ROWS = [
    (search_cmd, ["--project", "atlas", "Q"], "query", "Q", {}, ATLAS),
    (search_cmd, ["--project=atlas", "Q"], "query", "Q", {}, ATLAS),
    (search_cmd, ["Q", "--project", "atlas"], "query", "Q", {}, ATLAS),
    (search_cmd, ["Q", "--project=atlas"], "query", "Q", {}, ATLAS),
    (
        search_cmd,
        ["--top-k", "3", "--project", "atlas", "Q"],
        "query",
        "Q",
        {"top_k": 3},
        ATLAS,
    ),
    (
        search_cmd,
        ["--project", "atlas", "--top-k", "2", "Q"],
        "query",
        "Q",
        {"top_k": 2},
        ATLAS,
    ),
    (
        search_cmd,
        ["--project", "atlas", "--since", "7d", "Q"],
        "query",
        "Q",
        {"since": "7d"},
        ATLAS,
    ),
    (
        search_cmd,
        ["--project", "atlas", "--domain", "legal", "Q"],
        "query",
        "Q",
        {},
        {"project": "atlas", "domain": "legal"},
    ),
    (search_cmd, ["--prop", "project=atlas", "Q"], "query", "Q", {}, ATLAS),
    (search_cmd, ["--project=atlas", "--", "--Q"], "query", "--Q", {}, ATLAS),
    (add_cmd, ["--project", "atlas", "T"], "text", "T", {}, ATLAS),
    (add_cmd, ["--project=atlas", "T"], "text", "T", {}, ATLAS),
    (add_cmd, ["T", "--project", "atlas"], "text", "T", {}, ATLAS),
    (add_cmd, ["T", "--project=atlas"], "text", "T", {}, ATLAS),
    (
        add_cmd,
        ["--type=semantic", "--project", "atlas", "T"],
        "text",
        "T",
        {"memory_type": "semantic"},
        ATLAS,
    ),
    (add_cmd, ["--project=atlas", "--", "--T"], "text", "--T", {}, ATLAS),
]


@pytest.mark.parametrize(
    "row", ORDER_ROWS, ids=lambda row: f"{row[0].name}:{' '.join(row[1])}"
)
def test_property_position(row):
    command, args, positional, expected, known, props = row
    with command.make_context(command.name, args) as context:
        assert context.params[positional] == expected
        for name, value in known.items():
            assert context.params[name] == value
        assert _parse_props(context.params["explicit_props"]) == props


# Failures: each must be refused before any transport (the run never reaches a daemon).
FAILURE_ROWS = [
    ("search", ["Q", "--project"], "requires a value"),
    ("search", ["--project"], "requires a value"),
    ("search", ["--project", "--top-k", "2", "Q"], "requires a value"),
    ("search", ["Q", "--project="], "non-empty"),
    ("search", ["Q", "--project=a", "--prop", "project=b"], "duplicate property"),
    ("search", ["two", "words"], "Quote the query"),
    ("add", ["two", "words"], "Quote the text"),
    ("add", ["T", "--project"], "requires a value"),
]


@pytest.mark.parametrize(
    "row", FAILURE_ROWS, ids=lambda row: f"{row[0]}:{' '.join(row[1])}"
)
def test_failures(row):
    name, args, message = row
    result = CliRunner().invoke(cli, [name, *args])
    assert result.exit_code != 0
    assert message in result.output


@pytest.mark.parametrize("name", ["add", "search"])
def test_help_shows_flag_first_example(name):
    result = CliRunner().invoke(cli, [name, "--help"])
    assert result.exit_code == 0
    assert f"smartmemory {name} --project atlas" in " ".join(result.output.split())
