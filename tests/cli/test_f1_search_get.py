"""Exact ID prefix lookup uses real SQLite and refuses ambiguity."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from smartmemory.graph.backends.sqlite import SQLiteBackend

from smartmemory_app import cli, local_api, storage


@pytest.fixture
def backend(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "test_F1_ids.db"))
    for iid in ("abcdef01-1111", "abcdef02-2222", "fedcba01-3333", "abcdef"):
        backend.add_node(iid, {"memory_type": "semantic", "content": "saved " + iid})
    yield backend
    backend.close()


def test_exact_first_unique_and_ambiguous_prefix(backend):
    assert storage.resolve_readonly_item_id(backend, "abcdef") == "abcdef"
    assert storage.resolve_readonly_item_id(backend, "fedcba") == "fedcba01-3333"
    assert storage.resolve_readonly_item_id(backend, "abcde") == "abcde"
    with pytest.raises(ValueError, match="abcdef01-1111.*abcdef02-2222"):
        storage.resolve_readonly_item_id(backend, "abcdef0")


def test_local_api_prefix_get_and_delete_stays_exact(backend, monkeypatch):
    monkeypatch.setattr(
        local_api,
        "_get_mem",
        lambda: SimpleNamespace(
            _graph=SimpleNamespace(backend=backend), delete=backend.remove_node
        ),
    )
    with TestClient(local_api.api) as client:
        result = client.get("/fedcba")
        assert result.status_code == 200 and result.json()["item_id"] == "fedcba01-3333"
        result = client.get("/abcdef0")
        assert result.status_code == 409
        assert "abcdef01-1111" in result.text and "abcdef02-2222" in result.text
        assert client.delete("/fedcba").status_code == 404
        assert backend.get_node("fedcba01-3333") is not None


def test_search_full_ids_and_json(monkeypatch):
    rows = [
        {
            "item_id": "abcdef01-1111-2222-3333-444444444444",
            "content": "body " * 100,
            "confidence": 0.7,
        }
    ]
    monkeypatch.setattr(cli, "_memory_request", lambda *args, **kwargs: {"items": rows})
    result = CliRunner().invoke(cli.cli, ["search", "test"])
    assert result.exit_code == 0 and rows[0]["item_id"] in result.output
    result = CliRunner().invoke(cli.cli, ["search", "test", "--json"])
    assert result.exit_code == 0 and json.loads(result.output) == rows


def test_remote_prefix_refused(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    result = CliRunner().invoke(cli.cli, ["get", "abcdef01"])
    assert result.exit_code != 0 and "full item ID" in result.output
