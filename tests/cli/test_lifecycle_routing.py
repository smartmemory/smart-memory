"""Lifecycle dispatch regressions and offline flows through the real engine."""

import json
import os

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, storage
from smartmemory_app.lifecycle import MemoryLifecycle


PHASES = ("orient", "recall", "observe", "distill", "learn")
SESSION = "test_p2_session"
WORKSPACE = "test_p2_workspace"
PROMPT = "How should we handle retries?"


def payload(cwd):
    return {
        "session_id": SESSION,
        "cwd": str(cwd),
        "prompt": PROMPT,
        "tool_name": "Bash",
        "tool_input": {"command": "test_p2_check"},
        "tool_response": {"stdout": "test_p2_result"},
        "transcript_path": str(cwd / "test_p2_transcript.jsonl"),
        "last_assistant_message": "Checkpoint each successful unit.",
        "error": {"message": "test_p2_failure"},
    }


def invoke(phase, body):
    return CliRunner().invoke(
        cli_module.cli, ["lifecycle", phase], input=json.dumps(body)
    )


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    return tmp_path


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("daemon_available", [True, False])
def test_local_still_tries_daemon_and_falls_back(
    isolated, monkeypatch, phase, daemon_available
):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    body = payload(isolated)
    daemon_calls, engine_calls = [], []

    def daemon(path, received):
        daemon_calls.append((path, received))
        return {"context": "test_p2_daemon_context"} if daemon_available else None

    def engine(self, *args, **kwargs):
        assert not daemon_available, "engine ran after a successful daemon phase"
        engine_calls.append((self.session_id, args, kwargs))
        return "test_p2_engine_context"

    monkeypatch.setattr(cli_module, "_lifecycle_via_daemon", daemon)
    monkeypatch.setattr(MemoryLifecycle, phase, engine)
    result = invoke(phase, body)
    assert result.exit_code == 0, result.output
    assert daemon_calls == [(f"/lifecycle/{phase}", body)]
    if daemon_available:
        assert engine_calls == []
    else:
        assert len(engine_calls) == 1
        session, args, kwargs = engine_calls[0]
        assert session == SESSION
        assert kwargs["cwd"] == body["cwd"]
        if phase == "recall":
            assert args == (PROMPT,)
        elif phase == "observe":
            assert kwargs == {
                "tool_name": body["tool_name"],
                "tool_input": body["tool_input"],
                "tool_result": json.dumps(body["tool_response"]),
                "transcript_path": body["transcript_path"],
                "cwd": body["cwd"],
            }
        elif phase == "distill":
            assert kwargs["response"] == body["last_assistant_message"]
        elif phase == "learn":
            assert kwargs["tool_name"] == body["tool_name"]
            assert kwargs["error"] == json.dumps(body["error"])
    expected = (
        "test_p2_daemon_context" if daemon_available else "test_p2_engine_context"
    )
    assert result.output == (expected + "\n" if phase in PHASES[:2] else "")


@pytest.fixture
def hosted(isolated, monkeypatch):
    """Replace only network/model boundaries, retaining config, engine and storage."""
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-p2.invalid")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_p2_key")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "test_p2_team")
    monkeypatch.setattr(storage, "_remote_memory", None)
    monkeypatch.setattr(
        storage, "_get_local_memory", lambda *a, **k: pytest.fail("local store touched")
    )
    monkeypatch.setattr(
        cli_module,
        "_lifecycle_via_daemon",
        lambda *a, **k: pytest.fail("remote hook attempted the local daemon"),
    )
    monkeypatch.setattr(
        "smartmemory.plugins.embedding.create_embeddings", lambda text: [1.0, 0.0]
    )
    (isolated / "config.toml").write_text(
        '[smartmemory]\nmode = "remote"\n'
        '[lifecycle]\nrecall_strategy = "every_prompt"\n'
        "orient_budget = 80\nrecall_budget = 80\n",
        encoding="utf-8",
    )
    calls, ingests = [], []
    reply = {"status": 200}

    def handle(request):
        calls.append(request)
        assert request.url.host == "test-p2.invalid"
        assert request.headers["Authorization"] == "Bearer test_p2_key"
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"default_team_id": "test_p2_team"})
        if reply["status"] != 200:
            return httpx.Response(reply["status"], json={"detail": "test_p2_offline"})
        if request.url.path == "/memory/ingest":
            ingests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"item_id": f"test_p2_write_{len(ingests)}"}
            )
        if request.url.path == "/memory/decisions":
            assert request.headers["X-Workspace-Id"] == WORKSPACE
            return httpx.Response(200, json={"decisions": []})
        assert request.url.path == "/memory/search"
        body = json.loads(request.content)
        if body.get("memory_type") == "snapshot":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "item_id": "test_p2_recalled",
                        "content": "Checkpoint every successful unit.",
                        "memory_type": "semantic",
                        "origin": "cli:add",
                        "metadata": {"workspace_id": WORKSPACE},
                    },
                    {
                        "item_id": "test_p2_wrong_workspace",
                        "content": "Do not inject this other project.",
                        "memory_type": "semantic",
                        "metadata": {"workspace_id": "test_p2_other_workspace"},
                    },
                    {
                        "item_id": "test_p2_over_budget",
                        "content": "test_p2_long_context " * 100,
                        "memory_type": "semantic",
                        "origin": "cli:add",
                        "metadata": {"workspace_id": WORKSPACE},
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handle)
    original_client = httpx.Client

    def client(**kwargs):
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx._api, "Client", client)
    return isolated, calls, ingests, reply


@pytest.mark.parametrize(
    "file_mode,env_mode,tries_daemon",
    [
        ("remote", None, False),
        ("local", "remote", False),
        ("remote", "local", True),
        ("local", None, True),
    ],
)
def test_dispatch_uses_resolved_config_mode(
    isolated, monkeypatch, file_mode, env_mode, tries_daemon
):
    (isolated / "config.toml").write_text(
        f'[smartmemory]\nmode = "{file_mode}"\n', encoding="utf-8"
    )
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    if env_mode:
        monkeypatch.setenv("SMARTMEMORY_MODE", env_mode)
    calls = []

    def daemon(path, body):
        assert tries_daemon, "remote config attempted the local daemon"
        calls.append(path)
        return {"context": "test_p2_context"}

    monkeypatch.setattr(cli_module, "_lifecycle_via_daemon", daemon)
    result = cli_module._lifecycle_request("/lifecycle/orient", {})
    assert calls == (["/lifecycle/orient"] if tries_daemon else [])
    assert result == ({"context": "test_p2_context"} if tries_daemon else None)


@pytest.mark.parametrize("phase", PHASES)
def test_config_failure_still_runs_engine_fail_open(
    isolated, monkeypatch, phase, caplog
):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    # The existing daemon helper swallows this config failure while resolving its URL.
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", "test_p2_invalid_port")
    calls = []

    def engine(self, *args, **kwargs):
        calls.append(self.session_id)
        return ""

    monkeypatch.setattr(MemoryLifecycle, phase, engine)
    result = invoke(phase, payload(isolated))
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert calls == [SESSION]
    assert "mode resolution failed; using in-process engine" in caplog.text


def state(root):
    return json.loads((root / "data" / "sessions" / f"{SESSION}.json").read_text())


def traces(root):
    return [
        json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()
    ]


def test_remote_phases_skip_daemon_and_preserve_engine_flow(hosted, caplog):
    root, calls, ingests, _ = hosted
    body = payload(root)
    for phase in PHASES:
        result = invoke(phase, body)
        assert result.exit_code == 0, result.output
        if phase in PHASES[:2]:
            assert "Checkpoint every successful unit." in result.output
            assert "Do not inject this other project" not in result.output
            assert "test_p2_long_context" not in result.output
            record = traces(root)[-1]
            assert record["phase"] == phase
            assert record["session_id"] == SESSION
            assert record["workspace_id"] == WORKSPACE
            assert record["cwd"] == body["cwd"]
            assert record["payload_tokens"] <= 80
            assert record["injected_ids"] == ["test_p2_recalled"]
        else:
            assert result.output == ""
        if phase == "orient":
            assert state(root)["turn_count"] == 0
            assert state(root)["rules_card_workspace"] == WORKSPACE
            assert state(root)["rules_card_ids"] == []
            assert (
                "remote backend has no complete workspace lesson listing" in caplog.text
            )
        elif phase == "recall":
            assert state(root)["current_user_turn"] == PROMPT
            assert state(root)["turn_count"] == 1
        elif phase == "observe":
            assert state(root)["observation_count"] == 1
        elif phase == "distill":
            assert state(root)["current_user_turn"] is None
            assert (
                state(root)["last_assistant_message"] == body["last_assistant_message"]
            )
    assert len(ingests) == 3
    contents = [item["content"] for item in ingests]
    assert "test_p2_result" in contents[0]
    assert contents[1] == f"User: {PROMPT}\nAssistant: {body['last_assistant_message']}"
    assert "test_p2_failure" in contents[2]
    assert all(not request.url.path.startswith("/lifecycle/") for request in calls)
    assert len(traces(root)) == 2


@pytest.mark.parametrize("phase", PHASES)
def test_remote_failures_stay_fail_open_with_diagnostics(hosted, phase, caplog):
    root, _, _, reply = hosted
    body = payload(root)
    # Seed the prompt using the real session file, then fail hosted operations.
    assert invoke("recall", body).exit_code == 0
    if phase == "recall":
        body["prompt"] = "What happens when the service fails?"
    reply["status"] = 503
    result = invoke(phase, body)
    assert result.exit_code == 0, result.output
    assert result.output == ""
    expected = {
        "orient": "Remote recall lost search context",
        "recall": "Remote recall lost search context",
        "observe": "Observe ingest failed; tool observation lost",
        "distill": "Distill ingest failed; turn pair lost",
        "learn": "Learn ingest failed; error memory lost",
    }
    assert expected[phase] in caplog.text
    if phase in PHASES[:2]:
        record = traces(root)[-1]
        assert record["phase"] == phase
        assert record["error"]
        assert record["payload"] == ""
    if phase == "recall":
        assert state(root)["current_user_turn"] == body["prompt"]
        assert state(root)["turn_count"] == 2
    elif phase == "observe":
        assert state(root)["observation_count"] == 0
    elif phase == "distill":
        assert state(root)["current_user_turn"] is None


@pytest.mark.parametrize("failure", ["connect", "timeout", "http", "json"])
def test_local_daemon_failures_fail_open_with_short_connect_budget(
    isolated, monkeypatch, failure
):
    """HOOK-DEADLINE: live marker -> 1.5 s connect, 5 s read; failures fall back."""
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "daemon.pid").write_text(str(os.getpid()))
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "connect":
            raise httpx.ConnectError("test_p2_refused")
        if failure == "timeout":
            raise httpx.ReadTimeout("test_p2_slow")
        return httpx.Response(503 if failure == "http" else 200, text="invalid-json")

    original_client = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    assert cli_module._lifecycle_request("/lifecycle/orient", payload(isolated)) is None
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {
        "connect": 1.5,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }
