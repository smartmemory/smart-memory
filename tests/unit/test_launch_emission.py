"""LAUNCH-METRICS-1 — CLI-side emission + daemon /launch/event ingest.

Covers the wrapper half of the launch-telemetry pipeline with a mock daemon:
- ``smartmemory_app.launch_metrics.emit`` posts the right payload, rejects
  unknown event types, and honors the opt-out env var.
- The daemon ``/launch/event`` endpoint appends JSONL in local mode and
  forwards to the hosted service (authenticated) in remote mode, falling back
  to local JSONL when the forward fails.
"""
import json

import pytest
from fastapi.testclient import TestClient

from smartmemory_app import launch_metrics
from smartmemory_app.local_api import api as local_api


# ---------------------------------------------------------------------------
# CLI emitter
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestCliEmit:
    def test_emit_posts_event_to_daemon(self, monkeypatch):
        calls = {}

        import httpx

        class FakeClient:
            def __init__(self, **kwargs):
                calls["client_kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, timeout=None):
                calls["url"] = url
                calls["json"] = json
                calls["timeout"] = timeout
                return _FakeResponse(200)

        monkeypatch.setattr(httpx, "Client", FakeClient)
        monkeypatch.setattr(launch_metrics, "_daemon_url", lambda: "http://127.0.0.1:9014")

        assert launch_metrics.emit("setup.complete", {"mode": "local"}) is True
        assert calls["client_kwargs"] == {"trust_env": False}
        assert calls["url"] == "http://127.0.0.1:9014/launch/event"
        assert calls["json"] == {"event_type": "setup.complete", "props": {"mode": "local"}}
        assert calls["timeout"] == 2.0

    def test_emit_ignores_socks_proxy_env_for_daemon_post(self, monkeypatch):
        calls = {}

        import httpx

        class FakeClient:
            def __init__(self, **kwargs):
                calls["client_kwargs"] = kwargs
                if kwargs.get("trust_env") is not False:
                    raise ImportError(
                        "Using SOCKS proxy, but the 'socksio' package is not installed"
                    )

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, *args, **kwargs):
                return _FakeResponse(200)

        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
        monkeypatch.setattr(httpx, "Client", FakeClient)
        monkeypatch.setattr(launch_metrics, "_daemon_url", lambda: "http://127.0.0.1:9014")

        assert launch_metrics.emit("setup.complete", {"mode": "local"}) is True
        assert calls["client_kwargs"] == {"trust_env": False}

    def test_emit_rejects_unknown_event_type(self, monkeypatch):
        import httpx

        def fail_post(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("must not POST for unknown event type")

        monkeypatch.setattr(httpx, "post", fail_post)
        monkeypatch.setattr(launch_metrics, "_daemon_url", lambda: "http://127.0.0.1:9014")
        assert launch_metrics.emit("not.a.real.event") is False

    def test_emit_honors_disable_env(self, monkeypatch):
        monkeypatch.setenv("SMARTMEMORY_DISABLE_LAUNCH_METRICS", "1")
        assert launch_metrics.emit("setup.complete") is False

    def test_emit_returns_false_when_daemon_unreachable(self, monkeypatch):
        import httpx

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, *args, **kwargs):
                raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "Client", FakeClient)
        monkeypatch.setattr(launch_metrics, "_daemon_url", lambda: "http://127.0.0.1:9014")
        assert launch_metrics.emit("setup.complete") is False

    def test_valid_event_types_match_contract(self):
        # Contract: docs/features/LAUNCH-METRICS-1/launch-event-contract.json
        assert launch_metrics.VALID_EVENT_TYPES == frozenset({
            "install.start",
            "setup.complete",
            "mcp.install",
            "index.start",
            "index.complete",
            "recall.invoke",
            "recall.first",
            "recall.accepted",
            "decision.create",
        })


# ---------------------------------------------------------------------------
# Daemon /launch/event ingest
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    # local_api.api is itself a FastAPI app (mounted under /memory by the daemon).
    return TestClient(local_api)


class _Cfg:
    def __init__(self, mode, api_url="https://api.example.test"):
        self.mode = mode
        self.api_url = api_url


class TestDaemonIngest:
    def test_local_mode_appends_jsonl(self, client, tmp_path, monkeypatch):
        import smartmemory_app.storage as storage

        monkeypatch.setattr(storage, "_resolve_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "smartmemory_app.config.load_config", lambda: _Cfg("local"), raising=True
        )

        resp = client.post(
            "/launch/event",
            json={"event_type": "setup.complete", "props": {"mode": "local"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_type"] == "setup.complete"
        assert body["event_id"]

        lines = (tmp_path / "launch_events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "setup.complete"
        assert record["props"] == {"mode": "local"}
        assert record["event_id"] == body["event_id"]

    def test_missing_event_type_is_400(self, client):
        resp = client.post("/launch/event", json={"props": {}})
        assert resp.status_code == 400

    def test_remote_mode_forwards_to_service(self, client, tmp_path, monkeypatch):
        import httpx

        forwarded = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            forwarded["url"] = url
            forwarded["json"] = json
            forwarded["headers"] = headers
            return _FakeResponse(200, {"event_id": "svc-1", "event_type": json["event_type"]})

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(
            "smartmemory_app.config.load_config", lambda: _Cfg("remote"), raising=True
        )
        monkeypatch.setattr(
            "smartmemory_app.config.get_api_key", lambda: "test-key-123", raising=True
        )

        resp = client.post(
            "/launch/event",
            json={"event_type": "mcp.install", "props": {"client": "claude"}},
        )
        assert resp.status_code == 200
        assert resp.json()["event_id"] == "svc-1"
        assert forwarded["url"] == "https://api.example.test/memory/launch/event"
        assert forwarded["json"] == {"event_type": "mcp.install", "props": {"client": "claude"}}
        assert forwarded["headers"]["Authorization"] == "Bearer test-key-123"
        # Forwarded events must NOT also be written locally.
        assert not (tmp_path / "launch_events.jsonl").exists()

    def test_remote_forward_failure_falls_back_to_local_jsonl(
        self, client, tmp_path, monkeypatch
    ):
        import httpx
        import smartmemory_app.storage as storage

        def boom(*a, **k):
            raise httpx.ConnectError("service down")

        monkeypatch.setattr(httpx, "post", boom)
        monkeypatch.setattr(storage, "_resolve_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "smartmemory_app.config.load_config", lambda: _Cfg("remote"), raising=True
        )
        monkeypatch.setattr(
            "smartmemory_app.config.get_api_key", lambda: "test-key-123", raising=True
        )

        resp = client.post(
            "/launch/event", json={"event_type": "index.complete", "props": {"repo": "r"}}
        )
        # No-silent-degradation: the event still lands (locally), request succeeds.
        assert resp.status_code == 200
        lines = (tmp_path / "launch_events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event_type"] == "index.complete"
