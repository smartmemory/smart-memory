"""Unit tests for the lite progress drain loop (events_server.py + event_sink.py).

PLAT-PUSH-SSE-1 deleted the ws://:9015 server; the drain loop is now the
top-level task and SSE is the only transport. Tests cover:
  - get_event_sink() singleton semantics (sequential + concurrent)
  - start_background() idempotency (sequential + concurrent)
  - _serve() attaches the loop and detaches it in finally, with no port to bind
  - stop_event causes the drain loop to exit
  - Fan-out: an item reaches every registered SSE subscriber, with an id
  - Operation mapping: adds, deletes and clears each get the right kind/action
  - QueueFull logs a WARNING naming what was lost (no silent degradation)
  - seq survives a process restart (persisted hi/lo block)
  - History ring backs the since / from_seq resume modes
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_events_server_state(tmp_path):
    """Give every test a private history ring, subscriber list and seq file."""
    import smartmemory_app.events_server as _mod

    _mod._sse_history.clear()
    _mod._sse_subscribers.clear()
    _mod._reset_seq_state_for_tests()
    with patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path):
        yield
    _mod._sse_history.clear()
    _mod._sse_subscribers.clear()
    _mod._reset_seq_state_for_tests()


def _span(operation: str, **data) -> dict:
    """A raw sink item as produced by _emit_span for a graph mutation."""
    return {
        "event_type": "span",
        "component": "graph",
        "operation": operation,
        "name": f"graph.{operation}",
        "trace_id": "",
        "span_id": "abc123",
        "parent_span_id": None,
        **data,
    }


# ---------------------------------------------------------------------------
# get_event_sink() singleton
# ---------------------------------------------------------------------------


class TestGetEventSink:
    def setup_method(self):
        """Reset singleton before each test."""
        import smartmemory_app.event_sink as _mod

        _mod._sink = None

    def teardown_method(self):
        import smartmemory_app.event_sink as _mod

        _mod._sink = None

    def test_sequential_calls_return_same_instance(self):
        from smartmemory_app.event_sink import get_event_sink

        a = get_event_sink()
        b = get_event_sink()
        assert a is b

    def test_concurrent_calls_create_exactly_one_instance(self):
        """Two threads racing through get_event_sink() must create exactly one sink."""
        from smartmemory_app.event_sink import get_event_sink

        results = []
        barrier = threading.Barrier(2)

        def _worker():
            barrier.wait()
            results.append(get_event_sink())

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        # Both must be the exact same object
        assert results[0] is results[1]


# ---------------------------------------------------------------------------
# start_background() idempotency
# ---------------------------------------------------------------------------


class TestStartBackground:
    def setup_method(self):
        import smartmemory_app.events_server as _mod

        _mod._server_thread = None
        _mod._stop_event.clear()

    def teardown_method(self):
        import smartmemory_app.events_server as _mod

        _mod._stop_event.set()
        if _mod._server_thread is not None:
            _mod._server_thread.join(timeout=2)
        _mod._server_thread = None
        _mod._stop_event.clear()

    def test_sequential_idempotent(self):
        """Second call while thread is alive spawns no new thread."""
        import smartmemory_app.events_server as _mod

        async def _fake_serve():
            await asyncio.sleep(10)

        with patch("smartmemory_app.events_server._serve", side_effect=_fake_serve):
            _mod.start_background()
            first_thread = _mod._server_thread

            _mod.start_background()
            second_thread = _mod._server_thread

        assert first_thread is second_thread

    def test_concurrent_safety(self):
        """Two concurrent callers each acquiring the lock still spawn exactly one thread."""
        import smartmemory_app.events_server as _mod

        async def _fake_serve():
            await asyncio.sleep(10)

        with patch("smartmemory_app.events_server._serve", side_effect=_fake_serve):
            threads_spawned = []
            barrier = threading.Barrier(2)

            def _caller():
                barrier.wait()
                _mod.start_background()
                threads_spawned.append(_mod._server_thread)

            t1 = threading.Thread(target=_caller)
            t2 = threading.Thread(target=_caller)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        # Both callers should observe the same thread object
        assert len(threads_spawned) == 2
        assert threads_spawned[0] is threads_spawned[1]


# ---------------------------------------------------------------------------
# _serve() lifecycle
# ---------------------------------------------------------------------------


class TestServe:
    def test_attach_loop_none_called_in_finally(self):
        """attach_loop(None) is always called, whatever exit path _serve takes."""
        import smartmemory_app.events_server as _mod
        from smartmemory.observability.events import InProcessQueueSink

        mock_sink = MagicMock(spec=InProcessQueueSink)
        mock_sink._q = asyncio.Queue()
        attach_calls = []
        mock_sink.attach_loop.side_effect = lambda loop: attach_calls.append(loop)

        _mod._stop_event.set()
        try:
            with patch(
                "smartmemory_app.event_sink.get_event_sink", return_value=mock_sink
            ):
                asyncio.run(_mod._serve())
        finally:
            _mod._stop_event.clear()

        # First call: real loop. Last (finally): None.
        assert attach_calls[0] is not None
        assert attach_calls[-1] is None

    def test_stop_event_exits_drain_loop(self):
        """Setting stop_event causes _serve to exit the while loop."""
        import smartmemory_app.events_server as _mod
        from smartmemory.observability.events import InProcessQueueSink

        mock_sink = MagicMock(spec=InProcessQueueSink)
        mock_sink._q = asyncio.Queue()
        mock_sink.attach_loop = MagicMock()

        _mod._stop_event.clear()

        async def _run():
            async def _set_stop():
                await asyncio.sleep(0.05)
                _mod._stop_event.set()

            with patch(
                "smartmemory_app.event_sink.get_event_sink", return_value=mock_sink
            ):
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(_mod._serve())
                    tg.create_task(_set_stop())

        try:
            asyncio.run(asyncio.wait_for(_run(), timeout=10))
        finally:
            _mod._stop_event.clear()
        # If we get here without hanging, stop_event correctly terminated the loop.

    def test_drain_loop_needs_no_port(self):
        """Regression: the drain loop used to live inside the WS serve() block.

        A busy :9015 therefore killed SSE delivery entirely. _serve now binds
        nothing, so there is no failure mode where SSE silently delivers no
        frames because some other process holds a port.
        """
        import inspect

        import smartmemory_app.events_server as _mod

        source = inspect.getsource(_mod)
        assert "import websockets" not in source
        assert "websockets.serve" not in source
        assert "port" not in inspect.signature(_mod.start_background).parameters
        assert "port" not in inspect.signature(_mod._serve).parameters


# ---------------------------------------------------------------------------
# SSE fan-out
# ---------------------------------------------------------------------------


class TestFanout:
    def test_item_reaches_every_subscriber_with_an_id(self):
        import smartmemory_app.events_server as _mod
        from smartmemory.observability.events import InProcessQueueSink

        async def _run():
            sink = InProcessQueueSink()
            loop = asyncio.get_running_loop()
            sink.attach_loop(loop)
            q1: asyncio.Queue = asyncio.Queue()
            q2: asyncio.Queue = asyncio.Queue()
            _mod.register_sse_subscriber(loop, q1)
            _mod.register_sse_subscriber(loop, q2)

            await sink._q.put(
                _span(
                    "add_node",
                    memory_id="item-1",
                    memory_type="semantic",
                    label="hello",
                )
            )
            await _mod._drain_once(sink)
            await asyncio.sleep(0)

            for q in (q1, q2):
                event_id, frame = await asyncio.wait_for(q.get(), timeout=1)
                assert event_id == _mod.make_event_id(frame["ts"], frame["seq"])
                assert frame["kind"] == "graph.node"
                assert frame["payload"]["action"] == "add"
                assert frame["payload"]["data"]["memory_id"] == "item-1"
                # The span envelope never leaks into payload.data.
                assert "span_id" not in frame["payload"]["data"]

        asyncio.run(_run())

    def test_queue_full_logs_warning_naming_the_lost_frame(self):
        """No silent degradation: a dropped frame says what was lost."""
        import smartmemory_app.events_server as _mod

        async def _run():
            loop = asyncio.get_running_loop()
            full: asyncio.Queue = asyncio.Queue(maxsize=1)
            full.put_nowait(("already", {}))
            _mod.register_sse_subscriber(loop, full)

            with patch("smartmemory_app.events_server.log") as mock_log:
                _mod._fanout_sse(_span("add_node", memory_id="x"))
                await asyncio.sleep(0)

            mock_log.warning.assert_called_once()
            args = mock_log.warning.call_args[0]
            assert "queue full" in args[0]
            assert "graph.node" in [str(a) for a in args]

        asyncio.run(_run())

    def test_history_records_frames_with_no_subscribers(self):
        """A frame emitted before anyone connects is still resumable."""
        import smartmemory_app.events_server as _mod

        _mod._fanout_sse(_span("add_node", memory_id="early"))
        assert len(_mod._sse_history) == 1
        _event_id, frame = _mod._sse_history[0]
        assert frame["payload"]["data"]["memory_id"] == "early"


# ---------------------------------------------------------------------------
# Operation → kind/action mapping
# ---------------------------------------------------------------------------


class TestOperationMapping:
    @pytest.mark.parametrize(
        "operation,kind,action",
        [
            ("add_node", "graph.node", "add"),
            ("add_dual_node", "graph.node", "add"),
            ("delete_node", "graph.node", "delete"),
            ("remove_node", "graph.node", "delete"),
            ("add_edge", "graph.edge", "add"),
            ("delete_edge", "graph.edge", "delete"),
            ("remove_edge", "graph.edge", "delete"),
            ("clear", "graph.cleared", "clear"),
            ("clear_all", "graph.cleared", "clear"),
        ],
    )
    def test_operation_maps_to_kind_and_action(self, operation, kind, action):
        """Regression: a lite delete used to arrive with no payload.action and
        rendered as node_added in the viewer (design.md §3)."""
        import smartmemory_app.events_server as _mod

        frame = _mod._to_progress_event(_span(operation, memory_id="n1"), 7)
        assert frame["kind"] == kind
        assert frame["payload"]["action"] == action
        assert frame["seq"] == 7

    def test_non_graph_span_is_not_projected(self):
        import smartmemory_app.events_server as _mod

        assert _mod._to_progress_event(_span("classify"), 1) is None


# ---------------------------------------------------------------------------
# Persistent seq
# ---------------------------------------------------------------------------


class TestPersistentSeq:
    def test_seq_is_monotonic_within_a_process(self):
        import smartmemory_app.events_server as _mod

        assert [_mod._next_seq() for _ in range(3)] == [1, 2, 3]

    def test_seq_does_not_restart_at_zero_after_a_restart(self):
        """A restarted daemon must not reissue seq values a client already saw."""
        import smartmemory_app.events_server as _mod

        first = [_mod._next_seq() for _ in range(3)]
        # Simulate a process restart: forget in-memory state, keep the file.
        _mod._reset_seq_state_for_tests()
        after_restart = _mod._next_seq()
        assert after_restart > max(first)


# ---------------------------------------------------------------------------
# Resume modes backed by the history ring
# ---------------------------------------------------------------------------


class TestHistoryResume:
    def test_history_since_returns_only_later_frames(self):
        import smartmemory_app.events_server as _mod

        for i in range(3):
            _mod._fanout_sse(_span("add_node", memory_id=f"n{i}"))
        ids = [eid for eid, _ in _mod._sse_history]

        gap = _mod.history_since(ids[0])
        assert [eid for eid, _ in gap] == ids[1:]

    def test_history_from_seq_is_inclusive(self):
        import smartmemory_app.events_server as _mod

        for i in range(3):
            _mod._fanout_sse(_span("add_node", memory_id=f"n{i}"))
        seqs = [f["seq"] for _, f in _mod._sse_history]

        replay = _mod.history_from_seq(seqs[1])
        assert [f["seq"] for _, f in replay] == seqs[1:]

    def test_history_since_unknown_id_replays_the_window(self):
        """An id older than the retained window replays everything retained,
        matching XRANGE on a trimmed stream."""
        import smartmemory_app.events_server as _mod

        for i in range(3):
            _mod._fanout_sse(_span("add_node", memory_id=f"n{i}"))
        assert len(_mod.history_since("0-0")) == 3

    def test_history_is_bounded(self):
        import smartmemory_app.events_server as _mod

        assert _mod._sse_history.maxlen == _mod.HISTORY_MAXLEN


# ---------------------------------------------------------------------------
# Loopback worker bridge
# ---------------------------------------------------------------------------


class TestInternalWorkerEvents:
    def test_loopback_worker_event_reaches_sink_as_graph_frame(self):
        """A Tier-2 worker notification becomes the same graph frame as Tier 1."""
        import smartmemory_app.events_server as events_server
        import smartmemory_app.local_api as local_api
        from smartmemory.observability.events import InProcessQueueSink
        from starlette.requests import Request

        async def _run():
            sink = InProcessQueueSink()
            sink.attach_loop(asyncio.get_running_loop())
            request = Request({"type": "http", "client": ("127.0.0.1", 50000)})
            body = local_api.InternalEventsRequest(
                events=[
                    local_api.InternalGraphEvent(
                        operation="add_node",
                        data={
                            "memory_id": "bo",
                            "label": "Bo",
                            "memory_type": "entity",
                            "node_category": "entity",
                            "entity_type": "person",
                        },
                    ),
                    local_api.InternalGraphEvent(
                        operation="add_edge",
                        data={
                            "source_id": "zed",
                            "target_id": "bo",
                            "edge_type": "TRUSTS",
                        },
                    ),
                ]
            )
            with patch("smartmemory_app.event_sink.get_event_sink", return_value=sink):
                result = local_api.publish_internal_events(body, request)
            assert result == {"accepted": 2}
            await asyncio.sleep(0)
            node_raw = await asyncio.wait_for(sink._q.get(), timeout=1)
            edge_raw = await asyncio.wait_for(sink._q.get(), timeout=1)
            node_frame = events_server._to_progress_event(node_raw, 1)
            edge_frame = events_server._to_progress_event(edge_raw, 2)
            assert node_frame["kind"] == "graph.node"
            assert node_frame["payload"]["data"]["memory_id"] == "bo"
            assert edge_frame["kind"] == "graph.edge"
            assert edge_frame["payload"]["data"] == {
                "source_id": "zed",
                "target_id": "bo",
                "edge_type": "TRUSTS",
            }

        asyncio.run(_run())

    def test_worker_deletes_and_clears_are_representable(self):
        """PLAT-PUSH-SSE-1: the bridge used to accept adds only, so a
        worker-side delete never reached the viewer."""
        import smartmemory_app.events_server as events_server
        import smartmemory_app.local_api as local_api
        from smartmemory.observability.events import InProcessQueueSink
        from starlette.requests import Request

        async def _run():
            sink = InProcessQueueSink()
            sink.attach_loop(asyncio.get_running_loop())
            request = Request({"type": "http", "client": ("127.0.0.1", 50000)})
            body = local_api.InternalEventsRequest(
                events=[
                    local_api.InternalGraphEvent(
                        operation="delete_node", data={"memory_id": "bo"}
                    ),
                    local_api.InternalGraphEvent(
                        operation="delete_edge",
                        data={"source_id": "zed", "target_id": "bo"},
                    ),
                    local_api.InternalGraphEvent(operation="clear", data={}),
                ]
            )
            with patch("smartmemory_app.event_sink.get_event_sink", return_value=sink):
                assert local_api.publish_internal_events(body, request) == {
                    "accepted": 3
                }
            await asyncio.sleep(0)

            frames = []
            for i in range(3):
                raw = await asyncio.wait_for(sink._q.get(), timeout=1)
                frames.append(events_server._to_progress_event(raw, i + 1))

            assert [(f["kind"], f["payload"]["action"]) for f in frames] == [
                ("graph.node", "delete"),
                ("graph.edge", "delete"),
                ("graph.cleared", "clear"),
            ]

        asyncio.run(_run())

    def test_delete_node_without_an_id_is_rejected(self):
        import smartmemory_app.local_api as local_api
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({"type": "http", "client": ("127.0.0.1", 50000)})
        body = local_api.InternalEventsRequest(
            events=[local_api.InternalGraphEvent(operation="delete_node", data={})]
        )
        with pytest.raises(HTTPException, match="requires an id"):
            local_api.publish_internal_events(body, request)

    def test_non_loopback_worker_event_is_rejected(self):
        import smartmemory_app.local_api as local_api
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request({"type": "http", "client": ("203.0.113.10", 50000)})
        body = local_api.InternalEventsRequest(events=[])
        with pytest.raises(HTTPException, match="loopback"):
            local_api.publish_internal_events(body, request)


# ---------------------------------------------------------------------------
# Drain-loop resilience
# ---------------------------------------------------------------------------


class TestDrainResilience:
    def test_idle_queue_returns_on_timeout(self):
        """wait_for timeout on an empty queue returns without error."""
        import smartmemory_app.events_server as _mod
        from smartmemory.observability.events import InProcessQueueSink

        async def _run():
            sink = InProcessQueueSink()
            sink.attach_loop(asyncio.get_running_loop())
            # Empty queue — _drain_once uses wait_for(timeout=1.0). Wrap the call
            # so the test completes in ≤2s regardless of the internal timeout.
            await asyncio.wait_for(_mod._drain_once(sink), timeout=2.0)

        asyncio.run(_run())

    def test_closed_subscriber_loop_does_not_raise(self):
        import smartmemory_app.events_server as _mod

        dead_loop = MagicMock()
        dead_loop.call_soon_threadsafe.side_effect = RuntimeError("loop is closed")
        _mod.register_sse_subscriber(dead_loop, asyncio.Queue())

        # Must not raise — unregister on disconnect handles the cleanup.
        _mod._fanout_sse(_span("add_node", memory_id="x"))
