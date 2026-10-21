"""Unit tests for smartmemory_app.remote_backend — DIST-LITE-5.

Tests cover the critical behaviors identified in the coverage sweep:
  - ingest() RAISES RemoteBackendError when _request() fails (surfaces, not masquerades)
  - search() RAISES RemoteBackendError when _request() fails
  - get_neighbors() normalizes response to always include "edges" key
  - recall() deduplication handles both "item_id" and "id" field names

Request-envelope tests exercise serialization through httpx.MockTransport.
Other tests isolate _request() to avoid network calls.
"""

import json
import logging
from unittest.mock import patch

import httpx
import pytest

from smartmemory_app.remote_backend import RemoteMemory


@pytest.fixture()
def remote(monkeypatch):
    """RemoteMemory instance with keyring and bootstrap suppressed."""
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"default_team_id": "t1"})
    )

    def bootstrap_get(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", bootstrap_get)
    r = RemoteMemory(api_url="https://api.example.com", team_id="t1")
    return r


# ── ingest ────────────────────────────────────────────────────────────────


def test_ingest_returns_item_id_on_success(remote):
    with patch.object(remote, "_request", return_value={"item_id": "abc-123"}):
        result = remote.ingest("hello world")
    assert result == "abc-123"


def test_ingest_raises_on_failure(remote):
    """ingest() must RAISE RemoteBackendError when the API fails — not return a fake
    'Error: ...' id that the CLI would print as if the add succeeded."""
    from smartmemory_app.remote_backend import RemoteBackendError

    with patch.object(remote, "_request", return_value={"error": "upstream timeout"}):
        with pytest.raises(RemoteBackendError, match="upstream timeout"):
            remote.ingest("hello world")


@pytest.fixture
def ingest_requests(monkeypatch):
    """Capture serialized HTTP requests without using credentials or a live service."""
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"item_id": "test_p1_remote_item"})

    transport = httpx.MockTransport(receive)

    def request(method, url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "request", request)
    return requests


@pytest.mark.parametrize(
    "phase,memory_type,origin,content",
    [
        (
            "observe",
            "episodic",
            "hook:observe",
            'Tool `Bash` called. Input: {"command": "pwd"}. Result: test_p1_result',
        ),
        (
            "distill",
            "pending",
            "lifecycle:distill",
            "User: test_p1_prompt\nAssistant: test_p1_response",
        ),
        (
            "learn",
            "episodic",
            "hook:learn",
            "Error in `Bash`: test_p1_error",
        ),
    ],
)
def test_lifecycle_ingest_delivers_producer_fields(
    remote, ingest_requests, monkeypatch, tmp_path, phase, memory_type, origin, content
):
    """Real lifecycle -> storage -> RemoteMemory -> serialized request for each phase."""
    from smartmemory_app import storage
    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "test_p1_cwd_workspace")
    monkeypatch.setattr(storage, "get_memory", lambda: remote)
    lifecycle = MemoryLifecycle(f"test_p1_{phase}", LifecycleConfig())
    if phase == "observe":
        lifecycle.observe(
            "Bash", {"command": "pwd"}, "test_p1_result", cwd=str(tmp_path)
        )
    elif phase == "distill":
        lifecycle._current_user_turn = "test_p1_prompt"
        lifecycle.distill("test_p1_response", cwd=str(tmp_path))
    else:
        lifecycle.learn("Bash", "test_p1_error", cwd=str(tmp_path))

    assert len(ingest_requests) == 1
    request = ingest_requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.example.com/memory/ingest"
    assert json.loads(request.content) == {
        "content": content,
        "context": {
            "memory_type": memory_type,
            "origin": origin,
            "workspace_id": "test_p1_cwd_workspace",
        },
    }
    # Cwd metadata is forwarded, but cannot select the hosted request scope.
    assert request.headers["X-Workspace-Id"] == "t1"
    assert request.headers["Authorization"] == "Bearer sk_test"


def test_ingest_reserved_properties_cannot_override_producer_or_request_scope(
    remote, ingest_requests, monkeypatch, caplog
):
    from smartmemory_app import storage

    reserved = {
        "memory_type",
        "node_category",
        "item_id",
        "content",
        "embedding",
        "created_at",
        "valid_from",
        "valid_to",
        "origin",
    }
    properties = {key: "forged" for key in reserved}
    properties.update(
        workspace_id="test_p1_untrusted_workspace",
        tenant_id="test_p1_untrusted_tenant",
        user_id="test_p1_untrusted_user",
        source="test_p1_source",
    )
    before = dict(properties)
    monkeypatch.setattr(storage, "get_memory", lambda: remote)

    with caplog.at_level(logging.WARNING, logger="smartmemory_app.storage"):
        result = storage.ingest(
            "test_p1_content",
            memory_type="pending",
            origin="lifecycle:distill",
            properties=properties,
        )

    assert result == "test_p1_remote_item"
    assert properties == before
    assert len(ingest_requests) == 1
    request = ingest_requests[0]
    assert json.loads(request.content) == {
        "content": "test_p1_content",
        "context": {
            "memory_type": "pending",
            "origin": "lifecycle:distill",
            "workspace_id": "test_p1_untrusted_workspace",
            "tenant_id": "test_p1_untrusted_tenant",
            "user_id": "test_p1_untrusted_user",
            "source": "test_p1_source",
        },
    }
    assert request.headers["X-Workspace-Id"] == "t1"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warnings == [
        f"Dropped reserved ingest properties: {', '.join(sorted(reserved))}"
    ]


def test_ingest_context_is_copied_and_requested_type_wins(remote, ingest_requests):
    context = {
        "memory_type": "procedural",
        "origin": "hook:learn",
        "source": "test_p1_source",
    }
    before = dict(context)
    assert (
        remote.ingest("test_p1_content", "episodic", context=context)
        == "test_p1_remote_item"
    )
    assert context == before
    assert json.loads(ingest_requests[0].content)["context"] == {
        **before,
        "memory_type": "episodic",
    }


# ── search ────────────────────────────────────────────────────────────────


def test_search_returns_list_of_dicts_on_success(remote):
    items = [
        {"item_id": "x", "content": "memory"},
        {"item_id": "y", "content": "other"},
    ]
    with patch.object(remote, "_request", return_value=items):
        result = remote.search("query")
    assert result == items


def test_search_raises_on_failure(remote):
    """search() must RAISE RemoteBackendError when the API fails — not return an
    error-dict that the CLI renders as 'No results', hiding a 30s timeout."""
    from smartmemory_app.remote_backend import RemoteBackendError

    with patch.object(remote, "_request", return_value={"error": "rate limited"}):
        with pytest.raises(RemoteBackendError, match="rate limited"):
            remote.search("query")


def test_search_returns_empty_list_on_none_response(remote):
    """search() returns [] when _request() returns None (e.g. 204 No Content)."""
    with patch.object(remote, "_request", return_value=None):
        result = remote.search("query")
    assert result == []


def test_search_unwraps_lineage_response_envelope(remote):
    """CORE-RECALL-LINEAGE-1: the service returns {"results": [...], "group_roots": {...}}.

    search() must unwrap the envelope — returning [] here silently rendered every
    remote-mode `sm search` as "No results" (found live in DEMO-WALKTHROUGH-4 spike 0.1).
    """
    items = [{"item_id": "x", "content": "memory"}]
    envelope = {"results": items, "group_roots": {}}
    with patch.object(remote, "_request", return_value=envelope):
        result = remote.search("query")
    assert result == items


def test_search_returns_empty_on_malformed_envelope(remote):
    """A dict response without a list under "results" degrades to [] (not a crash)."""
    with patch.object(remote, "_request", return_value={"results": "not-a-list"}):
        assert remote.search("query") == []


# ── get_neighbors ─────────────────────────────────────────────────────────


def test_get_neighbors_injects_edges_key_when_absent(remote):
    """Service (links.py) returns {neighbors, item_id} — no 'edges' key.

    get_neighbors() must normalize so the viewer always receives 'edges'.
    """
    service_response = {
        "neighbors": [{"item_id": "n1", "content": "neighbor"}],
        "item_id": "parent-id",
    }
    with patch.object(remote, "_request", return_value=service_response):
        result = remote.get_neighbors("parent-id")
    assert "edges" in result, (
        "get_neighbors() must inject empty 'edges' key when absent"
    )
    assert result["edges"] == []
    assert result["neighbors"][0]["item_id"] == "n1"


def test_get_neighbors_preserves_edges_when_present(remote):
    """When service does return 'edges', they must be preserved unchanged."""
    service_response = {
        "neighbors": [],
        "edges": [{"source_id": "a", "target_id": "b"}],
    }
    with patch.object(remote, "_request", return_value=service_response):
        result = remote.get_neighbors("a")
    assert len(result["edges"]) == 1


def test_get_neighbors_returns_error_shape_on_failure(remote):
    with patch.object(remote, "_request", return_value={"error": "not found"}):
        result = remote.get_neighbors("missing-id")
    assert result["neighbors"] == []
    assert result["edges"] == []
    assert "error" in result


# ── recall ────────────────────────────────────────────────────────────────


def test_recall_deduplicates_on_item_id_field(remote):
    """recall() must deduplicate items that appear in both recent and semantic results."""
    item = {"item_id": "dup-1", "content": "duplicate", "memory_type": "semantic"}
    with patch.object(remote, "search", return_value=[item]):
        result = remote.recall(cwd="/project", top_k=10)
    # item appears in both calls but must appear only once in output
    assert result.count("duplicate") == 1


def test_recall_deduplicates_on_id_field(remote):
    """recall() must also handle responses where ID field is 'id' (not 'item_id')."""
    item = {"id": "dup-2", "content": "alt id field", "memory_type": "episodic"}
    with patch.object(remote, "search", return_value=[item]):
        result = remote.recall(cwd="/project", top_k=10)
    assert result.count("alt id field") == 1


def test_recall_returns_empty_string_when_no_results(remote):
    with patch.object(remote, "search", return_value=[]):
        result = remote.recall(cwd="/project")
    assert result == ""


def test_recall_filters_zero_confidence(remote, monkeypatch):
    """Regression: confidence=0.0 must NOT be coerced to 1.0 and must be filtered."""
    monkeypatch.setenv("SMARTMEMORY_RECALL_FLOOR", "0.3")
    items = [
        {
            "item_id": "zero-conf",
            "content": "zero confidence item",
            "memory_type": "semantic",
            "confidence": 0.0,
        },
        {
            "item_id": "high-conf",
            "content": "high confidence item",
            "memory_type": "semantic",
            "confidence": 0.9,
        },
    ]
    with patch.object(remote, "search", return_value=items):
        result = remote.recall(cwd="/project", top_k=10)
    assert "zero confidence item" not in result, (
        "confidence=0.0 must be filtered by recall floor"
    )
    assert "high confidence item" in result


def test_recall_tilde_marker_on_low_confidence(remote, monkeypatch):
    """Items with confidence < 0.5 get a ~ prefix in remote recall output."""
    monkeypatch.setenv("SMARTMEMORY_RECALL_FLOOR", "0.1")
    items = [
        {
            "item_id": "low-conf",
            "content": "low confidence memory",
            "memory_type": "episodic",
            "confidence": 0.4,
        },
    ]
    with patch.object(remote, "search", return_value=items):
        result = remote.recall(cwd="/project", top_k=10)
    assert "~[" in result, "Low-confidence items should have ~ prefix"
    assert "low confidence memory" in result


def test_recall_stale_marker(remote, monkeypatch):
    """Stale items get ⚠ prefix in remote recall output."""
    monkeypatch.setenv("SMARTMEMORY_RECALL_FLOOR", "0.1")
    items = [
        {
            "item_id": "stale-1",
            "content": "stale remote memory",
            "memory_type": "semantic",
            "confidence": 0.8,
            "stale": True,
        },
    ]
    with patch.object(remote, "search", return_value=items):
        result = remote.recall(cwd="/project", top_k=10)
    assert "⚠" in result, "Stale items should have ⚠ prefix"
    assert "stale remote memory" in result


def test_recall_no_stale_marker_when_fresh(remote, monkeypatch):
    """Non-stale items have no ⚠ prefix in remote recall output."""
    monkeypatch.setenv("SMARTMEMORY_RECALL_FLOOR", "0.1")
    items = [
        {
            "item_id": "fresh-1",
            "content": "fresh remote memory",
            "memory_type": "semantic",
            "confidence": 0.8,
            "stale": False,
        },
    ]
    with patch.object(remote, "search", return_value=items):
        result = remote.recall(cwd="/project", top_k=10)
    assert "⚠" not in result
    assert "fresh remote memory" in result
