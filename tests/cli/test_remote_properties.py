"""CLI property contracts over real HTTP serialization and disposable SQLite.

This provider-free server verifies the wrapper wire contract, not deployed
service ingestion, authorization, or graph persistence.
"""

import json
import re
import shlex
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from smartmemory_app import cli as cli_module, config, storage
from smartmemory_app.remote_cli import _HOSTED_SCOPE_PROPERTIES


@pytest.fixture
def properties_server(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-p4.invalid/api")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_p4_key")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "test_p4_workspace")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(storage, "_remote_memory", None)
    calls = []
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE memories (item_id TEXT, content TEXT, memory_type TEXT, workspace TEXT, metadata TEXT)"
    )
    app = FastAPI()

    @app.get("/api/auth/me")
    def auth():
        return {"default_team_id": "test_p4_workspace"}

    @app.post("/api/memory/ingest")
    async def ingest(request: Request):
        body = await request.json()
        context = body["context"]
        item_id = f"test_p4_{db.execute('SELECT count(*) FROM memories').fetchone()[0]}"
        db.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
            (
                item_id,
                body["content"],
                context["memory_type"],
                request.headers["X-Workspace-Id"],
                json.dumps(context),
            ),
        )
        return {"item_id": item_id}

    @app.get("/api/memory/list")
    def list_page(
        request: Request,
        limit: int,
        offset: int,
        order: str,
        metadata_key: str | None = None,
        metadata_value: str | None = None,
        memory_type: str | None = None,
    ):
        assert order == "asc"
        where = "workspace = ?"
        values = [request.headers["X-Workspace-Id"]]
        if metadata_key is not None:
            if (
                not re.fullmatch(r"[a-zA-Z_]\w*", metadata_key.replace(".", "__"))
                or not metadata_value
            ):
                return JSONResponse(
                    {"detail": "Invalid metadata filter"}, status_code=422
                )
            where += " AND json_extract(metadata, ?) = ?"
            values.extend([f"$.{metadata_key}", metadata_value])
        if memory_type is not None:
            where += " AND memory_type = ?"
            values.append(memory_type)
        rows = db.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY item_id LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        total = db.execute(
            f"SELECT count(*) FROM memories WHERE {where}", values
        ).fetchone()[0]
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    class ContractTransport(httpx.BaseTransport):
        def handle_request(self, request):
            body = json.loads(request.content) if request.content else None
            calls.append(
                (request.method, request.url.path, dict(request.url.params), body)
            )
            if request.url.path.startswith("/api/"):
                assert request.headers["Authorization"] == "Bearer test_p4_key"
                if request.url.path != "/api/auth/me":
                    assert request.headers["X-Workspace-Id"] == "test_p4_workspace"
            with TestClient(app) as client:
                response = client.request(
                    request.method,
                    str(request.url),
                    headers=dict(request.headers),
                    content=request.content,
                )
            return httpx.Response(
                response.status_code, content=response.content, request=request
            )

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx._api,
        "Client",
        lambda **kwargs: original_client(transport=ContractTransport(), **kwargs),
    )
    try:
        yield calls, db, app
    finally:
        db.close()


def invoke(*args, **kwargs):
    return CliRunner().invoke(cli_module.cli, list(args), **kwargs)


@pytest.mark.parametrize(
    "options", [["--project", "atlas"], ["--prop", "project=atlas"]]
)
def test_remote_add_delivers_and_persists_all_properties(properties_server, options):
    result = invoke(
        "add",
        "A decision",
        "--type",
        "decision",
        *options,
        "--prop",
        "domain=R&D = team",
    )
    assert result.exit_code == 0, result.output
    assert "test_p4_0" in result.output
    calls, db, _ = properties_server
    expected = {
        "project": "atlas",
        "domain": "R&D = team",
        "origin": "cli:add",
        "memory_type": "decision",
    }
    assert calls[-1] == (
        "POST",
        "/api/memory/ingest",
        {},
        {"content": "A decision", "context": expected},
    )
    row = db.execute("SELECT * FROM memories").fetchone()
    assert json.loads(row["metadata"]) == expected
    assert row["workspace"] == "test_p4_workspace"


def test_remote_stdin_properties_reach_each_write(properties_server):
    result = invoke("add", "-", "--prop", "project=atlas", input="first\nsecond\n")
    assert result.exit_code == 0, result.output
    assert "Added 2 memories" in result.output
    rows = properties_server[1].execute("SELECT metadata FROM memories").fetchall()
    assert len(rows) == 2
    assert all(json.loads(row[0])["project"] == "atlas" for row in rows)


@pytest.mark.parametrize(
    "key", sorted(storage.RESERVED_INGEST_PROPERTIES | _HOSTED_SCOPE_PROPERTIES)
)
def test_remote_reserved_properties_refuse_before_write(properties_server, key):
    result = invoke("add", "note", "--prop", f"{key}=forged")
    assert result.exit_code == 1, result.output
    assert "Reserved remote add properties" in result.output
    assert key in result.output
    assert all(call[1] == "/api/auth/me" for call in properties_server[0])
    assert (
        properties_server[1].execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    "key", ["origin.nested", "workspace_id__nested", "workspace-id", "bad-key"]
)
def test_remote_unsupported_write_key_refuses(properties_server, key):
    result = invoke("add", "note", "--prop", f"{key}=forged")
    assert result.exit_code == 1, result.output
    assert "simple identifiers" in result.output
    assert all(call[1] == "/api/auth/me" for call in properties_server[0])


@pytest.mark.parametrize(
    "options", [["--project", "atlas"], ["--prop", "project=atlas"]]
)
def test_remote_exact_filter_and_continuation(properties_server, options):
    calls, db, _ = properties_server
    for content, project in [
        ("match one", "atlas"),
        ("match two", "atlas"),
        ("unmatched", "other"),
    ]:
        result = invoke("add", content, "--prop", f"project={project}")
        assert result.exit_code == 0, result.output
    db.execute(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
        (
            "test_p4_foreign",
            "foreign secret",
            "episodic",
            "test_p4_foreign_workspace",
            '{"project":"atlas"}',
        ),
    )
    calls.clear()
    result = invoke("search", "*", "--top-k", "1", *options)
    assert result.exit_code == 0, result.output
    assert "exact-match listing, without semantic ranking" in result.output
    assert "Showing 1 of 2" in result.output
    assert "match one" in result.output
    assert "match two" not in result.output
    assert "unmatched" not in result.output and "foreign secret" not in result.output
    assert (
        "Next page: sm search '*' --top-k 1 --offset 1 --prop project=atlas"
        in result.output
    )
    assert calls == [
        (
            "GET",
            "/api/memory/list",
            {
                "limit": "1",
                "offset": "0",
                "order": "asc",
                "metadata_key": "project",
                "metadata_value": "atlas",
            },
            None,
        )
    ]
    result = invoke(
        "search", "*", "--top-k", "1", "--offset", "1", "--prop", "project=atlas"
    )
    assert result.exit_code == 0, result.output
    assert "match two" in result.output and "Next page:" not in result.output


@pytest.mark.parametrize(
    "key,value,query_key",
    [
        ("memory_type", "decision", "memory_type"),
        ("profile.tier", "gold & silver", "metadata_key"),
    ],
)
def test_remote_type_and_nested_metadata_filters(
    properties_server, key, value, query_key
):
    db = properties_server[1]
    db.execute(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
        (
            "test_p4_nested",
            "Matched nested note",
            "decision",
            "test_p4_workspace",
            '{"profile":{"tier":"gold & silver"}}',
        ),
    )
    result = invoke("search", "*", "--prop", f"{key}={value}")
    assert result.exit_code == 0, result.output
    assert "Matched nested note" in result.output
    params = properties_server[0][-1][2]
    assert params[query_key] == (value if key == "memory_type" else key)
    if query_key == "metadata_key":
        assert params["metadata_value"] == value


@pytest.mark.parametrize(
    "args,message",
    [
        (
            ["search", "note", "--prop", "project=atlas"],
            "hosted search API has no arbitrary property predicates",
        ),
        (
            ["search", "*", "--prop", "project=atlas", "--prop", "domain=legal"],
            "only one property filter",
        ),
        (
            ["search", "*", "--prop", "project=atlas", "--multi-hop"],
            "--max-hops, --multi-hop",
        ),
        (["search", "*", "--prop", "project=atlas", "--since", "7d"], "--since"),
        (["add", "note", "--prop", "project"], "key=value"),
        (["add", "note", "--prop", "=atlas"], "key=value"),
        (["add", "note", "--prop", "project="], "key=value"),
        (["add", "note", "--unsupported"], "missing value"),
        (["search", "*", "--unsupported"], "missing value"),
        (
            ["search", "*", "--prop", "project=atlas", "--max-hops", "2"],
            "--max-hops requires --multi-hop",
        ),
        (
            ["add", "note", "--prop", "project=atlas", "--project", "other"],
            "duplicate property",
        ),
    ],
)
def test_unsupported_flags_are_visible_refusals(properties_server, args, message):
    result = invoke(*args)
    assert result.exit_code == 1, result.output
    assert message in result.output
    assert all(call[1] == "/api/auth/me" for call in properties_server[0])


def test_quoted_property_filter_survives_next_page_command(properties_server):
    value = "R&D 'topic' = 2"
    for note in ("first quoted note", "second quoted note"):
        result = invoke("add", "--prop", f"project={value}", note)
        assert result.exit_code == 0, result.output
    result = invoke("search", "*", "--top-k", "1", "--prop", f"project={value}")
    assert result.exit_code == 0, result.output
    command = next(
        line.removeprefix("Next page: ")
        for line in result.output.splitlines()
        if line.startswith("Next page:")
    )
    continuation = invoke(*shlex.split(command)[1:])
    assert continuation.exit_code == 0, continuation.output
    assert "second quoted note" in continuation.output
    assert properties_server[0][-1][2]["metadata_value"] == value


def test_list_filter_pair_refuses_before_http(properties_server):
    from smartmemory_app.remote_backend import RemoteBackendError

    memory = storage._get_remote_memory(config.load_config())
    properties_server[0].clear()
    with pytest.raises(RemoteBackendError, match="must be supplied together") as raised:
        memory.list_memories(metadata_key="project")
    assert raised.value.status_code == 422
    assert properties_server[0] == []


def test_filtered_list_service_error_is_not_empty(properties_server):
    result = invoke("search", "*", "--prop", "bad-key=atlas")
    assert result.exit_code == 1, result.output
    assert "HTTP 422" in result.output
    assert "No results." not in result.output


def test_local_property_search_uses_real_sqlite(properties_server, monkeypatch):
    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory_app.local_api import api

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    properties_server[2].mount("/memory", api)
    monkeypatch.setattr(httpx, "Client", httpx._api.Client)
    backend = SQLiteBackend()
    try:
        for index, project in enumerate(["atlas", "other"]):
            backend.add_node(
                f"test_p4_local_{index}",
                {
                    "content": f"Local {project}",
                    "project": project,
                    "node_category": "memory",
                },
                memory_type="semantic",
            )
        monkeypatch.setattr(
            storage,
            "get_memory",
            lambda: SimpleNamespace(_graph=SimpleNamespace(backend=backend)),
        )
        result = invoke("search", "*", "--prop", "project=atlas")
        assert result.exit_code == 0, result.output
        assert "Local atlas" in result.output and "Local other" not in result.output
        assert "exact-match listing" not in result.output
        assert properties_server[0][-1] == (
            "POST",
            "/memory/search",
            {},
            {"query": "*", "top_k": 5, "filters": {"project": "atlas"}},
        )
    finally:
        backend.close()
