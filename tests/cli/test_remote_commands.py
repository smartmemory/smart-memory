"""Offline CLI flows through the real remote client, config and HTTP transport."""

import json

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import cli as cli_module, config, storage


ANSWER = {
    "answer": "We chose SQLite.",
    "reasoning": "The decision records the reason.",
    "evidence": [
        {"item_id": "test_remote_ask_1", "content": "SQLite keeps the setup small."}
    ],
    "relations": [
        {
            "source": "Team",
            "type": "chose",
            "target": "SQLite",
            "source_id": "team",
            "target_id": "db",
        }
    ],
}


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-remote.invalid/api/")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_remote_key")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "test_remote_team")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "hook-recall.jsonl"))
    monkeypatch.setattr(storage, "_remote_memory", None)
    monkeypatch.setattr(
        cli_module,
        "_daemon_request",
        lambda *a, **k: pytest.fail("local daemon touched"),
    )
    monkeypatch.setattr(
        storage, "_get_local_memory", lambda *a, **k: pytest.fail("local store touched")
    )
    monkeypatch.setattr(
        config, "llm_key_present", lambda: pytest.fail("local LLM key checked")
    )
    calls = []
    reply = {"status": 200, "body": ANSWER}

    def handle(request):
        calls.append(request)
        assert str(request.url).startswith("https://test-remote.invalid/api/")
        assert request.headers["Authorization"] == "Bearer test_remote_key"
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json={"default_team_id": "test_discovered_team"})
        assert request.headers["X-Workspace-Id"] == reply.get(
            "team", "test_remote_team"
        )
        if "error" in reply:
            raise reply["error"]
        return httpx.Response(reply["status"], json=reply["body"])

    transport = httpx.MockTransport(handle)

    original_client = httpx.Client

    def client(**kwargs):
        # Keep httpx.request/get real, including their proxy policy and timeout.
        assert kwargs.get("trust_env", True) is True
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx._api, "Client", client)
    return calls, reply


def invoke(args, **kwargs):
    return CliRunner().invoke(cli_module.cli, args, **kwargs)


@pytest.mark.parametrize("reasoning", [False, True])
def test_remote_ask_hosted_contract_and_rendering(hosted, reasoning):
    calls, _ = hosted
    result = invoke(
        ["ask", "What did we choose?", "--limit", "3"]
        + (["--reasoning"] if reasoning else [])
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith(ANSWER["answer"])
    assert ("Reasoning:" in result.output) == reasoning
    assert ("SQLite keeps the setup small." in result.output) == reasoning
    if reasoning:
        assert "Team --chose--> SQLite" in result.output
    assert calls[-1].method == "POST"
    assert str(calls[-1].url) == "https://test-remote.invalid/api/memory/ask"
    assert json.loads(calls[-1].content) == {
        "question": "What did we choose?",
        "limit": 3,
        "reasoning": reasoning,
    }


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            401,
            {"detail": "bad token"},
            ["API key is invalid or expired", "setup --mode remote"],
        ),
        (
            403,
            {"detail": {"reason": "daily_query_quota_exceeded", "limit": 20}},
            ["daily_query_quota_exceeded", "20"],
        ),
        (403, {"detail": "Account is inactive"}, ["HTTP 403", "Account is inactive"]),
        (
            429,
            {"detail": "Daily query quota exceeded", "limit": 100, "current": 100},
            ["Daily query quota", "limit: 100"],
        ),
        (
            429,
            {"detail": "Memory quota exceeded", "limit": 50},
            ["Memory quota", "limit: 50"],
        ),
        (
            502,
            {"detail": "provider failed"},
            ["hosted LLM is unavailable", "No answer was generated"],
        ),
        (
            503,
            {"detail": "server key missing"},
            ["hosted LLM is unavailable", "No answer was generated"],
        ),
        (400, {"detail": "question must not be blank"}, ["question must not be blank"]),
    ],
)
def test_remote_ask_http_errors(hosted, status, body, expected):
    calls, reply = hosted
    reply.update(status=status, body=body)
    result = invoke(["ask", "What?"])
    assert result.exit_code == 1, result.output
    for text in expected:
        assert text in result.output
    assert ANSWER["answer"] not in result.output
    assert "GROQ_API_KEY" not in result.output
    assert len([r for r in calls if r.url.path.endswith("/memory/ask")]) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ConnectError("offline"), "API unreachable"),
        (httpx.ConnectTimeout("offline"), "timeout"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ProxyError("secret proxy credentials"), "network/proxy"),
        (httpx.RemoteProtocolError("dropped"), "network/proxy"),
    ],
)
def test_remote_ask_transport_errors(hosted, error, expected):
    _, reply = hosted
    reply["error"] = error
    result = invoke(["ask", "What?"])
    assert result.exit_code == 1
    assert expected in result.output
    assert "secret proxy credentials" not in result.output
    assert "direct local access" not in result.output


def test_remote_ask_missing_key(hosted, monkeypatch):
    calls, _ = hosted
    monkeypatch.delenv("SMARTMEMORY_API_KEY")
    monkeypatch.setattr("smartmemory_app.remote_backend.get_api_key", lambda: "")
    result = invoke(["ask", "What?"])
    assert result.exit_code == 1
    assert "No SmartMemory API key configured" in result.output
    assert calls == []


def test_remote_keychain_and_team_discovery(hosted, monkeypatch):
    calls, reply = hosted
    monkeypatch.delenv("SMARTMEMORY_API_KEY")
    monkeypatch.delenv("SMARTMEMORY_TEAM_ID")
    import keyring

    monkeypatch.setattr(
        keyring, "get_password", lambda service, user: "test_remote_key"
    )
    reply["team"] = "test_discovered_team"
    result = invoke(["ask", "What?"])
    assert result.exit_code == 0, result.output
    assert calls[-1].headers["X-Workspace-Id"] == "test_discovered_team"


@pytest.mark.parametrize(
    "body", [{}, {"answer": ""}, {"answer": " "}, {"answer": None}]
)
def test_remote_ask_invalid_success_is_not_an_answer(hosted, body):
    hosted[1]["body"] = body
    result = invoke(["ask", "What?"])
    assert result.exit_code == 1
    assert "invalid ask response" in result.output


def test_local_ask_unchanged(hosted, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    calls = []

    def daemon(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return ANSWER

    monkeypatch.setattr(cli_module, "_daemon_request", daemon)
    result = invoke(["ask", "What?", "--reasoning"])
    assert result.exit_code == 0, result.output
    assert "Reasoning:" in result.output
    assert calls == [
        ("POST", "/memory/ask", {"json": {"question": "What?", "limit": 5}})
    ]
    assert hosted[0] == []


def test_remote_add_preserves_type_and_origin(hosted):
    calls, reply = hosted
    reply["body"] = {"item_id": "test_remote_added"}
    result = invoke(["add", "A decision", "--type", "decision"])
    assert result.exit_code == 0, result.output
    assert "test_remote_added" in result.output
    assert calls[-1].url.path.endswith("/memory/ingest")
    assert json.loads(calls[-1].content) == {
        "content": "A decision",
        "context": {"origin": "cli:add", "memory_type": "decision"},
    }


def test_remote_add_stdin(hosted):
    hosted[1]["body"] = {"item_id": "test_remote_added"}
    result = invoke(["add", "-"], input="first\nsecond\n")
    assert result.exit_code == 0, result.output
    assert "Added 2 memories" in result.output
    assert len([r for r in hosted[0] if r.url.path.endswith("/memory/ingest")]) == 2


def test_remote_search_renders_service_envelope(hosted):
    calls, reply = hosted
    reply["body"] = {
        "results": [
            {
                "item_id": "test_remote_search_1",
                "memory_type": "semantic",
                "content": "A matched note",
            }
        ]
    }
    result = invoke(
        ["search", "note", "--top-k", "3", "--multi-hop", "--max-hops", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "A matched note" in result.output
    assert json.loads(calls[-1].content) == {
        "query": "note",
        "top_k": 3,
        "multi_hop": True,
        "max_hops": 2,
    }


@pytest.mark.parametrize(
    "args,message",
    [
        (["search", "note", "--project", "test"], "Property filters"),
        (["add", "note", "--project", "test"], "Property flags"),
    ],
)
def test_remote_unsupported_options_are_explicit(hosted, args, message):
    result = invoke(args)
    assert result.exit_code == 1
    assert message in result.output
    assert all(r.url.path.endswith("/auth/me") for r in hosted[0])


def test_remote_get(hosted):
    hosted[1]["body"] = {"item_id": "test_remote_get", "content": "The hosted note"}
    result = invoke(["get", "test_remote_get"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["content"] == "The hosted note"
    assert hosted[0][-1].url.path.endswith("/memory/test_remote_get")


def test_remote_get_does_not_hide_bad_key_as_not_found(hosted):
    hosted[1].update(status=401, body={"detail": "bad key"})
    result = invoke(["get", "test_remote_get"])
    assert result.exit_code == 1
    assert "API key is invalid" in result.output
    assert "Memory not found" not in result.output


def test_remote_recall_uses_existing_search_path(hosted):
    calls, reply = hosted
    reply["body"] = {
        "results": [
            {
                "item_id": "test_remote_recall",
                "content": "Remember this note",
                "memory_type": "semantic",
                "origin": "cli:add",
                "metadata": {"workspace_id": "test_remote_team"},
            }
        ]
    }
    result = invoke(
        [
            "recall",
            "--query",
            "note",
            "--workspace",
            "test_remote_team",
            "--no-snapshot",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Remember this note" in result.output
    assert all(not r.url.path.endswith("/recall") for r in calls)
    assert any(r.url.path.endswith("/memory/search") for r in calls)


def test_remote_status_uses_scoped_summary(hosted, monkeypatch):
    monkeypatch.setattr(
        "smartmemory_app.daemon.get_status", lambda: pytest.fail("local status touched")
    )
    hosted[1]["body"] = {"total_items": 42}
    result = invoke(["status"])
    assert result.exit_code == 0, result.output
    assert "Mode:       remote" in result.output
    assert "Memories:   42" in result.output
    assert "LLM:" not in result.output
    assert hosted[0][-1].url.path.endswith("/memory/summary")


def test_remote_why_reuses_hosted_client(hosted):
    calls, reply = hosted
    reply["body"] = {
        "decisions": [{"decision_id": "test_remote_decision"}],
        "decision": {"content": "The decision"},
    }
    result = invoke(["why", "Why?", "--json"])
    assert result.exit_code == 0, result.output
    assert [r.url.path for r in calls[-2:]] == [
        "/api/memory/decisions/search",
        "/api/memory/decisions/test_remote_decision/provenance",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["warm"],
        ["retag", "--content", "note", "--origin", "seed:test"],
        ["clear", "--yes"],
        ["clear"],
        ["rebuild", "--lexical"],
        ["tour", "--no-viewer"],
        ["export", "unused-bundle"],
        ["admin", "install-pack", "test_pack"],
        ["provenance", "import-codex"],
        ["import", "."],
        ["lifecycle", "persist"],
        ["lifecycle", "drain"],
    ],
)
def test_remote_local_only_commands_fail_before_state_access(hosted, args):
    result = invoke(args)
    assert result.exit_code == 1, result.output
    assert "only available in local mode" in result.output
    assert hosted[0] == []


def test_remote_daemon_ask_proxy_uses_hosted_llm(hosted):
    from smartmemory_app.local_api import api

    with TestClient(api) as client:
        response = client.post(
            "/ask", json={"question": "What?", "limit": 2, "reasoning": False}
        )
    assert response.status_code == 200, response.text
    assert response.json() == ANSWER
    assert json.loads(hosted[0][-1].content) == {
        "question": "What?",
        "limit": 2,
        "reasoning": False,
    }


def test_remote_daemon_ask_proxy_preserves_quota_failure(hosted):
    from smartmemory_app.local_api import api

    hosted[1].update(
        status=429, body={"detail": "Daily query quota exceeded", "limit": 20}
    )
    with TestClient(api) as client:
        response = client.post("/ask", json={"question": "What?"})
    assert response.status_code == 429
    assert "Daily query quota" in response.json()["detail"]


def test_remote_recall_surfaces_search_failure(hosted):
    hosted[1].update(status=401, body={"detail": "Invalid key"})
    result = invoke(["recall", "--query", "note", "--no-snapshot"])
    assert result.exit_code == 1
    assert "401" in result.output


def test_remote_add_does_not_retry_malformed_success(hosted):
    hosted[1]["body"] = {}
    result = invoke(["add", "note"])
    assert result.exit_code == 1
    assert "invalid ingest response" in result.output
    assert len([r for r in hosted[0] if r.url.path.endswith("/memory/ingest")]) == 1
