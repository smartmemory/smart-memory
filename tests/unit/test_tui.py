"""DIST-LITE-10: pilot tests for `sm explore` against a fake daemon.

No network and no real daemon: every route is served by an httpx.MockTransport
handler over the in-memory amulet graph below (the same shape the live :9014
daemon returns — one memory item wired to entity nodes by CONTAINS_ENTITY, the
semantic edges hanging off the entities).
"""

import json

import httpx
import pytest
from click.testing import CliRunner
from textual.widgets import Input

from smartmemory_app.cli import cli
from smartmemory_app.tui.app import ExploreApp
from smartmemory_app.tui.client import ExploreClient, ExploreUnavailable
from smartmemory_app.tui.panes import FocusPane, LivePane, RelationsPane, ResultsPane

MEMORY_ID = "mem-amulet"
ZED = "ent-zed"
YARA = "ent-yara"

NODES = {
    MEMORY_ID: {
        "item_id": MEMORY_ID,
        "content": "Zed trusts Yara and thinks Xavier is a liar.",
        "memory_type": "episodic",
        "node_category": "memory",
    },
    ZED: {
        "item_id": ZED,
        "name": "Zed",
        "content": "Zed",
        "entity_type": "person",
        "memory_type": "entity",
        "node_category": "entity",
    },
    YARA: {
        "item_id": YARA,
        "name": "Yara",
        "content": "Yara",
        "entity_type": "person",
        "memory_type": "entity",
        "node_category": "entity",
    },
}

EDGES = [
    {"source_id": MEMORY_ID, "target_id": ZED, "edge_type": "CONTAINS_ENTITY"},
    {"source_id": MEMORY_ID, "target_id": YARA, "edge_type": "CONTAINS_ENTITY"},
    {"source_id": ZED, "target_id": MEMORY_ID, "edge_type": "MENTIONED_IN"},
    {"source_id": YARA, "target_id": MEMORY_ID, "edge_type": "MENTIONED_IN"},
    {"source_id": ZED, "target_id": YARA, "edge_type": "trusts"},
    {"source_id": YARA, "target_id": ZED, "edge_type": "distrusts"},
]

# ── the ask response, per DIST-LITE-9 ask-contract.json ─────────────────────
ASK_RESPONSE = {
    "answer": "Zed believes Yara did not steal the amulet.",
    "reasoning": "Zed trusts Yara and treats Xavier as unreliable.",
    "evidence": [{"item_id": MEMORY_ID, "content": NODES[MEMORY_ID]["content"]}],
    "relations": [
        {
            "source": "Zed",
            "type": "trusts",
            "target": "Yara",
            "source_id": ZED,
            "target_id": YARA,
        }
    ],
}

SSE_BODY = (
    ": connected\n\n"
    "id: 1757001234567-0\n"
    "data: "
    + json.dumps(
        {
            "run_id": "lite",
            "scope": "workspace:lite",
            "seq": 1,
            "ts": 1757001234.5,
            "kind": "graph.node",
            "status": "ok",
            "payload": {
                "action": "add",
                "data": {"memory_id": MEMORY_ID, "label": "Zed"},
            },
        }
    )
    + "\n\n"
    "id: 1757001234567-1\n"
    "data: "
    + json.dumps(
        {
            "run_id": "lite",
            "scope": "workspace:lite",
            "seq": 2,
            "ts": 1757001235.5,
            "kind": "graph.edge",
            "status": "ok",
            "payload": {
                "action": "add",
                "data": {"source_id": "other", "target_id": "else"},
            },
        }
    )
    + "\n\n"
)


def _neighbors(item_id):
    neighbors, seen = [], set()
    for edge in EDGES:
        if edge["source_id"] == item_id:
            other, direction = edge["target_id"], "outgoing"
        elif edge["target_id"] == item_id:
            other, direction = edge["source_id"], "incoming"
        else:
            continue
        key = (other, edge["edge_type"], direction)
        if key in seen:
            continue
        seen.add(key)
        neighbors.append(
            {"item_id": other, "link_type": edge["edge_type"], "direction": direction}
        )
    edges = [e for e in EDGES if item_id in (e["source_id"], e["target_id"])]
    return {"neighbors": neighbors, "edges": edges}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"status": "ok", "mode": "lite"})
    if path == "/memory/search":
        return httpx.Response(200, json={"items": [NODES[MEMORY_ID]]})
    if path == "/memory/ask":
        return httpx.Response(200, json=ASK_RESPONSE)
    if path == "/memory/progress/stream":
        return httpx.Response(
            200, text=SSE_BODY, headers={"content-type": "text/event-stream"}
        )
    if path.endswith("/neighbors"):
        return httpx.Response(200, json=_neighbors(path.split("/")[2]))
    if path.startswith("/memory/"):
        node = NODES.get(path.split("/")[2])
        if node is None:
            return httpx.Response(404, json={"detail": "Memory not found"})
        return httpx.Response(200, json=node)
    return httpx.Response(404, json={"detail": f"no fake route for {path}"})


@pytest.fixture
def client():
    fake = ExploreClient("http://fake", transport=httpx.MockTransport(_handler))
    yield fake
    fake.close()


async def _until(pilot, predicate, limit=200):
    """Pump the event loop until predicate holds — worker IO is off the UI thread."""
    for _ in range(limit):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("condition never became true")


# ── client-level ───────────────────────────────────────────────────────────


def test_focus_filters_infra_edges_and_walks_the_entity_hop(client):
    view = client.focus(MEMORY_ID)
    types = {row.type for row in view.rows}
    assert types == {"trusts", "distrusts"}, "structural edges must not be relations"
    assert not types & {"CONTAINS_ENTITY", "MENTIONED_IN"}
    assert [t for t, _ in view.grouped()] == ["trusts", "distrusts"]
    trusts = [r for r in view.rows if r.type == "trusts"][0]
    assert (trusts.source, trusts.target) == ("Zed", "Yara")
    assert trusts.focus_id == YARA


def test_health_refuses_when_daemon_is_unreachable():
    def dead(request):
        raise httpx.ConnectError("connection refused")

    with ExploreClient(
        "http://fake", transport=httpx.MockTransport(dead)
    ) as dead_client:
        with pytest.raises(ExploreUnavailable):
            dead_client.health()


def test_ask_response_matches_the_dist_lite_9_contract(client):
    """Contract fixture: ask-contract.json response.required + relations.required."""
    response = client.ask("does Zed believe it")
    assert set(response) >= {"answer", "reasoning", "evidence", "relations"}
    assert isinstance(response["answer"], str) and response["answer"]
    assert isinstance(response["reasoning"], str)  # may be empty, never null
    for item in response["evidence"]:
        assert set(item) >= {"item_id", "content"}
    for relation in response["relations"]:
        assert set(relation) >= {"source", "type", "target", "source_id", "target_id"}


def test_ask_rejects_an_invalid_response_shape():
    def bad(request):
        return httpx.Response(200, json={"evidence": []})

    with ExploreClient("http://fake", transport=httpx.MockTransport(bad)) as bad_client:
        with pytest.raises(ExploreUnavailable):
            bad_client.ask("anything")


# ── pilot ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pilot_walk_and_back(client):
    app = ExploreApp(client, MEMORY_ID, live=False)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: app.focus_view is not None)
        assert app.focus_view.item_id == MEMORY_ID
        relations = app.query_one(RelationsPane)
        assert len(relations.children) > 2  # a type header plus its rows

        # The cursor lands on the first walkable row, so Enter walks immediately.
        await pilot.press("enter")
        await _until(pilot, lambda: app.focus_view.item_id == YARA)
        assert app.trail[-1] == "Yara"

        await pilot.press("backspace")
        await _until(pilot, lambda: app.focus_view.item_id == MEMORY_ID)
        assert len(app.trail) == 1


@pytest.mark.asyncio
async def test_pilot_search_then_focus_the_hit(client):
    app = ExploreApp(client, live=False)
    async with app.run_test() as pilot:
        await pilot.press("/")
        await pilot.pause()
        assert app._input_mode == "search"
        assert app.query_one(Input).value == "", (
            "`/` opens the box, it is not typed into it"
        )
        await pilot.press(*"amulet")
        await pilot.press("enter")
        results = app.query_one(ResultsPane)
        await _until(pilot, lambda: len(results.children) >= 2)

        await pilot.press("down")
        await pilot.press("enter")
        await _until(pilot, lambda: app.focus_view is not None)
        assert app.focus_view.item_id == MEMORY_ID


@pytest.mark.asyncio
async def test_pilot_ask_evidence_row_refocuses(client):
    app = ExploreApp(client, live=False)
    async with app.run_test() as pilot:
        await pilot.press("?")
        await pilot.pause()
        assert app._input_mode == "ask"
        assert app.query_one(Input).value == ""
        await pilot.press(*"who")
        await pilot.press("enter")
        results = app.query_one(ResultsPane)
        await _until(pilot, lambda: len(results.children) >= 5)
        rendered = " ".join(child.text for child in results.children)
        assert "Zed believes" in rendered
        assert "trusts" in rendered

        evidence_index = [
            i
            for i, child in enumerate(results.children)
            if getattr(child, "focus_id", None) == MEMORY_ID
        ][0]
        results.index = evidence_index
        await pilot.press("enter")
        await _until(pilot, lambda: app.focus_view is not None)
        assert app.focus_view.item_id == MEMORY_ID


@pytest.mark.asyncio
async def test_pilot_live_pane_tails_frames_and_flashes_the_focused_item(client):
    app = ExploreApp(client, MEMORY_ID, live=True)
    seen = []
    original = app.on_frame

    def record(frame):
        seen.append(frame)
        original(frame)

    app.on_frame = record
    async with app.run_test() as pilot:
        await _until(pilot, lambda: len(seen) == 2)
        assert [f["kind"] for f in seen] == ["graph.node", "graph.edge"]
        assert app.query_one(LivePane).lines, "one line per frame reaches the live pane"

        # A frame naming the focused item flashes the focus pane; an unrelated
        # frame does not.
        await _until(pilot, lambda: app.focus_view is not None)
        focus_pane = app.query_one(FocusPane)
        focus_pane.remove_class("flash")
        app.on_frame(seen[1])
        assert not focus_pane.has_class("flash")
        app.on_frame(seen[0])
        assert focus_pane.has_class("flash")


# ── CLI refusal ────────────────────────────────────────────────────────────


def test_explore_refuses_clearly_when_the_daemon_is_down(monkeypatch):
    import smartmemory_app.tui.client as client_module

    class Dead(client_module.ExploreClient):
        def health(self):
            raise client_module.ExploreUnavailable("connection refused")

    monkeypatch.setattr(client_module, "ExploreClient", Dead)
    result = CliRunner().invoke(cli, ["explore"])
    assert result.exit_code != 0
    assert "daemon is not running" in result.output
