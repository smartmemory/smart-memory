"""CLI-TENANT-1: query commands fail loudly on no LLM key and on a workspace the web login does not use."""

import json

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, preflight, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS

CANARY = "test_canary_key_9f3a71c2"
WEB_TEAM = "test_web_team"
CLI_TEAM = "test_cli_team"
TENANT = "test_web_tenant"
EXTRACTIVE = {
    "answer": "x",
    "mode": "extractive",
    "synthesized": False,
    "evidence": [{"memory_type": "semantic", "item_id": "m1", "content": "c1"}],
}
ANSWER = {"answer": "We chose SQLite.", "evidence": [], "relations": []}


@pytest.fixture
def local(monkeypatch, tmp_path):
    for name in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", CANARY)
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(preflight, "_stored_llm_key_present", lambda: False)
    sent = []

    def fake_request(method, path, **kwargs):
        sent.append(path)
        return fake_request.reply

    fake_request.reply = ANSWER
    monkeypatch.setattr(cli_module, "_memory_request", fake_request)

    def no_network(*a, **k):
        pytest.fail("network used before the pre-flight check")

    monkeypatch.setattr(httpx.Client, "request", no_network)
    return sent, fake_request


def ask(*args):
    return CliRunner().invoke(cli_module.cli, ["ask", "What did we choose?", *args])


def test_no_key_names_the_key_and_the_fix_before_any_request(local):
    sent, _ = local
    result = ask()
    assert result.exit_code == 1, result.output
    for needle in (
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "export GROQ_API_KEY",
        "sm restart",
        "sm setup",
        "--extractive",
    ):
        assert needle in result.output
    assert CANARY not in result.output
    assert sent == []


def test_configured_provider_names_only_its_own_key(local, monkeypatch):
    sent, _ = local
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "anthropic")
    result = ask()
    assert result.exit_code == 1, result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "GROQ_API_KEY" not in result.output
    assert sent == []


def test_key_in_env_reaches_the_request(local, monkeypatch):
    sent, _ = local
    monkeypatch.setenv("GROQ_API_KEY", CANARY + "_llm")
    result = ask()
    assert result.exit_code == 0, result.output
    assert "We chose SQLite." in result.output
    assert CANARY not in result.output
    assert sent == ["/memory/ask"]


def test_key_in_keychain_or_profile_reaches_the_request(local, monkeypatch):
    sent, _ = local
    monkeypatch.setattr(preflight, "_stored_llm_key_present", lambda: True)
    assert ask().exit_code == 0
    assert sent == ["/memory/ask"]


def test_extractive_flag_keeps_the_keyless_listing(local):
    sent, request = local
    request.reply = EXTRACTIVE
    result = ask("--extractive")
    assert result.exit_code == 0, result.output
    assert "not a written answer" in result.output
    assert sent == ["/memory/ask"]


def test_daemon_without_key_is_an_error_not_excerpts(local, monkeypatch):
    sent, request = local
    monkeypatch.setenv("GROQ_API_KEY", CANARY + "_llm")
    request.reply = EXTRACTIVE
    result = ask()
    assert result.exit_code == 1, result.output
    assert "sm restart" in result.output
    assert "not a written answer" not in result.output
    assert CANARY not in result.output


def test_user_facing_strings_have_no_em_dash_or_semicolon():
    mismatch = [
        preflight.WorkspaceMismatch(
            CLI_TEAM, source, WEB_TEAM, TENANT, "https://api.invalid"
        )
        for source in ("SMARTMEMORY_TEAM_ID", "the config file")
    ]
    texts = [
        str(preflight.no_llm_key_error("none")),
        str(preflight.no_llm_key_error("groq")),
        str(preflight.daemon_missing_key_error()),
        *(m.warning() for m in mismatch),
        *(m.empty_hint() for m in mismatch),
    ]
    for text in texts:
        assert "—" not in text and ";" not in text, text


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-tenant.invalid/api")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", CANARY)
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", CLI_TEAM)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(storage, "_remote_memory", None)
    monkeypatch.setattr(preflight, "_workspace_warned", set())
    monkeypatch.setattr(
        cli_module,
        "_daemon_request",
        lambda *a, **k: pytest.fail("local daemon touched"),
    )
    state = {
        "me": {"default_team_id": WEB_TEAM, "tenant_id": TENANT},
        "rows": [],
        "ask": None,
    }
    seen = []

    def handle(request):
        seen.append((request.url.path, request.headers.get("X-Workspace-Id")))
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json=state["me"])
        if request.url.path.endswith("/memory/ask"):
            status, body = state["ask"] or (200, ANSWER)
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"results": state["rows"]})

    transport = httpx.MockTransport(handle)
    original = httpx.Client
    monkeypatch.setattr(
        httpx._api, "Client", lambda **kw: original(transport=transport, **kw)
    )
    return state, seen


def run(args):
    return CliRunner().invoke(cli_module.cli, args)


ROW = {"item_id": "m1", "content": "web memory", "memory_type": "semantic"}


def test_mismatch_env_source_warns_and_marks_the_empty_result(hosted):
    result = run(["search", "web"])
    assert result.exit_code == 0, result.output
    for needle in (
        f"workspace {CLI_TEAM} (from SMARTMEMORY_TEAM_ID)",
        f"defaults to workspace {WEB_TEAM}",
        f"tenant {TENANT}",
        "https://test-tenant.invalid/api",
        "unset SMARTMEMORY_TEAM_ID",
        f"No results in workspace {CLI_TEAM}",
    ):
        assert needle in result.stderr
    assert result.stdout.strip() == "No results."
    assert CANARY not in result.output


def test_mismatch_config_source_names_the_setup_command(hosted, monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_TEAM_ID")
    config.save_config(config.SmartMemoryConfig(mode="remote", team_id=CLI_TEAM))
    result = run(["search", "web"])
    assert result.exit_code == 0, result.output
    assert f"workspace {CLI_TEAM} (from the config file)" in result.stderr
    assert "smartmemory setup --mode remote" in result.stderr


def test_mismatch_with_results_warns_without_the_empty_hint(hosted):
    state, _ = hosted
    state["rows"] = [ROW]
    result = run(["search", "web"])
    assert result.exit_code == 0, result.output
    assert "defaults to workspace" in result.stderr
    assert "No results in workspace" not in result.stderr
    assert "web memory" in result.stdout


def test_mismatch_keeps_json_stdout_parseable(hosted):
    state, _ = hosted
    state["rows"] = [ROW]
    result = run(["search", "web", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["item_id"] == "m1"
    assert "defaults to workspace" in result.stderr


def test_same_workspace_is_silent(hosted):
    state, _ = hosted
    state["me"] = {"default_team_id": CLI_TEAM, "tenant_id": TENANT}
    result = run(["search", "web"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.strip() == "No results."


def test_unresolvable_login_does_not_invent_a_mismatch(hosted):
    state, _ = hosted
    state["me"] = {}
    result = run(["search", "web"])
    assert result.exit_code == 0, result.output
    assert "defaults to workspace" not in result.output


def test_mismatch_and_hosted_llm_outage_both_reach_the_user(hosted):
    state, _ = hosted
    state["ask"] = (502, {"detail": "llm unavailable"})
    result = run(["ask", "What?"])
    assert result.exit_code == 1, result.output
    assert result.output.index("defaults to workspace") < result.output.index(
        "hosted LLM is unavailable"
    )
    assert CANARY not in result.output


def test_remote_ask_never_needs_a_local_llm_key(hosted, monkeypatch):
    monkeypatch.setattr(
        preflight, "_stored_llm_key_present", lambda: pytest.fail("local key checked")
    )
    for name in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(name, "")
    assert run(["ask", "What?"]).exit_code == 0
