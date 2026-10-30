"""CLI-TOUR-LLM-ROUTE-1: every LLM-route failure is one coded, plain message.

One explicit row table over the real cases a fresh install hits (no stacked
parametrize). Each row goes through the mounted daemon app, pins the stable
``code`` and the message parts, and asserts the terminal never sees an SDK body,
a ``TypeError`` or a key value. Provider none sends nothing: no OpenAI client is
constructed, through /memory/ask or through a direct core ``call_llm``.

Provider keys are neutered for every test; no network is reached.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smartmemory_app import crash_reporter, llm_errors
from smartmemory_app.config import LLM_KEY_ENV_VARS, SmartMemoryConfig, save_config

PORT = 19016
BOGUS_OPENAI = "sk-bogus-inherited-openai-key"
BOGUS_GROQ = "gsk-bogus-groq-key"
EXTRACTIVE = {
    "answer": "x",
    "mode": "extractive",
    "synthesized": False,
    "evidence": [{"memory_type": "semantic", "item_id": "m1", "content": "c1"}],
}


class AuthenticationError(Exception):
    """The shape of the OpenAI SDK's 401: a name, a status_code and a raw body."""

    status_code = 401


RAW_401 = AuthenticationError(
    "Error code: 401 - {'error': {'message': 'Invalid API Key', 'type': 'invalid_request_error'}}"
)
RAW_TYPE_ERROR = TypeError("key_for() got an unexpected keyword argument 'step'")
RAW_NETWORK = ConnectionError("Connection error.")

# (id, llm_provider, env, call_llm raises, status, code, message parts)
ROWS = [
    (
        "none-no-key",
        "none",
        {},
        None,
        200,
        "llm_provider_none",
        [
            "No LLM is configured",
            "Nothing was sent",
            "sm setup",
            "sm config llm_provider groq",
        ],
    ),
    (
        "none-bogus-openai-key",
        "none",
        {"OPENAI_API_KEY": BOGUS_OPENAI},
        None,
        200,
        "llm_provider_none",
        ["No LLM is configured", "Nothing was sent", "sm setup"],
    ),
    (
        "groq-no-key",
        "groq",
        {},
        None,
        200,
        "llm_key_missing",
        [
            "provider groq",
            "It needs GROQ_API_KEY",
            "export GROQ_API_KEY",
            "sm restart",
            ".env files are not read",
            "sm report --send",
        ],
    ),
    (
        # case e: the only key is OpenAI's, which Groq never uses
        "groq-only-openai-key",
        "groq",
        {"OPENAI_API_KEY": BOGUS_OPENAI},
        None,
        200,
        "llm_key_missing",
        ["It needs GROQ_API_KEY", "export GROQ_API_KEY", "sm restart"],
    ),
    (
        "groq-bogus-key-401",
        "groq",
        {"GROQ_API_KEY": BOGUS_GROQ},
        RAW_401,
        502,
        "llm_key_rejected",
        ["GROQ_API_KEY", "was rejected (401)", "sm restart", "sm setup"],
    ),
    (
        "gemini-no-key",
        "gemini",
        {},
        None,
        200,
        "llm_key_missing",
        ["provider gemini", "It needs GEMINI_API_KEY", "sm restart"],
    ),
    (
        "openai-no-key",
        "openai",
        {},
        None,
        200,
        "llm_key_missing",
        ["provider openai", "It needs OPENAI_API_KEY", "sm restart"],
    ),
    (
        # the doctor bug class: a TypeError out of the route code, never shown raw
        "route-type-error",
        "groq",
        {"GROQ_API_KEY": BOGUS_GROQ},
        RAW_TYPE_ERROR,
        502,
        "llm_route_unresolved",
        [
            "could not be resolved",
            "nothing was sent",
            "sm config llm_provider",
            "sm restart",
        ],
    ),
    (
        "groq-network-error",
        "groq",
        {"GROQ_API_KEY": BOGUS_GROQ},
        RAW_NETWORK,
        502,
        "llm_request_failed",
        ["The request failed", "~/.smartmemory/daemon.log", "sm report --send"],
    ),
]
NEVER_SHOWN = (
    "Error code: 401 - {",
    "invalid_request_error",
    "TypeError",
    "AuthenticationError",
    "Traceback",
    "unexpected keyword argument",
    BOGUS_OPENAI,
    BOGUS_GROQ,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for name in (*LLM_KEY_ENV_VARS, "GEMINI_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(name, "")
    # apply_runtime_config writes os.environ directly: setenv first so the
    # teardown restores (removes) whatever the code under test set.
    for name in (
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def no_client(monkeypatch):
    """Both OpenAI constructors core can reach fail the test if they are called."""
    import openai
    import smartmemory.utils.llm_client.openai_chat as openai_chat

    built = MagicMock(side_effect=AssertionError("an OpenAI client was constructed"))
    monkeypatch.setattr(openai, "OpenAI", built)
    monkeypatch.setattr(openai_chat, "OpenAI", built, raising=False)
    return built


@pytest.fixture
def reports(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        crash_reporter,
        "report_in_background",
        lambda exc, **kw: sent.append((exc, kw)),
    )
    return sent


def _client(monkeypatch, provider, exc):
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import local_api
    from smartmemory_app.viewer_server import _build_app

    save_config(
        SmartMemoryConfig(mode="local", daemon_port=PORT, llm_provider=provider)
    )
    mem = MagicMock()
    mem.ask.return_value = dict(EXTRACTIVE)
    monkeypatch.setattr(local_api, "_get_mem", lambda: mem)
    monkeypatch.setattr(local_api, "_search_items", lambda body: [])
    monkeypatch.setattr(local_api, "_ask_relations", lambda hits: [])

    def call_llm(**_kw):
        if exc is None:
            raise AssertionError("call_llm must not run: the route is not usable")
        raise exc

    monkeypatch.setattr(core_llm, "call_llm", call_llm)
    return TestClient(
        _build_app(), base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False
    )


@pytest.mark.parametrize(
    "provider,env,exc,status,code,parts",
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_daemon_ask_route_failure_is_one_coded_plain_message(
    monkeypatch, no_client, reports, provider, env, exc, status, code, parts
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = _client(monkeypatch, provider, exc)

    response = client.post("/memory/ask", json={"question": "why that database?"})

    body = response.json()
    assert response.status_code == status, response.text
    assert body["code"] == code
    text = body.get("detail") if status == 502 else body.get("notice")
    assert isinstance(text, str), body
    for part in parts:
        assert part in text, text
    for never in NEVER_SHOWN:
        assert never not in response.text, never
    assert llm_errors.render(body) == text.strip()
    assert no_client.call_count == 0
    if status == 502:  # a failure is reported with the raw exception, not shown
        assert reports == [(exc, {"source": "daemon", "handled": True})]
    else:
        assert reports == []


def test_provider_none_sends_nothing_even_from_a_direct_core_call(
    monkeypatch, no_client
):
    """The daemon's startup hold stops core's own call_llm, not only /memory/ask."""
    import smartmemory.utils.llm as core_llm

    from smartmemory_app.storage import apply_runtime_config

    # Restore what apply_runtime_config replaces in core's module at teardown.
    for name in (
        "call_openai_chat",
        "_call_claude_agent",
        "_call_bedrock_converse",
        "llm_route_available",
    ):
        monkeypatch.setattr(core_llm, name, getattr(core_llm, name))
    monkeypatch.setenv("OPENAI_API_KEY", BOGUS_OPENAI)
    save_config(SmartMemoryConfig(mode="local", daemon_port=PORT, llm_provider="none"))

    apply_runtime_config()

    with pytest.raises(llm_errors.LlmProviderNone):
        core_llm.call_llm(user_content="why that database?", max_output_tokens=5)
    assert no_client.call_count == 0
    assert core_llm.llm_route_available()[0] is False
    failure = llm_errors.classify(llm_errors.LlmProviderNone(), "none")
    assert failure.code == "llm_provider_none"


def test_messages_name_keys_never_values():
    """Every code renders for every provider without printing a key value."""
    env = {"GROQ_API_KEY": BOGUS_GROQ, "OPENAI_API_KEY": BOGUS_OPENAI}
    for provider in ("none", "groq", "gemini", "openai", "anthropic", "deepseek"):
        for code in llm_errors.CODES:
            failure = llm_errors.LlmFailure(code, llm_errors.route_for(provider, env))
            text = failure.message() + repr(failure.payload())
            assert BOGUS_GROQ not in text and BOGUS_OPENAI not in text, (provider, code)
