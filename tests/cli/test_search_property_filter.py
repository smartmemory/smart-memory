"""sm search --<property> filters the local store through the real storage post-filter (CLI-SEARCH-PROPERTY-FILTER-FLAGS-1)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from smartmemory_app.cli import cli

# id -> metadata. a/b/c/d cover project only, domain only, both, and another project.
ITEMS = {
    "a": {"project": "atlas"},
    "b": {"domain": "legal"},
    "c": {"project": "atlas", "domain": "legal"},
    "d": {"project": "other"},
}

# (args, ids shown in order, window kwargs core must receive)
FILTER_ROWS = [
    (["--project", "atlas", "Q"], ["a", "c"], {}),
    (["Q", "--project", "atlas"], ["a", "c"], {}),
    (["--domain", "legal", "Q"], ["b", "c"], {}),
    (["--project", "atlas", "--domain", "legal", "Q"], ["c"], {}),
    (["--project", "nomatch", "Q"], [], {}),
    (["--domain", "nomatch", "Q"], [], {}),
    (["--project", "atlas", "--top-k", "1", "Q"], ["a"], {}),
    (
        ["--project", "atlas", "--until", "2030-01-01T00:00:00Z", "Q"],
        ["a", "c"],
        {"until": "2030-01-01T00:00:00+00:00"},
    ),
]


class _Hit:
    def __init__(self, item_id):
        self.item_id = item_id

    def to_dict(self):
        return {
            "item_id": self.item_id,
            "content": f"memory {self.item_id}",
            "memory_type": "episodic",
            "metadata": ITEMS[self.item_id],
        }


@pytest.mark.parametrize("row", FILTER_ROWS, ids=lambda row: " ".join(row[0]))
def test_property_filter_reaches_local_results(row):
    args, expected, window = row
    calls = []

    def core_search(query, top_k, **kwargs):
        calls.append(kwargs)
        return [_Hit(item_id) for item_id in ITEMS][:top_k]

    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.cli._prepare_direct_access"),
        patch("smartmemory_app.storage.load_config") as config,
        patch(
            "smartmemory_app.storage.get_memory",
            return_value=SimpleNamespace(search=core_search),
        ),
    ):
        config.return_value.mode = "local"
        result = CliRunner().invoke(cli, ["search", *args])

    assert result.exit_code == 0, result.output
    shown = [
        line.split()[1] for line in result.output.splitlines() if "[episodic]" in line
    ]
    assert shown == expected
    if not expected:
        assert "No results." in result.output
    for key, value in window.items():
        assert all(kwargs.get(key) == value for kwargs in calls)
