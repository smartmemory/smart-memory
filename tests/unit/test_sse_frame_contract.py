"""PLAT-PUSH-SSE-1: one frame contract, asserted against both SSE routes.

`progress-event-contract.json` v1.5.0 (PLAT-PROGRESS-1) defines a single wire
shape for `GET /memory/progress/stream`. Two servers implement it: the hosted
service on :9001 and the lite daemon on :9014. Before this feature the lite
route diverged — no `id:` line, no `payload.action`, resume modes silently
ignored — so the shared graph viewer rendered a lite delete as an add.

This module holds the contract as a fixture and one validator, then runs the
validator against both arms:

  arm A (lite)   — live frames from the real `local_api.progress_stream`
                   route body, driven directly (TestClient buffers a
                   never-completing StreamingResponse and cannot read SSE).
  arm B (hosted) — a golden frame recorded verbatim from the hosted route's
                   `_make_sse_frame` / `_decode_stream_entry`
                   (smart-memory-service/memory_service/api/routes/progress.py).
                   The wrapper cannot import the service, so the hosted arm is
                   a recorded sample, not a live call. Its own suite covers the
                   live path; what is proven here is that one validator accepts
                   both, i.e. the shapes have not diverged.
"""

import json
import re

import pytest

# ---------------------------------------------------------------------------
# The contract fixture — progress-event-contract.json v1.5.0
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "1.5.0"

# ProgressEvent.required
REQUIRED_FIELDS = ("run_id", "scope", "seq", "ts", "kind", "status", "payload")
OPTIONAL_FIELDS = ("stage",)

# ProgressEvent.properties.status.enum
STATUS_ENUM = frozenset({"started", "progress", "ok", "warn", "error"})

# ProgressEvent.properties.scope.pattern
SCOPE_PATTERN = re.compile(r"^workspace:[a-zA-Z0-9_-]+$")

# SSEEndpoint.frame — "id: <stream-id>\ndata: { ProgressEvent as JSON }\n\n"
FRAME_PATTERN = re.compile(r"^id: (?P<id>[^\n]+)\ndata: (?P<data>.+)$", re.DOTALL)

# SSEEndpoint.query_params.since — "<ms>-<seq>"
EVENT_ID_PATTERN = re.compile(r"^\d+-\d+$")

# ---------------------------------------------------------------------------
# arm B: golden frame recorded from the hosted route
# ---------------------------------------------------------------------------

HOSTED_GOLDEN_FRAME = (
    "id: 1757001234567-0\n"
    "data: "
    + json.dumps(
        {
            "run_id": "6f1c9a2e4b7d4c8f9a0b1c2d3e4f5061",
            "scope": "workspace:ws_abc123",
            "seq": 4,
            "ts": 1757001234.567,
            "kind": "graph.node",
            "status": "ok",
            "payload": {
                "action": "add",
                "data": {"memory_id": "item-1", "label": "hello"},
                "workspace_id": "ws_abc123",
                "original_ts": 1757001234.567,
            },
        }
    )
    + "\n\n"
)


# ---------------------------------------------------------------------------
# The one validator
# ---------------------------------------------------------------------------


def assert_valid_sse_frame(raw: str) -> dict:
    """Assert one SSE frame satisfies SSEEndpoint.frame + ProgressEvent.

    Returns the decoded ProgressEvent so callers can make route-specific
    assertions on top of the shared ones.
    """
    assert raw.endswith("\n\n"), "frame must terminate with a blank line"
    match = FRAME_PATTERN.match(raw[:-2])
    assert match, f"frame must be 'id: <id>\\ndata: <json>\\n\\n', got: {raw!r}"

    event_id = match.group("id")
    assert EVENT_ID_PATTERN.match(event_id), (
        f"SSE id must be the <ms>-<seq> stream-ID shape so a client can pass it "
        f"back as `since` / Last-Event-ID, got {event_id!r}"
    )

    event = json.loads(match.group("data"))
    for field in REQUIRED_FIELDS:
        assert field in event, f"ProgressEvent is missing required field {field!r}"
    assert set(event) <= set(REQUIRED_FIELDS + OPTIONAL_FIELDS), (
        f"ProgressEvent carries fields outside the contract: "
        f"{sorted(set(event) - set(REQUIRED_FIELDS + OPTIONAL_FIELDS))}"
    )

    assert isinstance(event["run_id"], str) and event["run_id"]
    assert SCOPE_PATTERN.match(event["scope"]), f"bad scope {event['scope']!r}"
    assert isinstance(event["seq"], int) and event["seq"] >= 0
    assert isinstance(event["ts"], (int, float))
    assert isinstance(event["kind"], str) and "." in event["kind"]
    assert event["status"] in STATUS_ENUM
    assert isinstance(event["payload"], dict)

    # PayloadConventions.original_ts — required on kinds that drive animated
    # replay, and it must equal the top-level ts at first emission.
    if event["kind"] in ("graph.node", "graph.edge", "pipeline.stage"):
        assert "original_ts" in event["payload"], (
            f"{event['kind']} must carry payload.original_ts or the viewer's "
            f"replay dumps every event instantly"
        )
        assert event["payload"]["original_ts"] == event["ts"]

    return event


# ---------------------------------------------------------------------------
# arm A: the lite route
#
# The route is driven directly rather than through TestClient: TestClient
# buffers a StreamingResponse whose generator never completes, so an SSE
# route cannot be read through it. Driving the coroutine exercises the real
# route body, including its query validation.
# ---------------------------------------------------------------------------


class _StubRequest:
    """Minimal Request stand-in for the SSE route (headers + connection state)."""

    def __init__(self, headers: dict | None = None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def _collect(request, limit: int, **query) -> list[str]:
    """Run the lite route and collect `limit` non-comment frames."""
    import smartmemory_app.local_api as local_api

    params = {"run_id": None, "from_seq": None, "since": None}
    params.update(query)
    response = await local_api.progress_stream(request, **params)

    frames: list[str] = []
    async for raw in response.body_iterator:
        if raw.startswith(":"):
            continue  # comment: ": connected" / ": keepalive"
        frames.append(raw)
        if len(frames) >= limit:
            request.disconnected = True
            break
    await response.body_iterator.aclose()
    return frames


def _run(coro):
    import asyncio

    return asyncio.run(asyncio.wait_for(coro, timeout=10))


@pytest.fixture()
def lite_state(tmp_path, monkeypatch):
    import smartmemory_app.events_server as events_server

    monkeypatch.setattr(
        "smartmemory_app.storage._resolve_data_dir", lambda *a, **k: tmp_path
    )
    events_server._sse_history.clear()
    events_server._sse_subscribers.clear()
    events_server._reset_seq_state_for_tests()
    yield events_server
    events_server._sse_history.clear()
    events_server._sse_subscribers.clear()
    events_server._reset_seq_state_for_tests()


def _emit(operation: str, **data) -> None:
    import smartmemory_app.events_server as events_server

    events_server._fanout_sse(
        {
            "event_type": "span",
            "component": "graph",
            "operation": operation,
            "name": f"graph.{operation}",
            "trace_id": "",
            "span_id": "s1",
            "parent_span_id": None,
            **data,
        }
    )


class TestLiteRouteFrames:
    def test_replayed_frame_satisfies_the_contract(self, lite_state):
        """The lite route's frames pass the same validator as the hosted one."""
        _emit("add_node", memory_id="item-1", label="hello")
        frames = _run(_collect(_StubRequest(), 1, since="0-0"))

        assert len(frames) == 1
        event = assert_valid_sse_frame(frames[0])
        assert event["kind"] == "graph.node"
        assert event["payload"]["action"] == "add"
        assert event["payload"]["data"]["memory_id"] == "item-1"

    def test_delete_carries_a_delete_action(self, lite_state):
        """The divergence this feature closed: a lite delete used to arrive
        with no action at all and rendered as an add."""
        _emit("delete_node", memory_id="item-1")
        frames = _run(_collect(_StubRequest(), 1, since="0-0"))

        assert assert_valid_sse_frame(frames[0])["payload"]["action"] == "delete"

    def test_resume_after_reconnect_delivers_the_gap(self, lite_state):
        """Regression for the reconnect gap: events emitted while the client
        was disconnected must arrive on the reconnect, not be skipped."""
        _emit("add_node", memory_id="before")
        first = _run(_collect(_StubRequest(), 1, since="0-0"))
        last_event_id = FRAME_PATTERN.match(first[0][:-2]).group("id")

        # Client is now disconnected. Three events happen in the gap.
        for i in range(3):
            _emit("add_node", memory_id=f"gap-{i}")

        gap = _run(_collect(_StubRequest({"Last-Event-ID": last_event_id}), 3))
        ids = [assert_valid_sse_frame(f)["payload"]["data"]["memory_id"] for f in gap]
        assert ids == ["gap-0", "gap-1", "gap-2"]

    def test_live_frame_reaches_a_connected_client(self, lite_state):
        """Live mode: a frame emitted after subscribe is delivered."""
        import asyncio

        async def _run_live():
            import smartmemory_app.local_api as local_api

            request = _StubRequest()
            response = await local_api.progress_stream(
                request, run_id=None, from_seq=None, since=None
            )
            it = response.body_iterator
            assert await it.__anext__() == ": connected\n\n"
            _emit("add_edge", source_id="a", target_id="b", edge_type="RELATES_TO")
            await asyncio.sleep(0)
            raw = await asyncio.wait_for(it.__anext__(), timeout=5)
            request.disconnected = True
            await it.aclose()
            return raw

        event = assert_valid_sse_frame(_run(_run_live()))
        assert event["kind"] == "graph.edge"
        assert event["payload"]["action"] == "add"

    def test_run_replay_mode_is_honoured(self, lite_state):
        _emit("add_node", memory_id="n0")
        _emit("add_node", memory_id="n1")
        seq_of_second = lite_state._sse_history[1][1]["seq"]

        frames = _run(
            _collect(
                _StubRequest(),
                1,
                run_id=lite_state.LITE_RUN_ID,
                from_seq=seq_of_second,
            )
        )
        assert assert_valid_sse_frame(frames[0])["seq"] == seq_of_second

    @pytest.mark.parametrize(
        "query,status",
        [
            ({"from_seq": 0}, 400),  # from_seq without run_id
            ({"run_id": "lite", "from_seq": 0, "since": "1-1"}, 400),  # exclusive
            ({"run_id": "some-other-run"}, 404),  # unknown run
        ],
    )
    def test_unsupported_query_modes_are_refused_not_ignored(
        self, lite_state, query, status
    ):
        """No silent degradation: the lite route used to accept and ignore
        every query mode, so a resume request looked like it worked."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            _run(_collect(_StubRequest(), 1, **query))
        assert excinfo.value.status_code == status


# ---------------------------------------------------------------------------
# arm B: the hosted route
# ---------------------------------------------------------------------------


class TestHostedRouteFrame:
    def test_golden_hosted_frame_satisfies_the_same_contract(self):
        event = assert_valid_sse_frame(HOSTED_GOLDEN_FRAME)
        assert event["kind"] == "graph.node"
        assert event["scope"].startswith("workspace:")


class TestBothArmsAgree:
    def test_lite_and_hosted_frames_have_the_same_field_set(self, lite_state):
        """The point of the feature: one consumer, no per-backend branch."""
        _emit("add_node", memory_id="item-1", label="hello")
        lite_frames = _run(_collect(_StubRequest(), 1, since="0-0"))

        lite = assert_valid_sse_frame(lite_frames[0])
        hosted = assert_valid_sse_frame(HOSTED_GOLDEN_FRAME)

        assert set(lite) == set(hosted)
        assert lite["kind"] == hosted["kind"]
        assert lite["status"] == hosted["status"]
        # payload is kind-specific, but the conventions both consumers read
        # must be present on both.
        for field in ("action", "data", "original_ts"):
            assert field in lite["payload"]
            assert field in hosted["payload"]
