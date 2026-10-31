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

F1 (review r1 H1, M1, fold-ins): the guard is a normalized-host allowlist, so a second
table sets ``OPENAI_BASE_URL`` (every row of the first table clears it explicitly):
non-vendor, look-alike, regional and Vertex hosts are refused with nothing sent, the
documented Groq base URL and a LAN ``llm_base_url`` are still served, and the bearer on
the wire is mapped back to a key NAME (distinct dummy values) and must be the picked
provider's own key, or the local placeholder for a local server.
"""

from __future__ import annotations

import socket
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from smartmemory_app.config import (
    LLM_KEY_ENV_VARS,
    LOCAL_PLACEHOLDER_KEY,
    SmartMemoryConfig,
    save_config,
)

PLACEHOLDER = "local placeholder"
BASIC = "basic auth from the URL userinfo"
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
LAN_HOST = "192.168.1.20"
SDK = "claude-agent-sdk"


def _dummy(name: str) -> str:
    """A distinct dummy value per key name, so the bearer on the wire maps back to a NAME."""
    return f"dummy-{name.lower()}"


BEARER_NAMES = {f"Bearer {_dummy(n)}": n for n in (*LLM_KEY_ENV_VARS, "LLM_API_KEY")}
BEARER_NAMES[f"Bearer {LOCAL_PLACEHOLDER_KEY}"] = PLACEHOLDER


def _host(raw: str) -> str:
    """Test-side normalization of a recorded host (lowercase, one trailing dot dropped)."""
    raw = raw.lower()
    return raw[:-1] if raw.endswith(".") else raw


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

    seen = Sent()

    def record(request):
        seen.append(_host(request.url.host))
        auth = request.headers.get("authorization") or ""
        if auth.startswith("Basic "):
            name = BASIC  # httpx sends a base URL's userinfo as Basic auth: no key on the wire
        else:
            name = BEARER_NAMES.get(auth, "unknown" if auth else "none")
        seen.bearers.append(name)

    def http(self, request):
        record(request)
        return httpx.Response(200, json=OK, request=request)

    async def ahttp(self, request):
        record(request)
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


class Sent(list):
    """Hosts the transport saw (normalized), with the bearer's key NAME per request."""

    def __init__(self):
        super().__init__()
        self.bearers: list[str] = []


def _pick(
    monkeypatch,
    tmp_path,
    provider,
    keys,
    sdk,
    model="",
    *,
    base_url,
    llm_base_url="",
):
    """Save the config and set the env for one row. ``base_url`` None clears OPENAI_BASE_URL."""
    import smartmemory.integration.llm.llm_client as llm_client

    for name in keys:
        monkeypatch.setenv(name, _dummy(name))
    # setenv first so teardown restores it: the code under test writes it directly.
    monkeypatch.setenv("OPENAI_BASE_URL", base_url or "")
    if base_url is None:
        monkeypatch.delenv("OPENAI_BASE_URL")
    monkeypatch.setattr(llm_client, "CLAUDE_AGENT_SDK_AVAILABLE", sdk)
    save_config(
        SmartMemoryConfig(
            mode="local",
            data_dir=str(tmp_path / "data"),
            llm_provider=provider,
            llm_model=model,
            llm_base_url=llm_base_url,
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
    return client.post("/memory/ask", json={"question": QUESTION})


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
#                  the transport it may reach, core can serve it, the bearer it may carry)
PROVIDERS = {
    "groq": ("groq", "", False, GROQ_HOST, True, GROQ),
    "openai": ("openai", "", False, OPENAI_HOST, True, OPENAI),
    "gemini": ("gemini", "", False, GEMINI_HOST, True, GEMINI),
    # None: served iff core routes claude-* to Anthropic (main core 7d792b92)
    "anthropic": ("anthropic", "", False, ANTHROPIC_HOST, None, ANTHROPIC),
    "claude-agent": (
        "claude-agent",
        "",
        False,
        ANTHROPIC_HOST,
        None,
        ANTHROPIC,
    ),  # no SDK
    "claude-agent-sdk": ("claude-agent", "", True, SDK, True, None),
    "ollama": ("ollama", "qwen2.5:7b", False, LOCAL_HOST, True, PLACEHOLDER),
    # A llama* model looks like Groq to core's extractor, which sends it to Groq
    # whenever GROQ_API_KEY is set: the guard refuses that, so extraction gets nothing.
    "ollama-llama": ("ollama", "llama3.1", False, LOCAL_HOST, False, PLACEHOLDER),
    "none": ("none", "", False, None, False, None),
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
    sent, caplog, monkeypatch, tmp_path, provider, keys, path
):
    configured, model, sdk, allowed, served, bearer = PROVIDERS[provider]
    if path in GROQ_ONLY:
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
    _pick(monkeypatch, tmp_path, configured, keys, sdk, model, base_url=None)

    with caplog.at_level("WARNING", logger="smartmemory_app.storage"):
        PATHS[path]()

    hosts = list(sent)
    assert set(hosts) <= ({allowed} - {None}), (
        f"{configured!r} picked, but a request went to {hosts}"
    )
    assert set(sent.bearers) <= {bearer}, (
        f"{configured!r} picked, but a request carried {sent.bearers}"
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
    if (
        configured in ("anthropic", "claude-agent")
        and not sdk
        and path in SENDS_TO_PICKED
        and not _core_serves_anthropic()
    ):
        # The pinned Claude model is what core tried to send (to its OpenAI client, which
        # the guard refused), never a model chosen by whichever other key is ambient.
        refused = [
            r.args[1]
            for r in caplog.records
            if r.getMessage().startswith("LLM call refused before sending")
            and len(r.args) > 1
        ]
        assert refused and set(refused) == {OPENAI_HOST}, (
            f"{configured!r} picked: refused calls were headed for {refused}"
        )


def test_every_cloud_provider_lists_its_hosts():
    """A provider added to the table without hosts would be refused everywhere."""
    from smartmemory_app.config import LLM_PROVIDER_KEY_ENV, LLM_PROVIDER_TABLE

    cloud = set(LLM_PROVIDER_KEY_ENV) | {"claude-agent"}
    assert {p for p, row in LLM_PROVIDER_TABLE.items() if row[2]} == cloud


# ---------------------------------------------------------------------------
# F1: OPENAI_BASE_URL set. The guard is a normalized-host allowlist (review r1 H1).
# ---------------------------------------------------------------------------

# The reviewer's probe hosts (review-r1.md H1): none is owned by groq, anthropic, ollama or
# an unknown provider, so every one is refused for each of them with nothing sent.
LEAK_BASES = {
    "non-vendor": "https://evil.example/v1",
    "openai-trailing-dot": "https://api.openai.com./v1",
    "openai-us": "https://us.api.openai.com/v1",
    "openai-eu": "https://eu.api.openai.com/v1",
    "vertex": "https://aiplatform.googleapis.com/v1",
    "openai-upper": "https://API.OPENAI.COM/v1",
    "openai-port": "https://api.openai.com:443/v1",
    "openai-userinfo": "https://u:p@api.openai.com/v1",
    "openai-http": "http://api.openai.com/v1",
}
# row provider -> (llm_provider, llm_model)
LEAK_PROVIDERS = {
    "groq": ("groq", ""),
    "anthropic": ("anthropic", ""),
    "ollama": ("ollama", "qwen2.5:7b"),
    "unknown": ("deepseek", ""),
}

# (id, llm_provider, llm_model, OPENAI_BASE_URL or None, llm_base_url,
#  the host it must reach or None for refused, the bearer it must carry)
BASE_URL_ROWS = [
    (f"{p}-{b}", *LEAK_PROVIDERS[p], LEAK_BASES[b], "", None, None)
    for p in LEAK_PROVIDERS
    for b in LEAK_BASES
] + [
    # The reviewer's two base-less rows: an unknown provider is refused, groq is served.
    ("unknown-no-base", "deepseek", "", None, "", None, None),
    ("groq-no-base-control", "groq", "", None, "", GROQ_HOST, GROQ),
    # Over-block: core's documented standing config and its normalized variants.
    (
        "groq-documented-base",
        "groq",
        "",
        "https://api.groq.com/openai/v1",
        "",
        GROQ_HOST,
        GROQ,
    ),
    ("groq-upper", "groq", "", "https://API.GROQ.COM/openai/v1", "", GROQ_HOST, GROQ),
    (
        "groq-trailing-dot",
        "groq",
        "",
        "https://api.groq.com./openai/v1",
        "",
        GROQ_HOST,
        GROQ,
    ),
    (
        "groq-port-443",
        "groq",
        "",
        "https://api.groq.com:443/openai/v1",
        "",
        GROQ_HOST,
        GROQ,
    ),
    (
        "groq-explicit-port",
        "groq",
        "",
        "https://api.groq.com:8443/openai/v1",
        "",
        GROQ_HOST,
        GROQ,
    ),
    (
        "groq-userinfo",
        "groq",
        "",
        "https://u@api.groq.com/openai/v1",
        "",
        GROQ_HOST,
        BASIC,
    ),
    (
        "openai-eu-served",
        "openai",
        "",
        "https://eu.api.openai.com/v1",
        "",
        "eu.api.openai.com",
        OPENAI,
    ),
    # Look-alikes of an allowed host: no prefix, suffix or substring match.
    (
        "groq-lookalike-suffix",
        "groq",
        "",
        "https://api.groq.com.evil.example/v1",
        "",
        None,
        None,
    ),
    (
        "groq-lookalike-prefix",
        "groq",
        "",
        "https://evil-api.groq.com/v1",
        "",
        None,
        None,
    ),
    ("groq-lookalike-extended", "groq", "", "https://api.groq.comx/v1", "", None, None),
    (
        "groq-lookalike-path",
        "groq",
        "",
        "https://evil.example/api.groq.com",
        "",
        None,
        None,
    ),
    (
        "groq-lookalike-userinfo",
        "groq",
        "",
        "https://api.groq.com@evil.example/v1",
        "",
        None,
        None,
    ),
    # A cloud provider never gets loopback (its key must not reach a local listener).
    ("groq-loopback", "groq", "", "http://localhost:4000/v1", "", None, None),
    # Local servers: a LAN llm_base_url is served with the placeholder key (step 2, 3).
    (
        "ollama-lan-base",
        "ollama",
        "qwen2.5:7b",
        None,
        f"http://{LAN_HOST}:11434/v1",
        LAN_HOST,
        PLACEHOLDER,
    ),
    ("ollama-default-base", "ollama", "qwen2.5:7b", None, "", LOCAL_HOST, PLACEHOLDER),
    # A local provider does not inherit a remote OPENAI_BASE_URL silently: refused (step 4).
    (
        "ollama-lan-base-remote-env",
        "ollama",
        "qwen2.5:7b",
        "https://evil.example/v1",
        f"http://{LAN_HOST}:11434/v1",
        None,
        None,
    ),
    (
        "unknown-loopback",
        "deepseek",
        "gpt-4o-mini",
        "http://127.0.0.1:8080/v1",
        "",
        "127.0.0.1",
        PLACEHOLDER,
    ),
]


@pytest.mark.parametrize(
    "provider,model,base_url,llm_base_url,reach,bearer",
    [r[1:] for r in BASE_URL_ROWS],
    ids=[r[0] for r in BASE_URL_ROWS],
)
def test_base_url_reaches_only_an_allowed_host(
    sent,
    caplog,
    monkeypatch,
    tmp_path,
    provider,
    model,
    base_url,
    llm_base_url,
    reach,
    bearer,
):
    _pick(
        monkeypatch,
        tmp_path,
        provider,
        ALL_KEYS,
        False,
        model,
        base_url=base_url,
        llm_base_url=llm_base_url,
    )
    with caplog.at_level("WARNING", logger="smartmemory_app.storage"):
        _core_call_llm()

    refusals = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("LLM call refused before sending")
    ]
    if reach is None:
        assert list(sent) == [], f"{provider!r} with {base_url!r}: sent to {list(sent)}"
        assert refusals, f"{provider!r} with {base_url!r}: no refusal was logged"
        if base_url:
            assert all("OPENAI_BASE_URL" in m and "unset it" in m for m in refusals), (
                refusals
            )
    else:
        assert list(sent) and set(sent) == {reach}, (
            f"{provider!r} with {base_url!r}: expected {reach}, sent to {list(sent)}"
        )
        assert set(sent.bearers) == {bearer}, (
            f"{provider!r} with {base_url!r}: bearer {sent.bearers}"
        )
        assert not refusals, refusals


# (id, llm_provider, endpoint, llm_base_url, refused host or None). The comparison is exact
# string equality of normalized hosts: no prefix, suffix or substring match.
HOST_ROWS = [
    ("groq-canonical", "groq", "https://api.groq.com/openai/v1", "", None),
    ("groq-upper", "groq", "https://API.Groq.COM/openai/v1", "", None),
    ("groq-trailing-dot", "groq", "https://api.groq.com./openai/v1", "", None),
    (
        "groq-two-trailing-dots",
        "groq",
        "https://api.groq.com../openai/v1",
        "",
        "api.groq.com.",
    ),
    ("groq-port", "groq", "https://api.groq.com:443/v1", "", None),
    ("groq-userinfo", "groq", "https://user:pw@api.groq.com/v1", "", None),
    (
        "groq-suffix",
        "groq",
        "https://api.groq.com.evil.example/v1",
        "",
        "api.groq.com.evil.example",
    ),
    ("groq-prefix", "groq", "https://evil-api.groq.com/v1", "", "evil-api.groq.com"),
    ("groq-subdomain", "groq", "https://x.api.groq.com/v1", "", "x.api.groq.com"),
    ("groq-extended", "groq", "https://api.groq.comx/v1", "", "api.groq.comx"),
    ("groq-path", "groq", "https://evil.example/api.groq.com", "", "evil.example"),
    (
        "groq-userinfo-trick",
        "groq",
        "https://api.groq.com@evil.example/v1",
        "",
        "evil.example",
    ),
    ("groq-no-scheme", "groq", "api.groq.com/openai/v1", "", "(no host)"),
    ("groq-openai-host", "groq", "https://api.openai.com/v1", "", "api.openai.com"),
    ("groq-loopback", "groq", "http://127.0.0.1:4000/v1", "", "127.0.0.1"),
    ("openai-us", "openai", "https://us.api.openai.com/v1", "", None),
    (
        "gemini-canonical",
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "",
        None,
    ),
    (
        "gemini-vertex",
        "gemini",
        "https://aiplatform.googleapis.com/v1",
        "",
        "aiplatform.googleapis.com",
    ),
    ("ollama-loopback-v6", "ollama", "http://[::1]:11434/v1", "", None),
    (
        "ollama-lan-configured",
        "ollama",
        "http://192.168.1.20:11434/v1",
        "http://192.168.1.20:11434/v1",
        None,
    ),
    (
        "ollama-lan-unconfigured",
        "ollama",
        "http://192.168.1.20:11434/v1",
        "",
        "192.168.1.20",
    ),
    (
        "lmstudio-other-lan",
        "lmstudio",
        "http://10.0.0.9:1234/v1",
        "http://10.0.0.8:1234/v1",
        "10.0.0.9",
    ),
    ("unknown-loopback", "deepseek", "http://localhost:8080/v1", "", None),
    ("unknown-vendor", "deepseek", "https://api.openai.com/v1", "", "api.openai.com"),
    ("unreadable-config", "", "https://api.groq.com/openai/v1", "", "api.groq.com"),
    ("none-left-to-the-none-block", "none", "https://evil.example/v1", "", None),
]


@pytest.mark.parametrize(
    "provider,endpoint,llm_base_url,refused",
    [r[1:] for r in HOST_ROWS],
    ids=[r[0] for r in HOST_ROWS],
)
def test_refused_host_compares_normalized_hosts_exactly(
    provider, endpoint, llm_base_url, refused
):
    from smartmemory_app.llm_errors import refused_host

    assert refused_host(provider, endpoint, llm_base_url) == refused


# ---------------------------------------------------------------------------
# F1 M1: the ask refusal for the Anthropic family on a core without the Anthropic route.
# ---------------------------------------------------------------------------

M1_TEXT = (
    "cannot be served by this install (core has no Anthropic route): install the Claude "
    "Agent SDK (claude-agent) or pick another provider with sm setup"
)
# (id, llm_provider)
ASK_REFUSAL_ROWS = [
    ("anthropic", "anthropic"),
    ("claude-agent-no-sdk", "claude-agent"),
]


@pytest.mark.parametrize(
    "provider", [r[1] for r in ASK_REFUSAL_ROWS], ids=[r[0] for r in ASK_REFUSAL_ROWS]
)
def test_anthropic_family_ask_refusal_says_what_to_do(
    sent, monkeypatch, tmp_path, provider
):
    _pick(monkeypatch, tmp_path, provider, ALL_KEYS, False, base_url=None)

    response = _daemon_ask()

    if _core_serves_anthropic():
        assert response.status_code == 200, response.text
        assert set(sent) == {ANTHROPIC_HOST} and set(sent.bearers) == {ANTHROPIC}
        return
    body = response.json()
    assert response.status_code == 502 and body["code"] == "llm_route_unresolved", body
    assert f"llm_provider {provider} {M1_TEXT}" in body["detail"], body["detail"]
    assert "api.anthropic.com" not in body["detail"], body["detail"]
    assert "api.anthropic.com" not in str(body["llm"]["endpoint"]), body["llm"]
    assert list(sent) == []


# ---------------------------------------------------------------------------
# F1 fold-in: an explicit llm_provider none beats a leftover llm_model / llm_base_url.
# ---------------------------------------------------------------------------


def _write_raw_config(tmp_path, local: dict) -> None:
    import tomli_w

    from smartmemory_app.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"mode": "local"}
    data = {"smartmemory": body, "local": {"data_dir": str(tmp_path / "data"), **local}}
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


# (id, [local] table written, path, requests expected)
NONE_ROWS = [
    (
        "explicit-none-model-core",
        {"llm_provider": "none", "llm_model": "openai/gpt-oss-120b"},
        "core-call-llm",
        False,
    ),
    (
        "explicit-none-model-ask",
        {"llm_provider": "none", "llm_model": "openai/gpt-oss-120b"},
        "daemon-ask",
        False,
    ),
    (
        "explicit-none-base-url-core",
        {"llm_provider": "none", "llm_base_url": "https://api.groq.com/openai/v1"},
        "core-call-llm",
        False,
    ),
    (
        "explicit-none-base-url-ask",
        {"llm_provider": "none", "llm_base_url": "https://api.groq.com/openai/v1"},
        "daemon-ask",
        False,
    ),
    # The unset default (no llm_provider in the file): an explicit model still turns the LLM on.
    (
        "default-none-model-core",
        {"llm_model": "openai/gpt-oss-120b"},
        "core-call-llm",
        True,
    ),
]


@pytest.mark.parametrize(
    "local,path,sends", [r[1:] for r in NONE_ROWS], ids=[r[0] for r in NONE_ROWS]
)
def test_explicit_none_beats_a_leftover_model(
    sent, monkeypatch, tmp_path, local, path, sends
):
    from smartmemory_app.config import llm_off, load_config

    for name in ALL_KEYS:
        monkeypatch.setenv(name, _dummy(name))
    _write_raw_config(tmp_path, local)
    assert llm_off(load_config()) is not sends

    PATHS[path]()

    if sends:
        assert set(sent) == {GROQ_HOST}, list(sent)
    else:
        assert list(sent) == [], list(sent)


def test_sm_config_llm_provider_none_clears_model_and_base_url(sent, tmp_path):
    import tomllib

    from click.testing import CliRunner

    from smartmemory_app.cli import cli
    from smartmemory_app.config import config_path

    _write_raw_config(
        tmp_path,
        {
            "llm_provider": "groq",
            "llm_model": "openai/gpt-oss-120b",
            "llm_base_url": "https://api.groq.com/openai/v1",
        },
    )

    result = CliRunner().invoke(cli, ["config", "llm_provider", "none"])

    assert result.exit_code == 0, result.output
    saved = tomllib.loads(config_path().read_text(encoding="utf-8"))["local"]
    assert saved["llm_provider"] == "none"
    assert not saved.get("llm_model") and not saved.get("llm_base_url"), saved
