"""Offline transport coverage for setup telemetry routing and failure levels."""

import json
import logging
import threading
import time

import httpx
import pytest

from smartmemory_app import config, launch_metrics


@pytest.fixture(autouse=True)
def isolated_launch_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.delenv("SMARTMEMORY_DISABLE_LAUNCH_METRICS", raising=False)
    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    config.save_config(config.SmartMemoryConfig(mode="local", daemon_port=19017))


def install_transport(monkeypatch, handler):
    real_client = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return options


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
def test_launch_metrics_unreachable_daemon_is_debug(monkeypatch, caplog, error):
    def fail(request):
        raise error("unreachable", request=request)

    install_transport(monkeypatch, fail)
    with caplog.at_level(logging.DEBUG, logger=launch_metrics.__name__):
        assert launch_metrics.emit("setup.complete") is False
    records = [r for r in caplog.records if r.name == launch_metrics.__name__]
    assert any(
        r.levelno == logging.DEBUG and "setup.complete" in r.message for r in records
    )
    assert not any(r.levelno >= logging.WARNING for r in records)


@pytest.mark.parametrize("status", [400, 503])
def test_launch_metrics_reachable_non_2xx_warns(monkeypatch, caplog, status):
    install_transport(monkeypatch, lambda request: httpx.Response(status))
    assert launch_metrics.emit("setup.complete") is False
    assert any(
        r.levelno == logging.WARNING
        and "setup.complete" in r.message
        and str(status) in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("error", [httpx.ReadTimeout, RuntimeError])
def test_launch_metrics_unexpected_error_warns(monkeypatch, caplog, error):
    def fail(request):
        raise error("unexpected")

    install_transport(monkeypatch, fail)
    assert launch_metrics.emit("setup.complete") is False
    assert any(
        r.levelno == logging.WARNING and "setup.complete" in r.message
        for r in caplog.records
    )


def test_launch_metrics_remote_targets_hosted_url_and_key(monkeypatch):
    config.save_config(
        config.SmartMemoryConfig(
            mode="remote",
            api_url="https://hosted.example.test/custom/",
            team_id="test_launch_team",
        )
    )
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_launch_key")
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={"event_id": "test_launch_event"})

    options = install_transport(monkeypatch, receive)
    assert launch_metrics.emit("setup.complete", {"mode": "remote"}) is True
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://hosted.example.test/custom/memory/launch/event"
    assert request.headers["Authorization"] == "Bearer test_launch_key"
    assert request.headers["X-Workspace-Id"] == "test_launch_team"
    assert json.loads(request.content) == {
        "event_type": "setup.complete",
        "props": {"mode": "remote"},
    }
    assert set(request.extensions["timeout"].values()) == {2.0}
    assert options == [{"trust_env": True}]


def test_launch_metrics_local_bypasses_proxy(monkeypatch):
    requests = []
    options = install_transport(
        monkeypatch, lambda request: requests.append(request) or httpx.Response(200)
    )
    assert launch_metrics.emit("setup.complete") is True
    assert str(requests[0].url) == "http://127.0.0.1:19017/memory/launch/event"
    assert "Authorization" not in requests[0].headers
    assert options == [{"trust_env": False}]


def test_launch_metrics_remote_unreachable_warns(monkeypatch, caplog):
    config.save_config(config.SmartMemoryConfig(mode="remote"))
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_launch_key")

    def fail(request):
        raise httpx.ConnectTimeout("unreachable", request=request)

    install_transport(monkeypatch, fail)
    assert launch_metrics.emit("setup.complete") is False
    assert any(
        r.levelno == logging.WARNING and "hosted service unreachable" in r.message
        for r in caplog.records
    )


def test_launch_metrics_remote_missing_key_does_not_post(monkeypatch, caplog):
    config.save_config(config.SmartMemoryConfig(mode="remote"))
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    options = install_transport(
        monkeypatch, lambda request: pytest.fail("must not POST without a key")
    )
    assert launch_metrics.emit("setup.complete") is False
    assert options == []
    assert "no remote API key event=setup.complete" in caplog.text


def test_launch_metrics_remote_keychain_and_transport_share_wall_budget(
    monkeypatch, caplog
):
    config.save_config(config.SmartMemoryConfig(mode="remote"))
    release = threading.Event()
    threads = []
    real_thread = threading.Thread

    def thread(**kwargs):
        worker = real_thread(**kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr(launch_metrics.threading, "Thread", thread)
    monkeypatch.setattr(
        config, "get_api_key", lambda: release.wait(5) and "test_launch_key"
    )
    install_transport(monkeypatch, lambda request: httpx.Response(200))
    started = time.monotonic()
    try:
        assert launch_metrics.emit("setup.complete") is False
        assert time.monotonic() - started < 2.2
        assert "exceeded two-second budget event=setup.complete" in caplog.text
    finally:
        release.set()
        for worker in threads:
            worker.join(1)
            assert not worker.is_alive()
