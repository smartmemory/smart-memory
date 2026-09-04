"""PLAT-PUSH-SSE-1: Lite progress-event drain loop (SSE only).

Runs as a daemon thread inside the daemon process. Drains the
InProcessQueueSink (fed by pipeline emit_event() calls) and fans the items
out, reshaped into ProgressEvent contract frames, to every registered SSE
subscriber queue.

History: this module used to run a ``ws://:9015`` WebSocket server and the
drain loop lived *inside* that server's ``serve()`` block — so if the port
was busy, SSE delivered nothing at all. PLAT-PUSH-SSE-1 deleted the
WebSocket (no browser client had read it since ``useGraphStream`` migrated
to SSE) and made the drain loop the top-level task.

Correctness constraints (all present):
- _server_thread_lock guards start_background() check+spawn as an atomic unit
- loop captured inside _serve() (after asyncio.run() starts it)
- sink.attach_loop(loop) called immediately after capture
- sink.attach_loop(None) called in _serve() finally regardless of exit path
- asyncio.wait_for(sink._q.get(), timeout=1.0) allows stop_event check on idle queue
- Thread is daemon=True — exits automatically with the process
- A subscriber whose queue is full is logged at WARNING, never silently dropped
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

# Span-envelope fields that belong at the message top level.
# Everything else in a queue item becomes the nested ``data`` payload
# that the graph viewer reads from ``payload.data``.
_SPAN_STRUCTURAL_FIELDS = frozenset(
    {
        "event_type",
        "component",
        "operation",
        "name",
        "trace_id",
        "span_id",
        "parent_span_id",
        "duration_ms",
        "error",
        "status",
    }
)

# Sink operation -> (ProgressEvent kind, payload.action).
#
# Every graph mutation the lite pipeline can emit is mapped here. Before
# PLAT-PUSH-SSE-1 only the three "add"/clear operations were projected and
# no frame carried ``payload.action``, so classifyProgressEvent() defaulted
# a lite delete to ``node_added`` (design.md §3).
_OPERATION_MAP: dict[str, tuple[str, str]] = {
    "add_node": ("graph.node", "add"),
    "add_dual_node": ("graph.node", "add"),
    "update_node": ("graph.node", "update"),
    "delete_node": ("graph.node", "delete"),
    "remove_node": ("graph.node", "delete"),
    "add_edge": ("graph.edge", "add"),
    "delete_edge": ("graph.edge", "delete"),
    "remove_edge": ("graph.edge", "delete"),
    "clear": ("graph.cleared", "clear"),
    "clear_all": ("graph.cleared", "clear"),
}


def _to_event_data(item: dict) -> dict:
    """Strip the span envelope, leaving the node/edge-specific payload data."""
    return {k: v for k, v in item.items() if k not in _SPAN_STRUCTURAL_FIELDS}


_server_thread: threading.Thread | None = None
_server_thread_lock = threading.Lock()
_stop_event = threading.Event()

# ---------------------------------------------------------------------------
# SSE fan-out.
#
# Subscribers register an asyncio.Queue + the loop it lives on (uvicorn's
# loop, a different thread/loop from this drain loop). Cross-loop delivery
# uses loop.call_soon_threadsafe — the same bridge pattern as
# InProcessQueueSink.emit.
#
# A bounded history ring gives the lite route the hosted route's resume
# modes (``since`` / ``run_id``+``from_seq``) without a Redis stream.
# ---------------------------------------------------------------------------
_sse_lock = threading.Lock()
_sse_subscribers: list = []  # list of (loop, asyncio.Queue)

# Bounded replay window. The hosted stream is MAXLEN ~10k; the lite daemon is
# a single-user process, so 1000 frames is a generous reconnect gap.
HISTORY_MAXLEN = 1000
_sse_history: deque = deque(maxlen=HISTORY_MAXLEN)  # (event_id, frame) pairs

# The lite stream's run_id. Fixed: the daemon is one long-lived "run".
LITE_RUN_ID = "lite"
LITE_SCOPE = "workspace:local"

# ---------------------------------------------------------------------------
# Monotonic seq that survives a daemon restart.
#
# ``seq`` is the client's ordering + dedupe key. A process counter reset to 0
# on every restart makes a reconnecting client discard every new frame as
# already-seen. Persisted with a hi/lo block allocator: reserve a block on
# load, write only the block ceiling, so a restart resumes above the highest
# seq ever issued without a disk write per event.
# ---------------------------------------------------------------------------
_SEQ_BLOCK = 1000
_SEQ_FILENAME = "sse_seq.json"
_seq_lock = threading.Lock()
_sse_seq: int = 0
_seq_ceiling: int = 0
_seq_loaded: bool = False


def _seq_state_path() -> Path:
    from smartmemory_app.storage import _resolve_data_dir

    return _resolve_data_dir() / _SEQ_FILENAME


def _write_seq_ceiling(ceiling: int) -> None:
    """Persist the reserved seq ceiling. Best effort — never breaks streaming."""
    try:
        path = _seq_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"next_seq": ceiling}))
    except OSError as exc:
        log.warning(
            "events-server: could not persist SSE seq ceiling to %s (%s). "
            "seq will restart from 0 after a daemon restart and reconnecting "
            "clients may discard fresh frames as already-seen.",
            _SEQ_FILENAME,
            exc,
        )


def _next_seq() -> int:
    """Return the next monotonic seq, reserving a new block when exhausted."""
    global _sse_seq, _seq_ceiling, _seq_loaded
    with _seq_lock:
        if not _seq_loaded:
            _seq_loaded = True
            path = _seq_state_path()
            if not path.exists():
                # First start on this data dir — nothing to resume from, and
                # no client can hold a higher seq. Not a degradation.
                _sse_seq = 0
            else:
                try:
                    _sse_seq = int(json.loads(path.read_text()).get("next_seq", 0))
                except (OSError, ValueError, TypeError) as exc:
                    log.warning(
                        "events-server: persisted SSE seq at %s is unreadable (%s); "
                        "restarting seq at 0. A client reconnecting across this "
                        "restart may discard fresh frames as already-seen.",
                        path,
                        exc,
                    )
                    _sse_seq = 0
            _seq_ceiling = _sse_seq + _SEQ_BLOCK
            _write_seq_ceiling(_seq_ceiling)
        _sse_seq += 1
        if _sse_seq >= _seq_ceiling:
            _seq_ceiling = _sse_seq + _SEQ_BLOCK
            _write_seq_ceiling(_seq_ceiling)
        return _sse_seq


def _reset_seq_state_for_tests() -> None:
    """Test helper: forget the loaded seq block so the next call re-reads disk."""
    global _sse_seq, _seq_ceiling, _seq_loaded
    with _seq_lock:
        _sse_seq = 0
        _seq_ceiling = 0
        _seq_loaded = False


def register_sse_subscriber(loop, queue) -> tuple:
    """Register an SSE subscriber queue. Returns an opaque entry for unregister."""
    entry = (loop, queue)
    with _sse_lock:
        _sse_subscribers.append(entry)
    return entry


def unregister_sse_subscriber(entry) -> None:
    """Remove a previously registered SSE subscriber. Idempotent."""
    with _sse_lock:
        try:
            _sse_subscribers.remove(entry)
        except ValueError:
            pass


def make_event_id(ts: float, seq: int) -> str:
    """Build the SSE ``id:`` value: ``<ms>-<seq>``, the hosted stream-ID shape."""
    return f"{int(ts * 1000)}-{seq}"


def history_since(event_id: str) -> list:
    """Return (event_id, frame) pairs strictly after ``event_id``.

    Ordering is by the (ms, seq) tuple the id encodes — the same total order
    the hosted Redis stream IDs give. An unknown/older id replays the whole
    retained window, matching XRANGE semantics on a trimmed stream.
    """
    key = _parse_event_id(event_id)
    if key is None:
        return []
    with _sse_lock:
        entries = list(_sse_history)
    return [(eid, f) for eid, f in entries if (_parse_event_id(eid) or (0, 0)) > key]


def history_from_seq(from_seq: int) -> list:
    """Return (event_id, frame) pairs with ``seq >= from_seq``."""
    with _sse_lock:
        entries = list(_sse_history)
    return [(eid, f) for eid, f in entries if f.get("seq", 0) >= from_seq]


def _parse_event_id(event_id: str) -> tuple[int, int] | None:
    try:
        ms, _, seq = str(event_id).partition("-")
        return (int(ms), int(seq))
    except (ValueError, AttributeError):
        return None


def _to_progress_event(item: dict, seq: int) -> dict | None:
    """Reshape a raw sink item into a ProgressEvent contract frame.

    Contract: progress-event-contract.json v1.5.0. Returns None for span
    events that are not graph mutations (the lite stream stays lean; the
    viewer paints from graph.node / graph.edge / graph.cleared only).
    """
    data = _to_event_data(item)
    mapped = _OPERATION_MAP.get(item.get("operation") or "")
    if mapped is None:
        return None
    kind, action = mapped
    now = time.time()
    return {
        "run_id": LITE_RUN_ID,
        "scope": LITE_SCOPE,
        "seq": seq,
        "ts": now,
        "kind": kind,
        "status": "ok",
        # payload.action is what classifyProgressEvent() reads to tell an add
        # from a delete. original_ts drives the viewer's replay pacing.
        "payload": {"action": action, "data": data, "original_ts": now},
    }


def _fanout_sse(item: dict) -> None:
    """Push a reshaped progress frame to history and every SSE subscriber.

    Called from the single drain loop. The frame is appended to the replay
    ring even with no subscribers, so a client that connects (or reconnects)
    a moment later can still resume the gap. Cross-loop hand-off via
    call_soon_threadsafe.
    """
    frame = _to_progress_event(item, _next_seq())
    if frame is None:
        return
    event_id = make_event_id(frame["ts"], frame["seq"])

    with _sse_lock:
        _sse_history.append((event_id, frame))
        subs = list(_sse_subscribers)

    for sub_loop, q in subs:

        def _push(q=q, frame=frame, event_id=event_id) -> None:
            try:
                q.put_nowait((event_id, frame))
            except asyncio.QueueFull:
                # No silent degradation: say what was lost and how the client
                # recovers (it can resume from its Last-Event-ID).
                log.warning(
                    "events-server: SSE subscriber queue full — dropped frame "
                    "id=%s kind=%s seq=%s. The consumer is slower than the "
                    "event rate; it can recover the gap by reconnecting with "
                    "Last-Event-ID.",
                    event_id,
                    frame.get("kind"),
                    frame.get("seq"),
                )

        try:
            sub_loop.call_soon_threadsafe(_push)
        except RuntimeError:
            # Subscriber loop closed mid-flight — unregister handles cleanup.
            pass


async def _drain_once(sink) -> None:
    """Move one queued sink event to the SSE fan-out.

    Uses asyncio.wait_for to bound the get() wait so the caller can check
    stop_event without blocking forever on an idle queue.
    """
    try:
        item = await asyncio.wait_for(sink._q.get(), timeout=1.0)
    except asyncio.TimeoutError:
        return
    _fanout_sse(item)


async def _serve() -> None:
    """Run the sink drain loop until stop_background() is signalled."""
    from smartmemory_app.event_sink import get_event_sink

    sink = get_event_sink()
    loop = asyncio.get_running_loop()
    sink.attach_loop(loop)

    try:
        log.info("events-server: draining sink to SSE subscribers")
        while not _stop_event.is_set():
            await _drain_once(sink)
    finally:
        sink.attach_loop(None)
        log.info("events-server: stopped")


def start_background() -> None:
    """Start the drain loop as a background daemon thread. Idempotent.

    The _server_thread_lock makes the is_alive() check + Thread() spawn atomic —
    two concurrent callers cannot both pass the check and spawn two threads.
    """
    global _server_thread

    with _server_thread_lock:
        if _server_thread is not None and _server_thread.is_alive():
            return

        _stop_event.clear()

        def _run() -> None:
            asyncio.run(_serve())

        _server_thread = threading.Thread(
            target=_run, daemon=True, name="smartmemory-events-server"
        )
        _server_thread.start()
        log.info("events-server: background thread started")


def stop_background() -> None:
    """Signal the background drain loop to stop. Used for clean shutdown in tests."""
    _stop_event.set()
