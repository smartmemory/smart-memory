"""WRAPPER-2.0-LLM-PROVIDER-ROUTE-PORT-1: an LLM call goes only to the provider the user picked.

Repro (main's CLI-PROVIDER-ROUTE-1 on 2.0): ``llm_provider = "anthropic"`` with no
``llm_model`` pinned nothing, so core picked a model by whichever key was ambient
(Groq, then Gemini, then OpenAI's gpt-4o-mini) and sent memory and question text to a
provider the user did not choose, billed to that key.

Each row picks one provider, puts every provider's dummy key in the environment (or
one other provider's key, for the repro rows), drives one wrapper LLM path in the
process state it runs in, and asserts at the transport: httpx's real transport
records the host and answers a canned completion, non-loopback socket connects are
recorded and refused, and the Claude Agent SDK is a recording fake. Every request
must go to the picked provider's endpoint, or nowhere. Providers that the installed
core can serve must also be reached (not over-blocked). One explicit row table.
"""

from __future__ import annotations

import socket
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from smartmemory_app.config import LLM_KEY_ENV_VARS, SmartMemoryConfig, save_config

DUMMY = "dummy-not-a-real-key"
QUESTION = "Which database did we pick?"
NOTE = "We picked SQLite for Lite because it needs no server."

GROQ, OPENAI, ANTHROPIC, GEMINI = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)
ALL_KEYS = (GROQ, OPENAI, ANTHROPIC, GEMINI)

GROQ_HOST = "api.groq.com"
OPENAI_HOST = "api.openai.com"
ANTHROPIC_HOST = "api.anthropic.com"
GEMINI_HOST = "generativelanguage.googleapis.com"
LOCAL_HOST = "localhost"
SDK = "claude-agent-sdk"

OK = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"ok": true}'},
            "finish_reason": "stop",
        }
    ],
}


def _core_serves_anthropic() -> bool:
    """Core routes claude-* ids to api.anthropic.com (main core 7d792b92, not yet on 2.0)."""
    import smartmemory.utils.llm as core_llm

    return hasattr(core_llm, "ANTHROPIC_OPENAI_BASE_URL")


@pytest.fixture
def sent(monkeypatch, tmp_path):
    import smartmemory.integration.llm.llm_client as llm_client
    import smartmemory.pipeline.work_graph.spawn as core_spawn
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import crash_reporter, storage

    for name in (*LLM_KEY_ENV_VARS, "LLM_API_KEY"):
        monkeypatch.setenv(name, "")
    # The code under test writes os.environ directly: setenv first so teardown restores.
    for name in (
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_LLM_PROVIDER",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "SMARTMEMORY_CAPTURE_LLM",
        "SMARTMEMORY_CAPTURE_OFFLINE",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    # Restore what apply_runtime_config replaces in core's module.
    for name in (
        "call_openai_chat",
        "_call_claude_agent",
        "_call_bedrock_converse",
        "llm_route_available",
    ):
        monkeypatch.setattr(core_llm, name, getattr(core_llm, name))
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    monkeypatch.setattr(
        core_spawn, "spawn_worker_if_idle", core_spawn.spawn_worker_if_idle
    )
    monkeypatch.setattr(crash_reporter, "report_in_background", lambda *a, **k: None)

    seen: list[str] = []

    def http(self, request):
        seen.append(request.url.host)
        return httpx.Response(200, json=OK, request=request)

    async def ahttp(self, request):
        seen.append(request.url.host)
        return httpx.Response(200, json=OK, request=request)

    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is None or host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        seen.append(f"socket:{host}")
        raise OSError("test socket guard: no request may leave")

    def sdk_query(*args, **kwargs):
        seen.append(SDK)
        raise RuntimeError("test Claude Agent SDK: recorded, nothing sent")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahttp)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(llm_client, "_claude_agent_query", sdk_query)
    monkeypatch.setattr(llm_client, "CLAUDE_AGENT_SDK_AVAILABLE", False)
    yield seen
    storage._shutdown()


def _pick(monkeypatch, tmp_path, provider, keys, sdk, model=""):
    import smartmemory.integration.llm.llm_client as llm_client

    for name in keys:
        monkeypatch.setenv(name, DUMMY)
    monkeypatch.setattr(llm_client, "CLAUDE_AGENT_SDK_AVAILABLE", sdk)
    save_config(
        SmartMemoryConfig(
            mode="local",
            data_dir=str(tmp_path / "data"),
            llm_provider=provider,
            llm_model=model,
        )
    )


# ---------------------------------------------------------------------------
# Path drivers: every wrapper path that sends to an LLM (report ## Sites)
# ---------------------------------------------------------------------------


def _runtime():
    from smartmemory_app.storage import apply_runtime_config

    apply_runtime_config()  # viewer_server.main, worker_entry and get_memory run it


def _core_call_llm():
    """Anything in the daemon that calls core's call_llm (enrichers, lifecycle, re-extraction)."""
    import smartmemory.utils.llm as core_llm

    _runtime()
    try:
        core_llm.call_llm(user_content=QUESTION, max_output_tokens=5)
    except Exception:
        pass  # a refused or failed call is fine; the transport record decides


def _daemon_ask():
    """sm ask and the tour's ask: the daemon's /memory/ask."""
    from smartmemory_app.viewer_server import _build_app

    _runtime()
    client = TestClient(_build_app(), raise_server_exceptions=False)
    client.post("/memory/ask", json={"question": QUESTION})


def _worker_extraction():
    """The work-graph worker (worker_entry, sm worker): its LLM extraction step."""
    from smartmemory.background.extraction_worker import _run_llm_extraction

    _runtime()
    _run_llm_extraction(NOTE)


def _lesson_classify():
    """sm lifecycle reclassify and session lessons: a Groq-wired classifier."""
    from smartmemory_app.lesson_classifier import classify_lessons

    _runtime()
    try:
        classify_lessons([NOTE], {})
    except Exception:
        pass


def _capture_worker():
    """Transcript capture with SMARTMEMORY_CAPTURE_LLM=1 builds its own memory (no apply_runtime_config)."""
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import capture_worker

    import smartmemory.tools.factory as factory

    real = factory.create_lite_memory
    factory.create_lite_memory = lambda **kw: types.SimpleNamespace(**kw)
    try:
        capture_worker._memory()
    finally:
        factory.create_lite_memory = real
    try:
        core_llm.call_llm(user_content=NOTE, max_output_tokens=5)
    except Exception:
        pass


PATHS = {
    "core-call-llm": _core_call_llm,
    "daemon-ask": _daemon_ask,
    "work-graph-worker": _worker_extraction,
    "lesson-classify": _lesson_classify,
    "capture-worker": _capture_worker,
}
# Paths that send to the configured provider (the lesson and capture paths are Groq-only by design).
SENDS_TO_PICKED = {"core-call-llm", "daemon-ask", "work-graph-worker"}
GROQ_ONLY = {"lesson-classify", "capture-worker"}

# row provider -> (configured llm_provider, llm_model, Agent SDK importable,
#                  the transport it may reach, core can serve it)
PROVIDERS = {
    "groq": ("groq", "", False, GROQ_HOST, True),
    "openai": ("openai", "", False, OPENAI_HOST, True),
    "gemini": ("gemini", "", False, GEMINI_HOST, True),
    # None: served iff core routes claude-* to Anthropic (main core 7d792b92)
    "anthropic": ("anthropic", "", False, ANTHROPIC_HOST, None),
    "claude-agent": ("claude-agent", "", False, ANTHROPIC_HOST, None),  # no SDK
    "claude-agent-sdk": ("claude-agent", "", True, SDK, True),
    "ollama": ("ollama", "qwen2.5:7b", False, LOCAL_HOST, True),
    # A llama* model looks like Groq to core's extractor, which sends it to Groq
    # whenever GROQ_API_KEY is set: the guard refuses that, so extraction gets nothing.
    "ollama-llama": ("ollama", "llama3.1", False, LOCAL_HOST, False),
    "none": ("none", "", False, None, False),
}

# (id, provider, ambient keys, path). Every provider x path with every key ambient, plus
# the repro rows: anthropic with ONE other provider's key (and its own).
ROWS = [
    (f"{provider}-all-keys-{path}", provider, ALL_KEYS, path)
    for provider in PROVIDERS
    for path in PATHS
] + [
    (
        f"anthropic-{other.split('_')[0].lower()}-key-{path}",
        "anthropic",
        (ANTHROPIC, other),
        path,
    )
    for other in (GROQ, OPENAI, GEMINI)
    for path in ("core-call-llm", "daemon-ask", "work-graph-worker")
]


@pytest.mark.parametrize(
    "provider,keys,path", [r[1:] for r in ROWS], ids=[r[0] for r in ROWS]
)
def test_llm_calls_reach_only_the_picked_provider(
    sent, monkeypatch, tmp_path, provider, keys, path
):
    configured, model, sdk, allowed, served = PROVIDERS[provider]
    if path in GROQ_ONLY:
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
    _pick(monkeypatch, tmp_path, configured, keys, sdk, model)

    PATHS[path]()

    hosts = list(sent)
    assert set(hosts) <= ({allowed} - {None}), (
        f"{configured!r} picked, but a request went to {hosts}"
    )
    if served is None:
        served = _core_serves_anthropic()
    if path in GROQ_ONLY:
        served = provider == "groq"
    elif path not in SENDS_TO_PICKED:
        served = False
    if served:
        assert hosts, (
            f"{configured!r} is servable but {path} sent nothing (over-blocked)"
        )


def test_every_keyed_provider_owns_its_endpoint():
    """A provider added to the table without an endpoint owner would be refused everywhere."""
    from smartmemory_app.config import LLM_PROVIDER_KEY_ENV
    from smartmemory_app.llm_errors import _ENDPOINT_OWNER

    assert set(_ENDPOINT_OWNER) == set(LLM_PROVIDER_KEY_ENV) | {"claude-agent"}
