"""Real CLI/client against a disposable SQLite-backed HTTP contract server.

The in-process server exercises pagination and transport without provider calls
or a deployed hosted account. It does not verify the production service's auth.
"""

import sqlite3
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from smartmemory_app import cli as cli_module, config, storage
from smartmemory_app.remote_backend import RemoteBackendError


@pytest.fixture
def listing_server(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-p3.invalid/api")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_p3_key")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "test_p3_workspace")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(storage, "_remote_memory", None)
    calls = []
    state = {"status": 200}
    app = FastAPI()
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE memories (item_id TEXT, content TEXT, memory_type TEXT, workspace TEXT)"
    )
    db.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?)",
        [
            (f"test_p3_{i}", f"Listed note {i}", "semantic", "test_p3_workspace")
            for i in range(7)
        ]
        + [
            (
                "test_p3_other",
                "Other workspace secret",
                "semantic",
                "test_p3_other_workspace",
            )
        ],
    )

    @app.middleware("http")
    async def record(request: Request, call_next):
        calls.append((request.method, request.url.path, dict(request.query_params)))
        if request.url.path.startswith("/api/"):
            assert request.headers["Authorization"] == "Bearer test_p3_key"
        return await call_next(request)

    @app.get("/api/auth/me")
    def auth():
        return {"default_team_id": "test_p3_workspace"}

    @app.get("/api/memory/list")
    def list_page(request: Request, limit: int, offset: int, order: str):
        if state["status"] != 200:
            return JSONResponse(
                {"detail": "test_p3_list_failure"}, status_code=state["status"]
            )
        if "body" in state:
            return JSONResponse(state["body"])
        assert order == "asc"
        workspace = request.headers["X-Workspace-Id"]
        assert workspace == "test_p3_workspace"
        rows = db.execute(
            "SELECT item_id, content, memory_type FROM memories WHERE workspace = ? "
            "ORDER BY item_id LIMIT ? OFFSET ?",
            (workspace, limit, offset),
        ).fetchall()
        total = db.execute(
            "SELECT count(*) FROM memories WHERE workspace = ?", (workspace,)
        ).fetchone()[0]
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "policy": {"include_grounding": False, "source": "env"},
        }

    class ContractTransport(httpx.BaseTransport):
        def handle_request(self, request):
            with TestClient(app) as server:
                response = server.request(
                    request.method,
                    str(request.url),
                    headers=dict(request.headers),
                    content=request.content,
                )
            # Starlette may use httpx2 internally. Return this client's native
            # response so raise_for_status uses the production httpx exceptions.
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

    original_client = httpx.Client

    def client(**kwargs):
        return original_client(transport=ContractTransport(), **kwargs)

    monkeypatch.setattr(httpx._api, "Client", client)
    try:
        yield calls, state, db, app
    finally:
        db.close()


def invoke(*args):
    return CliRunner().invoke(cli_module.cli, ["search", *args])


def test_remote_list_page_boundaries_and_continuation(listing_server):
    calls = listing_server[0]
    seen = []
    for offset, count in [(0, 3), (3, 3), (6, 1)]:
        result = invoke("*", "--top-k", "3", "--offset", str(offset))
        assert result.exit_code == 0, result.output
        assert f"Showing {count} of 7 memories (offset {offset})." in result.output
        notes = [i for i in range(7) if f"Listed note {i}" in result.output]
        assert notes == list(range(offset, offset + count))
        seen.extend(notes)
        if offset + count < 7:
            assert (
                f"Next page: sm search '*' --top-k 3 --offset {offset + count}"
                in result.output
            )
        else:
            assert "Next page:" not in result.output
        assert "Other workspace secret" not in result.output
        assert calls[-1] == (
            "GET",
            "/api/memory/list",
            {"limit": "3", "offset": str(offset), "order": "asc"},
        )
    assert seen == list(range(7))
    assert all(path in ("/api/auth/me", "/api/memory/list") for _, path, _ in calls)


@pytest.mark.parametrize("limit", [None, 1, 7, 20])
def test_remote_list_respects_default_and_requested_limit(listing_server, limit):
    args = [] if limit is None else ["--top-k", str(limit)]
    result = invoke(" * ", *args)
    assert result.exit_code == 0, result.output
    expected_limit = 5 if limit is None else limit
    assert result.output.count("[semantic]") == min(expected_limit, 7)
    assert listing_server[0][-1][2]["limit"] == str(expected_limit)
    assert ("Next page:" in result.output) == (expected_limit < 7)


@pytest.mark.parametrize("offset", [7, 10])
def test_remote_list_empty_page_past_end(listing_server, offset):
    result = invoke("*", "--top-k", "3", "--offset", str(offset))
    assert result.exit_code == 0, result.output
    assert f"Showing 0 of 7 memories (offset {offset})." in result.output
    assert "No results." in result.output
    assert "Next page:" not in result.output


def test_remote_list_empty_workspace(listing_server):
    listing_server[2].execute(
        "DELETE FROM memories WHERE workspace = ?", ("test_p3_workspace",)
    )
    result = invoke("*")
    assert result.exit_code == 0, result.output
    assert "Showing 0 of 0 memories (offset 0)." in result.output
    assert "No results." in result.output
    assert "Next page:" not in result.output
    assert "Other workspace secret" not in result.output


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
def test_remote_list_http_errors_are_typed_and_visible(listing_server, status):
    listing_server[1]["status"] = status
    mem = storage._get_remote_memory(config.load_config())
    with pytest.raises(RemoteBackendError) as raised:
        mem.list_memories(limit=3)
    assert raised.value.status_code == status
    result = invoke("*", "--top-k", "3")
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception.__context__, click.ClickException)
    cause = result.exception.__context__.__cause__
    assert isinstance(cause, RemoteBackendError)
    assert cause.status_code == status
    assert (
        "invalid or expired" if status == 401 else f"HTTP {status}"
    ) in result.output
    assert "No results." not in result.output
    assert "Next page:" not in result.output


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"items": [], "limit": 5, "offset": 0},
        {"items": [], "total": "0", "limit": 5, "offset": 0},
        {"items": [], "total": 0, "limit": 5, "offset": 1},
        {"items": ["bad row"], "total": 1, "limit": 5, "offset": 0},
        {"items": [{}] * 6, "total": 6, "limit": 5, "offset": 0},
    ],
)
def test_remote_list_malformed_success_is_not_empty(listing_server, body):
    listing_server[1]["body"] = body
    result = invoke("*")
    assert result.exit_code == 1, result.output
    assert "invalid list response" in result.output
    assert "No results." not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["*", "--top-k", "0"],
        ["*", "--top-k", "-1"],
        ["*", "--offset", "-1"],
        ["note", "--offset", "0"],
        ["*", "--since", "7d"],
        ["*", "--until", "24h"],
        ["*", "--include-reference"],
        ["*", "--multi-hop"],
    ],
)
def test_remote_listing_unsupported_options_refuse_before_list(listing_server, args):
    result = invoke(*args)
    assert result.exit_code in (1, 2), result.output
    assert "Error:" in result.output
    assert all(path == "/api/auth/me" for _, path, _ in listing_server[0])


def test_local_offset_is_explicit_refusal(listing_server, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    result = invoke("*", "--offset", "0")
    assert result.exit_code == 1, result.output
    assert "--offset is only supported by remote" in result.output
    assert listing_server[0] == []


def test_local_wildcard_still_lists_all_through_real_storage(
    listing_server, monkeypatch
):
    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory_app.local_api import api

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    listing_server[3].mount("/memory", api)
    monkeypatch.setattr(httpx, "Client", httpx._api.Client)
    backend = SQLiteBackend()
    try:
        for i in range(7):
            backend.add_node(
                f"test_p3_local_{i}",
                {"content": f"Local note {i}", "node_category": "memory"},
                memory_type="semantic",
            )
        # The wildcard path only needs a graph backend. Supply the real SQLite
        # store without starting unrelated embedding or extraction providers.
        memory = SimpleNamespace(_graph=SimpleNamespace(backend=backend))
        monkeypatch.setattr(storage, "get_memory", lambda: memory)
        result = invoke("*", "--top-k", "3")
        assert result.exit_code == 0, result.output
        assert result.output.count("[semantic]") == 7
        assert all(f"Local note {i}" in result.output for i in range(7))
        assert "Next page:" not in result.output
        assert "Showing" not in result.output
        assert listing_server[0] == [("POST", "/memory/search", {})]
    finally:
        backend.close()
