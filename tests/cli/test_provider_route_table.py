"""A configured provider reaches its own model, endpoint and key, never OpenAI's (CLI-PROVIDER-ROUTE-1).

Tester repro: provider ``anthropic`` or ``claude-agent`` with a stale OpenAI key in the environment fell through to
gpt-4o-mini at api.openai.com. One row per ``LLM_PROVIDER_TABLE`` member; a new provider without a row fails the guard.

WRAPPER-2.0-LLM-PROVIDER-ROUTE-PORT-1 (ported from main): every call runs behind the
wrapper's route guard with the provider picked in the config, as the daemon does. Core 2.0
lacks main's Anthropic route (core 7d792b92), so there the anthropic and claude-agent rows
assert the guard refuses before any request instead of reaching api.anthropic.com.
"""

import json

import httpx
import pytest

from smartmemory.utils import llm
from smartmemory.utils.llm_client import openai_chat
from smartmemory_app.config import LLM_PROVIDER_TABLE, SmartMemoryConfig, save_config
from smartmemory_app.llm_errors import LlmRouteRefused, route_for
from smartmemory_app.storage import _guard_llm_route, _provider_default_model

REAL_OPENAI = openai_chat.OpenAI
DECOY = "decoy-openai-key"
KEYS = {
    "OPENAI_API_KEY": DECOY,
    "ANTHROPIC_API_KEY": "canary-anthropic",
    "GROQ_API_KEY": "canary-groq",
    "GEMINI_API_KEY": "canary-gemini",
}
OK = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
}

# provider -> (pinned model, endpoint host route_for names, endpoint host the real call reaches, key env sent)
# claude-agent is run with the Agent SDK absent: core expands the alias to the Anthropic API.
ROWS = {
    "groq": ("openai/gpt-oss-120b", "api.groq.com", "api.groq.com", "GROQ_API_KEY"),
    "openai": ("gpt-4o-mini", "api.openai.com", "api.openai.com", "OPENAI_API_KEY"),
    "anthropic": (
        "claude-haiku-4-5-20251001",
        "api.anthropic.com",
        "api.anthropic.com",
        "ANTHROPIC_API_KEY",
    ),
    "gemini": (
        "gemini/gemini-2.0-flash",
        "generativelanguage.googleapis.com",
        "generativelanguage.googleapis.com",
        "GEMINI_API_KEY",
    ),
    "claude-agent": (
        "sonnet",
        "api.anthropic.com",
        "api.anthropic.com",
        "ANTHROPIC_API_KEY",
    ),
}
CORE_SERVES_ANTHROPIC = hasattr(llm, "ANTHROPIC_OPENAI_BASE_URL")
ANTHROPIC_FAMILY = {"anthropic", "claude-agent"}
NO_ROUTE = {
    "ollama",
    "lmstudio",
    "none",
}  # no pinned model: the chosen model or no LLM at all


@pytest.fixture(autouse=True)
def guarded(monkeypatch, tmp_path):
    """Pick nothing yet, and restore the core functions the route guard wraps."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    for name in ("call_openai_chat", "_call_claude_agent"):
        monkeypatch.setattr(llm, name, getattr(llm, name))


def _pick(tmp_path, provider):
    save_config(
        SmartMemoryConfig(
            mode="local", data_dir=str(tmp_path / "data"), llm_provider=provider
        )
    )
    _guard_llm_route()


def test_every_table_member_has_a_row():
    assert set(ROWS) | NO_ROUTE == set(LLM_PROVIDER_TABLE)


@pytest.mark.parametrize("provider", sorted(ROWS))
def test_provider_reaches_its_own_endpoint_with_its_own_key(
    monkeypatch, tmp_path, provider
):
    model, route_host, call_host, key_var = ROWS[provider]
    for var in (
        *KEYS,
        "OPENAI_BASE_URL",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "SMARTMEMORY_LLM_MODEL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    for var, value in KEYS.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(llm, "_claude_agent_sdk_available", lambda: False)

    _pick(tmp_path, provider)
    pinned = _provider_default_model(provider, {})
    assert pinned == model
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", pinned)
    assert (
        route_for(provider, {"SMARTMEMORY_LLM_MODEL": pinned}).endpoint.split("/")[2]
        == route_host
    )

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=OK)

    monkeypatch.setattr(
        openai_chat,
        "OpenAI",
        lambda **kw: REAL_OPENAI(
            http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
        ),
    )
    if provider in ANTHROPIC_FAMILY and not CORE_SERVES_ANTHROPIC:
        # Core 2.0 would send this to api.openai.com with the decoy key: refused, nothing sent.
        with pytest.raises(LlmRouteRefused, match="openai"):
            llm.call_llm(model=pinned, user_content="hi")
        assert seen == []
        return
    llm.call_llm(model=pinned, user_content="hi")
    assert [r.url.host for r in seen] == [call_host]
    assert seen[0].headers["authorization"] == f"Bearer {KEYS[key_var]}"
    assert json.loads(seen[0].content)["model"]


@pytest.mark.parametrize("provider", ["anthropic", "claude-agent"])
def test_anthropic_family_without_its_key_refuses_before_any_request(
    monkeypatch, tmp_path, provider
):
    for var in (*KEYS, "OPENAI_BASE_URL", "LLM_PROVIDER", "SMARTMEMORY_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", DECOY)  # the tester's stale keychain key
    monkeypatch.setattr(llm, "_claude_agent_sdk_available", lambda: False)
    _pick(tmp_path, provider)
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", _provider_default_model(provider, {}))
    seen = []
    monkeypatch.setattr(
        openai_chat, "OpenAI", lambda **kw: seen.append(kw) or REAL_OPENAI(**kw)
    )
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        llm.call_llm(model=None, user_content="hi")
    assert seen == []


def test_unsupported_provider_warns_and_pins_nothing(caplog):
    with caplog.at_level("WARNING"):
        assert _provider_default_model("deepseek", {}) is None
    assert any(
        "deepseek" in r.getMessage() and "not supported" in r.getMessage()
        for r in caplog.records
    )
