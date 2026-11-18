"""CLAIMS-21-FIX1: Lite search rows name the lanes that found them (core's ``_matched_by`` stamp -> ``matched_by``)."""

from click.testing import CliRunner
from smartmemory.models.memory_item import MemoryItem

from smartmemory_app import cli, storage


def _hit(lanes):
    item = MemoryItem(content="atlas budget", item_id="test_app_mb")
    item._matched_by = lanes
    return item


def test_a_core_search_hit_keeps_its_lanes_in_the_row():
    assert storage._result_dict(_hit(["lexical", "semantic"]))["matched_by"] == ["lexical", "semantic"]
    assert storage._result_dict(_hit(None))["matched_by"] is None


def test_a_row_without_the_stamp_gains_no_key():
    assert "matched_by" not in storage._result_dict(MemoryItem(content="x", item_id="test_app_mb_plain"))
    row = {"item_id": "test_app_mb_row", "content": "x", "matched_by": ["semantic"]}
    assert storage._result_dict(row)["matched_by"] == ["semantic"]


def test_cli_search_prints_the_lanes(monkeypatch):
    rows = [
        {"item_id": "test_app_mb_a", "content": "a", "matched_by": ["lexical", "semantic"]},
        {"item_id": "test_app_mb_b", "content": "b", "matched_by": None},
    ]
    monkeypatch.setattr(cli, "_memory_request", lambda *args, **kwargs: {"items": rows})
    result = CliRunner().invoke(cli.cli, ["search", "atlas budget"])
    assert result.exit_code == 0, result.output
    assert result.output.count("found by: lexical, semantic") == 1
    assert result.output.count("found by:") == 1
