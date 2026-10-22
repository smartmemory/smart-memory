"""TOUR-ASK-502-1: every ask failure shows its reason, is reported, and the
configured provider is honoured even when the Claude Agent SDK is importable.

Provider keys are neutered (``""``) for every test; no network is reached.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from smartmemory_app import crash_reporter, tour
from smartmemory_app.config import LLM_KEY_ENV_VARS, SmartMemoryConfig, save_config

PORT = 19015


@pytest.fixture(autouse=True)
def _neutered_keys(monkeypatch, tmp_path):
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


@pytest.fixture()
def reports(monkeypatch):
    """Record what would be reported, instead of sending it."""
    sent: list[tuple] = []
    monkeypatch.setattr(
        crash_reporter,
        "report_in_background",
        lambda exc, **kw: sent.append(("exception", exc, kw)),
    )
    monkeypatch.setattr(
        crash_reporter,
        "report_event",
        lambda event, props, **kw: sent.append(("event", event, props)),
    )
    return sent


# ---------------------------------------------------------------------------
# Daemon /memory/ask through the MOUNTED app (the parent's handlers never fire)
# ---------------------------------------------------------------------------


def _raise(exc):
    def call_llm(**_kw):
        raise exc

    return call_llm


ASK_ROWS = [
    # (id, call_llm, search raises, status, detail fragment, reported source, handled, raw reported)
    # "raw reported" is True when the report carries the raw exception (coded path), False when it is an HTTPException.
    # CLI-TOUR-LLM-ROUTE-1: a provider failure is a coded plain message; the raw
    # exception is what gets reported and never reaches the terminal.
    (
        "provider_error",
        _raise(RuntimeError("Not logged in · Please run /login")),
        None,
        502,
        "The request failed. The full error is in ~/.smartmemory/daemon.log.",
        "daemon",
        True,
        True,
    ),
    (
        "empty_answer",
        lambda **_kw: ("model", "   "),
        None,
        502,
        "The configured LLM returned no answer",
        "daemon",
        True,
        False,
    ),
    (
        "missing_openai",
        _raise(ImportError("openai package is required")),
        None,
        502,
        "Python dependency 'openai' is missing",
        "daemon",
        True,
        False,
    ),
    (
        "unhandled_route_error",
        lambda **_kw: ("model", "unused"),
        KeyError("boom"),
        500,
        None,
        "asgi",
        False,
        False,
    ),
]


@pytest.mark.parametrize(
    "call_llm,search_error,status,detail,source,handled,raw_reported",
    [row[1:] for row in ASK_ROWS],
    ids=[row[0] for row in ASK_ROWS],
)
def test_daemon_ask_failure_shows_detail_and_reports(
    monkeypatch,
    reports,
    call_llm,
    search_error,
    status,
    detail,
    source,
    handled,
    raw_reported,
):
    monkeypatch.setenv("GROQ_API_KEY", "bogus-not-a-real-key")
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import local_api
    from smartmemory_app.viewer_server import _build_app

    save_config(SmartMemoryConfig(mode="local", daemon_port=PORT, llm_provider="groq"))
    monkeypatch.setattr(local_api, "llm_key_present", lambda: True)

    def search(_body):
        if search_error is not None:
            raise search_error
        return []

    monkeypatch.setattr(local_api, "_search_items", search)
    monkeypatch.setattr(local_api, "_ask_relations", lambda hits: [])
    monkeypatch.setattr(core_llm, "call_llm", call_llm)

    client = TestClient(
        _build_app(), base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False
    )
    response = client.post("/memory/ask", json={"question": "why that database?"})

    assert response.status_code == status
    if detail is not None:
        assert detail in response.json()["detail"]
    assert len(reports) == 1, reports
    kind, exc, kwargs = reports[0]
    assert kind == "exception"
    assert kwargs["source"] == source
    assert kwargs.get("handled", False) is handled
    if handled and not raw_reported:
        assert exc.status_code == status
    if raw_reported:  # the coded path reports the raw exception, never shows it
        assert isinstance(exc, RuntimeError) and "Not logged in" in str(exc)
        body = response.json()
        assert body["code"] == "llm_request_failed"
        assert (
            "Not logged in" not in response.text and "RuntimeError" not in response.text
        )


HANDLED_STATUS_ROWS = [
    # (status, reported): 4xx are user errors, 501 is a by-design "not available"
    (404, False),
    (501, False),
    (502, True),
    (503, True),  # e.g. "SmartMemory not configured"
]


@pytest.mark.parametrize("status,reported", HANDLED_STATUS_ROWS)
def test_handled_http_errors_report_only_failures(
    monkeypatch, reports, status, reported
):
    from fastapi import HTTPException

    from smartmemory_app import local_api
    from smartmemory_app.viewer_server import _build_app

    save_config(SmartMemoryConfig(mode="local", daemon_port=PORT))

    def failing_item(memory_id: str):
        raise HTTPException(status_code=status, detail=f"route said {status}")

    # Swap the endpoint the route already dispatches to; the route table is untouched.
    route = next(
        r for r in local_api.api.routes if getattr(r, "name", "") == "get_memory_item"
    )
    monkeypatch.setattr(route, "endpoint", failing_item)
    monkeypatch.setattr(route.dependant, "call", failing_item)

    client = TestClient(_build_app(), base_url=f"http://127.0.0.1:{PORT}")
    response = client.get("/memory/some-id")

    assert response.status_code == status
    assert response.json()["detail"] == f"route said {status}"
    assert [r[2] for r in reports] == (
        [{"source": "daemon", "handled": True}] if reported else []
    )


# ---------------------------------------------------------------------------
# Tour: the ask step shows the server detail and reports the degradation
# ---------------------------------------------------------------------------

TOUR_ROWS = [
    # (id, status, body, shown on screen, report kind)
    (
        "detail_502",
        502,
        {"detail": "Unable to answer with the configured LLM: RuntimeError: x"},
        "(ask unavailable: HTTP 502: Unable to answer with the configured LLM: RuntimeError: x)",
        "exception",
    ),
    (
        "plain_text_500",
        500,
        "boom text",
        "(ask unavailable: HTTP 500: boom text)",
        "exception",
    ),
    (
        "empty_503",
        503,
        "",
        "(ask unavailable: HTTP 503: Service Unavailable)",
        "exception",
    ),
    (
        "list_detail_422",
        422,
        {"detail": [{"msg": "field required"}]},
        "(ask unavailable: HTTP 422: " + json.dumps([{"msg": "field required"}]) + ")",
        "exception",
    ),
    ("no_answer_200", 200, {"answer": "  "}, "(ask returned no answer)", "event"),
]


def _tour_client(status: int, body) -> tour.TourHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    client = tour.TourHttpClient("http://tour.test")
    client._client = httpx.Client(
        base_url="http://tour.test", transport=httpx.MockTransport(handler)
    )
    return client


@pytest.mark.parametrize(
    "status,body,shown,report_kind",
    [row[1:] for row in TOUR_ROWS],
    ids=[row[0] for row in TOUR_ROWS],
)
def test_tour_ask_shows_server_detail_and_reports(
    tmp_path, reports, status, body, shown, report_kind
):
    driver = tour.TourArcDriver(
        client=_tour_client(status, body), cwd=tmp_path, pace_seconds=0
    )

    assert driver._ask_answer(tour.ASK_QUESTION) == shown
    assert len(reports) == 1, reports
    kind, subject, detail = reports[0]
    assert kind == report_kind
    if kind == "exception":
        assert isinstance(subject, tour.TourRequestError)
        assert isinstance(subject, httpx.HTTPStatusError)  # old handlers still match
        assert detail == {"source": "tour", "handled": True}
    else:
        assert (subject, detail) == (
            "tour_degraded",
            {"tour_step": "ask", "reason": "ask returned no answer"},
        )


class _EmptyStoreClient:
    """The daemon accepted the facts but returns none of them."""

    def ingest(self, content: str, memory_type: str = "semantic") -> dict:
        return {"item_id": "item-1"}

    def search(self, query: str, top_k: int = 5) -> dict:
        return {"items": []}

    def recall(self, cwd=None, top_k: int = 8, query=None) -> dict:
        return {"context": ""}


def test_tour_seeded_content_degradation_reports(tmp_path, reports):
    driver = tour.TourArcDriver(
        client=_EmptyStoreClient(), cwd=tmp_path, pace_seconds=0
    )

    result = driver.run_default_arc(include_claude_import=False)

    assert result.success is False
    events = [r for r in reports if r[0] == "event"]
    assert len(events) == 1
    _, name, props = events[0]
    assert name == "tour_degraded"
    assert props["tour_step"] == "seeded_content"
    assert props["reason"] == result.degraded_reason


def test_tour_run_failure_reports(reports):
    failure = RuntimeError("daemon never came up")
    calls: list = []

    class Runner:
        def run(self, emit):
            raise failure

    fake_app = types.SimpleNamespace(
        runner=Runner(),
        current_step=2,
        call_from_thread=lambda fn, *a: calls.append(fn),
        _show_event=object(),
        _mark_finished=object(),
        _finish=object(),
        exit=object(),
    )

    tour.TourApp._run_tour.__wrapped__(fake_app)

    assert reports == [("exception", failure, {"source": "tour", "handled": True})]
    assert fake_app._show_event in calls  # the failure is still shown


# ---------------------------------------------------------------------------
# Reporting stays behind crash_reporter.enabled()
# ---------------------------------------------------------------------------

GATE_ROWS = [
    # (id, crash reports enabled, report sent)
    ("disabled", False, False),
    ("enabled", True, True),
]


@pytest.mark.parametrize(
    "is_enabled,sent", [row[1:] for row in GATE_ROWS], ids=[row[0] for row in GATE_ROWS]
)
def test_tour_degradation_report_is_gated(monkeypatch, is_enabled, sent):
    captured: list = []
    monkeypatch.setattr(crash_reporter, "enabled", lambda: is_enabled)
    monkeypatch.setattr(
        crash_reporter, "_capture", lambda *a, **kw: captured.append(a[0])
    )

    tour._report_degraded("ask", "ask returned no answer")

    assert captured == (["tour_degraded"] if sent else [])


# ---------------------------------------------------------------------------
# The configured provider outranks an importable Claude Agent SDK
# ---------------------------------------------------------------------------

PROVIDER_ROWS = [
    # (provider, config llm_model, OPENAI_MODEL env, expected SMARTMEMORY_LLM_MODEL)
    ("groq", "", None, "openai/gpt-oss-120b"),
    ("gemini", "", None, "gemini/gemini-2.0-flash"),
    ("openai", "", None, "gpt-4o-mini"),
    ("openai", "", "gpt-4.1-mini", "gpt-4.1-mini"),
    ("groq", "llama-3.3-70b-versatile", None, "llama-3.3-70b-versatile"),
    # claude-agent / anthropic / deepseek come from LLM_PROVIDER_TABLE (CLI-PROVIDER-ROUTE-1):
    # claude-agent pins the SDK alias, anthropic pins a claude-* id that core now routes to
    # api.anthropic.com with ANTHROPIC_API_KEY, deepseek is not a supported provider (no pin).
    ("claude-agent", "", None, "sonnet"),
    ("none", "", None, None),
    ("anthropic", "", None, "claude-haiku-4-5-20251001"),
    ("deepseek", "", None, None),
]


@pytest.mark.parametrize("provider,llm_model,openai_model,expected", PROVIDER_ROWS)
def test_configured_provider_pins_model_over_agent_sdk(
    monkeypatch, provider, llm_model, openai_model, expected
):
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import storage

    monkeypatch.setattr(
        storage,
        "load_config",
        lambda: SmartMemoryConfig(
            mode="local", llm_provider=provider, llm_model=llm_model
        ),
    )
    if openai_model:
        monkeypatch.setenv("OPENAI_MODEL", openai_model)
    monkeypatch.setattr(core_llm, "_claude_agent_sdk_available", lambda: True)
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")

    storage.apply_runtime_config()

    import os

    assert os.environ.get("SMARTMEMORY_LLM_MODEL") == expected
    assert core_llm.get_default_model() == (expected or "sonnet")


def test_explicit_model_env_still_wins(monkeypatch):
    from smartmemory_app import storage

    monkeypatch.setattr(
        storage,
        "load_config",
        lambda: SmartMemoryConfig(mode="local", llm_provider="groq"),
    )
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", "my-model")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")

    storage.apply_runtime_config()

    import os

    assert os.environ["SMARTMEMORY_LLM_MODEL"] == "my-model"
