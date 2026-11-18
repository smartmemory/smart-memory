"""LITE-LOOPBACK-HOST-GATE-1: the production root app refuses rebinding and cross-site requests.

Every test drives the root app from ``viewer_server._build_app()`` (never a replacement
root) and proves the mechanism: the refused request never reaches its handler or the store.
"""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from smartmemory_app import loopback_gate
from smartmemory_app.viewer_server import _build_app

PORT = 9014
BASE = f"http://127.0.0.1:{PORT}"
CROSS_SITE = {"Host": f"127.0.0.1:{PORT}", "Origin": "https://evil.example"}


class HandlerRan(Exception):
    pass


@pytest.fixture
def client():
    return TestClient(_build_app(), base_url=BASE, raise_server_exceptions=False)


@pytest.fixture
def handler_spies():
    """Stand-ins for the first globals the target handlers touch; entering them is the proof."""
    lock = MagicMock()
    lock.__enter__.side_effect = HandlerRan("rw_lock entered")
    get_mem = MagicMock(side_effect=HandlerRan("_get_mem called"))
    with (
        patch("smartmemory_app.local_api._rw_lock", lock),
        patch("smartmemory_app.local_api._get_mem", get_mem),
    ):
        yield lock, get_mem


@pytest.fixture
def store():
    mem = MagicMock()
    mem.delete.return_value = True
    with patch("smartmemory_app.local_api._get_mem", return_value=mem):
        yield mem


def _assert_refused(response, handler_spies, body):
    lock, get_mem = handler_spies
    assert response.status_code == 403
    assert response.json() == body
    assert not lock.__enter__.called
    assert not get_mem.called


def test_rebinding_host_cannot_read_the_graph(client, handler_spies):
    response = client.get(
        "/memory/graph/full", headers={"Host": "attacker.example:9014"}
    )

    _assert_refused(response, handler_spies, loopback_gate.HOST_REFUSED)


def test_rebinding_host_cannot_delete_a_memory(client, store):
    response = client.delete("/memory/item-1", headers={"Host": "attacker.example"})

    assert response.status_code == 403
    assert response.json() == loopback_gate.HOST_REFUSED
    store.delete.assert_not_called()


def test_cross_site_reindex_is_refused(client, handler_spies):
    response = client.post("/memory/reindex", headers=CROSS_SITE)

    _assert_refused(response, handler_spies, loopback_gate.ORIGIN_REFUSED)


def test_cross_site_reextract_plan_is_refused(client, handler_spies):
    with patch("smartmemory_app.work_graph.get_work_graph") as get_work_graph:
        response = client.post("/memory/reextract", headers=CROSS_SITE)

    _assert_refused(response, handler_spies, loopback_gate.ORIGIN_REFUSED)
    get_work_graph.assert_not_called()


def test_null_origin_cannot_delete(client, store):
    response = client.delete(
        "/memory/item-1", headers={"Host": f"127.0.0.1:{PORT}", "Origin": "null"}
    )

    assert response.status_code == 403
    assert response.json() == loopback_gate.ORIGIN_REFUSED
    store.delete.assert_not_called()


def test_loopback_name_on_another_port_is_refused(client, handler_spies):
    response = client.get("/memory/graph/full", headers={"Host": "localhost:8080"})

    _assert_refused(response, handler_spies, loopback_gate.HOST_REFUSED)


def test_missing_host_is_refused(handler_spies):
    sent = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/memory/graph/full",
        "raw_path": b"/memory/graph/full",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(_build_app()(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    lock, get_mem = handler_spies
    assert not lock.__enter__.called
    assert not get_mem.called


@pytest.mark.parametrize(
    "host", [f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}", "localhost"]
)
def test_loopback_hosts_reach_the_handlers(client, host):
    port_host = host if host.endswith(f":{PORT}") else f"{host}:{PORT}"
    for headers in ({"Host": host}, {"Host": host, "Origin": f"http://{port_host}"}):
        get_mem = MagicMock(side_effect=HandlerRan("_get_mem called"))
        with patch("smartmemory_app.local_api._get_mem", get_mem):
            read = client.get("/memory/graph/full", headers=headers)
        assert read.status_code == 500, headers
        get_mem.assert_called_once_with()

        mem = MagicMock()
        mem.delete.return_value = True
        with patch("smartmemory_app.local_api._get_mem", return_value=mem):
            deleted = client.delete("/memory/item-1", headers=headers)
        assert deleted.status_code == 204, headers
        mem.delete.assert_called_once_with("item-1")


def test_obsidian_origin_reaches_the_store(client, store):
    response = client.delete(
        "/memory/item-1",
        headers={"Host": f"127.0.0.1:{PORT}", "Origin": "app://obsidian.md"},
    )

    assert response.status_code == 204
    store.delete.assert_called_once_with("item-1")


def test_viewer_page_is_served_only_to_a_loopback_host(client):
    served = client.get("/", headers={"Host": f"localhost:{PORT}"})
    refused = client.get("/", headers={"Host": f"attacker.example:{PORT}"})

    assert served.status_code == 200
    assert "<html" in served.text.lower()
    assert refused.status_code == 403
    assert refused.json() == loopback_gate.HOST_REFUSED


def test_websocket_handshake_from_a_foreign_origin_is_closed(client):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/memory/ws",
            headers={"Host": f"127.0.0.1:{PORT}", "Origin": "https://evil.example"},
        ):
            pass

    assert closed.value.code == 1008


def test_gate_is_the_outermost_user_middleware():
    app = _build_app()

    assert app.user_middleware[0].cls is loopback_gate.LoopbackGate


def test_refusals_log_one_rate_limited_warning(client, caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(loopback_gate.time, "monotonic", lambda: now[0])
    hostile = "x" * 300 + ".attacker.example:9014"

    with caplog.at_level(logging.WARNING, logger=loopback_gate.__name__):
        client.get("/memory/graph/full", headers={"Host": hostile})
        client.get("/memory/graph/full", headers={"Host": "attacker.example"})
        client.get("/memory/graph/full", headers={"Host": "attacker.example"})
        now[0] += 11
        client.post("/memory/reindex", headers=CROSS_SITE)

    lines = [r.getMessage() for r in caplog.records if r.name == loopback_gate.__name__]
    assert len(lines) == 2
    assert all(
        r.levelno == logging.WARNING
        for r in caplog.records
        if r.name == loopback_gate.__name__
    )
    assert "GET" in lines[0] and "/memory/graph/full" in lines[0]
    assert repr(hostile[:100]) in lines[0]
    assert "x" * 101 not in lines[0]
    assert "POST" in lines[1] and "https://evil.example" in lines[1]
    assert "2 more refusals" in lines[1]


RECALL = "/memory/recall?include_snapshot=false&top_k=0&query=attacker-marker"
LOCAL_HOST = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def recall_spies():
    """The storage recall and the trace writer a cross-site GET /memory/recall would reach."""
    with (
        patch("smartmemory_app.storage.recall", return_value="ctx") as recall,
        patch("smartmemory_app.recall_format._trace") as trace,
    ):
        yield recall, trace


def _assert_recall_not_reached(response, recall_spies, body):
    recall, trace = recall_spies
    assert response.status_code == 403
    assert response.json() == body
    recall.assert_not_called()
    trace.assert_not_called()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_get_recall_is_refused_by_fetch_metadata(client, recall_spies, site):
    response = client.get(RECALL, headers={**LOCAL_HOST, "Sec-Fetch-Site": site})

    _assert_recall_not_reached(response, recall_spies, loopback_gate.FETCH_SITE_REFUSED)


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_foreign_origin_get_recall_is_refused(client, recall_spies, origin):
    response = client.get(RECALL, headers={**LOCAL_HOST, "Origin": origin})

    _assert_recall_not_reached(response, recall_spies, loopback_gate.ORIGIN_REFUSED)


def test_repeated_fetch_site_header_is_refused(client, recall_spies):
    response = client.get(
        RECALL,
        headers=[
            ("Host", LOCAL_HOST["Host"]),
            ("Sec-Fetch-Site", "same-origin"),
            ("Sec-Fetch-Site", "none"),
        ],
    )

    _assert_recall_not_reached(response, recall_spies, loopback_gate.FETCH_SITE_REFUSED)


@pytest.mark.parametrize(
    "extra", [{"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}]
)
def test_same_origin_user_and_non_browser_gets_reach_recall(
    client, recall_spies, extra
):
    response = client.get(RECALL, headers={**LOCAL_HOST, **extra})

    recall, _ = recall_spies
    assert response.status_code == 200
    assert response.json() == {"context": "ctx"}
    recall.assert_called_once()


def test_obsidian_origin_passes_even_with_cross_site_fetch_metadata(client, store):
    response = client.delete(
        "/memory/item-1",
        headers={
            **LOCAL_HOST,
            "Origin": "app://obsidian.md",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 204
    store.delete.assert_called_once_with("item-1")


def test_viewer_page_is_served_for_same_origin_fetch_metadata(client):
    served = client.get(
        "/", headers={"Host": f"localhost:{PORT}", "Sec-Fetch-Site": "same-origin"}
    )

    assert served.status_code == 200
    assert "<html" in served.text.lower()


@pytest.mark.parametrize("host", ["evil.example:" + "9" * 5000, "localhost:99999"])
def test_oversized_host_port_gets_the_fixed_refusal(
    client, handler_spies, caplog, host
):
    with caplog.at_level(logging.WARNING, logger=loopback_gate.__name__):
        response = client.get("/memory/graph/full", headers={"Host": host})

    _assert_refused(response, handler_spies, loopback_gate.HOST_REFUSED)
    assert [r.levelno for r in caplog.records if r.name == loopback_gate.__name__] == [
        logging.WARNING
    ]


def test_cross_site_refusal_logs_a_warning(client, recall_spies, caplog):
    with caplog.at_level(logging.WARNING, logger=loopback_gate.__name__):
        client.get(RECALL, headers={**LOCAL_HOST, "Sec-Fetch-Site": "cross-site"})

    lines = [r.getMessage() for r in caplog.records if r.name == loopback_gate.__name__]
    assert len(lines) == 1
    assert "GET" in lines[0] and "fetch-site" in lines[0] and "cross-site" in lines[0]
