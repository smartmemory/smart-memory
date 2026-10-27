"""--since/--until accept the forms the help lists and refuse the rest by name (CLI-UNTIL-FORMAT-ERROR-1)."""

import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module
from smartmemory_app.cli import cli

# (args, expected body window or None, expected message in output for a refusal)
# Accepted forms: Z, offset, naive (UTC), date only, space separator quoted and unquoted.
ROWS = [
    (
        ["--until", "2026-10-27T01:25:30Z", "Q"],
        {"until": "2026-10-27T01:25:30+00:00"},
        None,
    ),
    (
        ["--until", "2026-10-26T21:25:30-04:00", "Q"],
        {"until": "2026-10-27T01:25:30+00:00"},
        None,
    ),
    (
        ["--until", "2026-10-26T21:11:00", "Q"],
        {"until": "2026-10-26T21:11:00+00:00"},
        None,
    ),
    (["--until", "2026-10-26", "Q"], {"until": "2026-10-26T00:00:00+00:00"}, None),
    (
        ["--until", "2026-10-26 21:11:00", "Q"],
        {"until": "2026-10-26T21:11:00+00:00"},
        None,
    ),
    (
        ["--until", "2026-10-26", "21:11:00", "Q"],
        {"until": "2026-10-26T21:11:00+00:00"},
        None,
    ),
    (
        ["Q", "--until=2026-10-26", "21:11"],
        {"until": "2026-10-26T21:11:00+00:00"},
        None,
    ),
    (
        ["--since", "2026-10-26", "21:11:00", "Q", "--project", "a"],
        {"since": "2026-10-26T21:11:00+00:00"},
        None,
    ),
    (["--until", "garbage", "Q"], None, "Invalid value for --until: 'garbage'"),
    (["--since", "2026-13-45", "Q"], None, "Invalid value for --since: '2026-13-45'"),
    (["--until", "2026-10-26 25:99", "Q"], None, "Invalid value for --until"),
    (["--since", "1w", "Q"], None, "relative duration (7d, 24h, 30m)"),
    (["--until", "x", "Q"], None, "read as UTC"),
    (
        ["--since", "2026-10-27", "--until", "2026-10-26", "Q"],
        None,
        "--since must not be after --until",
    ),
]


@pytest.mark.parametrize("row", ROWS, ids=lambda row: " ".join(row[0]))
def test_time_option_forms(row, monkeypatch):
    args, window, message = row
    sent = []

    def fake_request(method, path, **kwargs):
        sent.append(kwargs["json"])
        return []

    monkeypatch.setattr(cli_module, "_memory_request", fake_request)
    result = CliRunner().invoke(cli, ["search", *args])
    if message:
        assert result.exit_code == 2, result.output
        assert message in result.output
        assert not sent
    else:
        assert result.exit_code == 0, result.output
        body = sent[0]
        assert {k: body[k] for k in ("since", "until") if k in body} == window
        assert body["query"] == "Q"


@pytest.mark.parametrize("flag", ["--since", "--until"])
def test_help_says_how_a_bare_time_is_read(flag):
    result = CliRunner().invoke(cli, ["search", "--help"])
    text = " ".join(result.output.split())
    assert flag in text
    assert "without an offset is read as UTC" in text
