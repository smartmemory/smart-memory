"""DIST-LITE-4/5 + DIST-DAEMON-1: Memory API — local SQLite or remote hosted API.

Mounted at /memory by viewer_server.py — routes here are relative to that mount.
GET  /graph/full         → /memory/graph/full
POST /graph/edges        → /memory/graph/edges
GET  /list               → /memory/list
POST /ingest             → /memory/ingest           (DAEMON-1: full pipeline ingest)
POST /search             → /memory/search           (DAEMON-1: semantic search)
GET  /recall             → /memory/recall           (DAEMON-1: session context)
POST /clear              → /memory/clear
POST /reindex            → /memory/reindex          (re-embed all with current model)
GET  /{id}/neighbors     → /memory/{id}/neighbors  (BEFORE /{id} — declaration order matters; DIST-OBSIDIAN-LITE-1: each neighbor carries direction)
GET  /{id}               → /memory/{id}
PATCH /{id}              → /memory/{id}            (DIST-OBSIDIAN-LITE-1: CORE-CRUD-UPDATE-1 contract)
DELETE /{id}             → /memory/{id}            (DIST-OBSIDIAN-LITE-1: lifted from prior 405; cascades vector via mem.delete)
DELETE /graph/nodes/{id} → /memory/graph/nodes/{id} (405 — entity-node ops still read-only)

All endpoints acquire _rw_lock for thread safety under uvicorn's thread pool.
"""

import ipaddress
import logging
import re
import threading
from typing import Any, Literal, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from smartmemory_app.config import UnconfiguredError, llm_key_present
from smartmemory_app.storage import get_memory

log = logging.getLogger(__name__)

# DIST-DAEMON-1: All endpoints serialize through this lock. Uvicorn runs sync
# endpoints in a thread pool — without locking, concurrent ingest+clear or
# ingest+read could race on the SmartMemory singleton's non-thread-safe state.
_rw_lock = threading.RLock()

# One-shot guard so the "no LLM key → Tier-2 disabled" warning is logged once per
# daemon lifetime instead of on every line of a batch ingest (the per-request
# signal still rides back to the user in the response `warning` field every time).
_llm_warned = False

api = FastAPI(title="SmartMemory Local API", docs_url=None, redoc_url=None)

# Fields that normalizeAPIResponse reads at top level (normalize.js:38,46).
# Also includes entity-detection fields (normalize.js:38): node_category, entity_type.
# Everything else from the properties blob lands in metadata.
_TOP_LEVEL_FIELDS = frozenset(
    {
        "label",
        "content",
        "memory_type",
        "category",
        "confidence",
        "created_at",
        "node_category",
        "entity_type",
    }
)


def _get_mem():
    """Return the active memory backend. Converts config errors → meaningful HTTP responses.

    FastAPI surfaces unhandled exceptions as 500. Two typed exceptions escape get_memory():
      UnconfiguredError — no config exists; HTTP 503 (run setup to fix)
      ValueError        — invalid mode value in env var; HTTP 400 (fix the env var)
    """
    try:
        return get_memory()
    except UnconfiguredError as e:
        raise HTTPException(
            status_code=503,
            detail=f"SmartMemory not configured. Run: smartmemory setup. ({e})",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"SmartMemory misconfigured: {e}",
        )


def _get_backend():
    """Return the SQLiteBackend directly — avoids going through SmartMemory pipeline.

    Routes through _get_mem() so UnconfiguredError converts to HTTP 503, not 500.
    Local mode only — callers must take the remote branch before calling this.
    """
    return _get_mem()._graph.backend


def _flatten_node(raw: dict) -> dict:
    """Reshape a serialize() node dict to the flat shape normalizeAPIResponse expects.

    serialize() (sqlite.py:329-335) returns:
        {item_id, memory_type, valid_from, valid_to, created_at, properties: {...}}

    normalizeAPIResponse (normalize.js:38,46) reads label/content/category/
    node_category/entity_type/confidence at the TOP LEVEL — not under properties.

    Note: get_node() / get_neighbors() use _row_to_node() (sqlite.py:119-127) which
    is already flat — do NOT call _flatten_node() on their output.
    """
    props = raw.get("properties", {})
    return {
        "item_id": raw.get("item_id") or raw.get("id", ""),
        # memory_type is a column (stripped from blob at add_node:153) — read from raw
        "memory_type": raw.get("memory_type", props.get("memory_type", "semantic")),
        "created_at": raw.get("created_at", props.get("created_at", "")),
        # All other viewer fields live inside the properties blob
        "label": props.get("label", ""),
        "content": props.get("content", ""),
        "category": props.get("category"),
        "node_category": props.get("node_category"),
        "entity_type": props.get("entity_type"),
        "confidence": props.get("confidence", 1.0),
        "metadata": {k: v for k, v in props.items() if k not in _TOP_LEVEL_FIELDS},
    }


@api.get("/graph/full")
def get_graph_full() -> dict:
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.get_graph_full()
    with _rw_lock:
        backend = _get_backend()
        snapshot = backend.serialize()
    nodes = [_flatten_node(n) for n in snapshot.get("nodes", [])]
    # Filter out internal Version nodes — Cytoscape crashes if edges reference
    # nodes that aren't in the graph (HAS_VERSION edges → missing Version targets).
    node_ids = set()
    filtered_nodes = []
    for n in nodes:
        if n.get("memory_type") == "Version":
            continue
        filtered_nodes.append(n)
        node_ids.add(n["item_id"])
    # Only include edges where both endpoints exist in the filtered node set
    edges = [
        e
        for e in snapshot.get("edges", [])
        if e.get("source_id") in node_ids and e.get("target_id") in node_ids
    ]
    return {
        "nodes": filtered_nodes,
        "edges": edges,
        "node_count": len(filtered_nodes),
        "edge_count": len(edges),
    }


class EdgesBulkRequest(BaseModel):
    node_ids: list[str]


@api.post("/graph/edges")
def get_edges_bulk(body: EdgesBulkRequest) -> dict:
    """Matches createFetchAdapter.getEdgesBulk — POST with {node_ids} body (fetchAdapter.js:35)."""
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.get_edges_bulk(body.node_ids)
    with _rw_lock:
        backend = _get_backend()
        seen: set[tuple] = set()
        edges: list[dict] = []
        for node_id in body.node_ids:
            for edge in backend.get_edges_for_node(node_id):
                key = (edge["source_id"], edge["target_id"], edge["edge_type"])
                if key not in seen:
                    seen.add(key)
                    edges.append(edge)
    return {"edges": edges}


# Option B delete endpoint — /graph/nodes/{node_id} via router mounted at /graph.
# Both delete endpoints must exist: fetchAdapter.js:48-49 calls two separate paths.
_graph_router = APIRouter()


@_graph_router.delete("/nodes/{node_id}")
def delete_entity_node_405(node_id: str) -> Response:
    """Local viewer is read-only. Entity node deletes return 405."""
    return Response(status_code=405, content="Local viewer is read-only.")


api.include_router(_graph_router, prefix="/graph")


@api.get("/list")
def list_memories(limit: int = 200, offset: int = 0) -> dict:
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        full = mem.get_graph_full()
        nodes = full.get("nodes", [])
        paginated = nodes[offset : offset + limit]
        return {
            "items": paginated,
            "total": len(nodes),
            "limit": limit,
            "offset": offset,
        }
    with _rw_lock:
        backend = _get_backend()
        snapshot = backend.serialize()
    nodes = [_flatten_node(n) for n in snapshot.get("nodes", [])]
    paginated = nodes[offset : offset + limit]
    return {"items": paginated, "total": len(nodes), "limit": limit, "offset": offset}


@api.get("/progress/stream")
async def progress_stream(
    request: Request,
    run_id: str = Query(
        None, description="Filter to a single run. Required when from_seq is present."
    ),
    from_seq: int = Query(
        None, description="Per-run replay starting point. Must accompany run_id."
    ),
    since: str = Query(
        None, description="Event ID to resume from. Mutually exclusive with from_seq."
    ),
):
    """SSE progress stream for the graph viewer (lite-mode parity).

    PLAT-PUSH-SSE-1: same wire contract as the hosted route
    (`progress-event-contract.json` SSEEndpoint) — `id:` line, all three
    query modes, the documented 400/404 errors. Frames are produced by the
    events_server drain loop; replay is served from its bounded history
    ring instead of a Redis stream.

    Declared BEFORE /{memory_id} routes — FastAPI matches in declaration
    order and the bare /{memory_id} would otherwise be ambiguous.
    """
    import asyncio
    import json

    from smartmemory_app import events_server

    # Honor Last-Event-ID for reconnect (maps to the scope_resume mode), the
    # same precedence rule the hosted route applies.
    last_event_id = request.headers.get("last-event-id") or request.headers.get(
        "Last-Event-ID"
    )
    if last_event_id:
        since = last_event_id
        from_seq = None

    # Query validation — mirrors the hosted route's 400 cases. Unsupported
    # combinations are refused, never silently ignored.
    if from_seq is not None and run_id is None:
        raise HTTPException(
            status_code=400,
            detail="from_seq requires run_id. Provide run_id=lite with from_seq.",
        )
    if from_seq is not None and since is not None:
        raise HTTPException(
            status_code=400,
            detail="from_seq and since are mutually exclusive.",
        )
    if run_id is not None and run_id != events_server.LITE_RUN_ID:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No events for run_id={run_id!r}. The lite daemon emits a single run: {events_server.LITE_RUN_ID!r}."
            ),
        )

    replay: list = []
    if since is not None:
        replay = events_server.history_since(since)
    elif from_seq is not None:
        replay = events_server.history_from_seq(from_seq)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    entry = events_server.register_sse_subscriber(loop, queue)

    def _sse_frame(event_id: str, frame: dict) -> str:
        return f"id: {event_id}\ndata: {json.dumps(frame)}\n\n"

    async def _gen():
        try:
            # Immediate comment so the client's onopen fires without waiting
            # for the first graph event.
            yield ": connected\n\n"
            replayed_ids = set()
            for event_id, frame in replay:
                if await request.is_disconnected():
                    return
                replayed_ids.add(event_id)
                yield _sse_frame(event_id, frame)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_id, frame = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # per contract SSE keepalive
                    continue
                # A frame can land in both the replay snapshot and the live
                # queue if it was emitted between the two; dedupe on id.
                if event_id in replayed_ids:
                    continue
                yield _sse_frame(event_id, frame)
        finally:
            events_server.unregister_sse_subscriber(entry)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# IMPORTANT: /{memory_id}/neighbors MUST be declared BEFORE /{memory_id}.
# FastAPI matches in declaration order — the bare /{memory_id} would otherwise
# capture /neighbors as the memory_id value.


@api.get("/recall")
def recall_endpoint(
    cwd: str = None,
    top_k: int = 10,
    query: str = None,
    workspace_id: str = None,
    include_snapshot: bool = True,
    strict: bool = False,
) -> dict:
    """Recall recent + relevant memories for session context.

    HOOK-RECALL-RELEVANCE-1: workspace_id, query, include_snapshot, strict
    params added. Backward compatible — existing callers passing only
    cwd+top_k still work. strict=True drops legacy items with no
    workspace_id (eliminates Alice/Atlas-style cross-workspace leak).
    """
    with _rw_lock:
        from smartmemory_app.storage import recall

        context = recall(
            cwd,
            top_k,
            query=query,
            workspace_id=workspace_id or None,
            include_snapshot=include_snapshot,
            strict=strict,
        )
    return {"context": context}


@api.get("/{memory_id}/neighbors")
def get_neighbors(memory_id: str) -> dict:
    """Neighbors with direction-tagged link types.

    DIST-OBSIDIAN-LITE-1: each neighbor entry now includes a `direction` field
    ("outgoing" | "incoming") so clients can disambiguate asymmetric edges
    (e.g. SUPERSEDES, written one-way newer→older). Mirrors the service
    contract added in DIST-OBSIDIAN-1 (links.py:148-158).

    Response shape:
      {
        "neighbors": [{"item_id": str, "link_type": str, "direction": "outgoing"|"incoming"}, ...],
        "edges": [...]   # unchanged; raw edge rows
      }

    Adapter contract preserved: createFetchAdapter.getNeighbors reads
    res?.neighbors || [] — still a list, just with richer entries.
    """
    return _get_neighbor_payload(memory_id)


@api.get("/{memory_id}/links")
def get_links(memory_id: str) -> dict:
    """All edges incident to a node in the {source_id, target_id, link_type}
    shape the graph adapter's getLinks consumer expects (useGraphData.js
    fallback path) and the hosted service route returns.

    DIST-OBSIDIAN-LITE-PARITY-1: was missing on the daemon, so the Obsidian
    graph's per-node link fallback and any links() call 404'd in lite mode.
    """
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        # RemoteMemory carries raw edges on its neighbors payload; reshape.
        edges = mem.get_neighbors(memory_id).get("edges", []) or []
    else:
        with _rw_lock:
            edges = _get_backend().get_edges_for_node(memory_id)
    links = []
    for e in edges:
        link_type = e.get("edge_type") or e.get("link_type")
        if not link_type:
            continue  # malformed edge — skip rather than emit link_type=None
        links.append(
            {
                "source_id": e.get("source_id"),
                "target_id": e.get("target_id"),
                "link_type": link_type,
            }
        )
    return {"links": links}


@api.get("/{memory_id}/lineage")
def get_lineage(memory_id: str) -> dict:
    """Walk the derived_from chain from an item back to its root (the item
    with no derived_from). Mirrors the hosted service route verbatim
    (smart-memory-service crud.py:634) so the Obsidian LineagePanel renders
    identically in lite mode. Depth-capped at 20 to bound cycles.

    DIST-OBSIDIAN-LITE-PARITY-1: was missing on the daemon — the panel showed
    "No derivation history" for every note in lite mode.
    """
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    is_remote = isinstance(mem, RemoteMemory)

    def _get_node(node_id: str) -> Optional[dict]:
        if is_remote:
            return mem.get_node(node_id)
        with _rw_lock:
            return _get_backend().get_node(node_id)

    chain: list[dict] = []
    current_id: Optional[str] = memory_id
    seen: set = set()
    while current_id and len(chain) < 20 and current_id not in seen:
        seen.add(current_id)
        node = _get_node(current_id)
        if node is None:
            break
        derived_from = node.get("derived_from")
        chain.append(
            {
                "item_id": node.get("item_id") or node.get("id") or current_id,
                "content": (node.get("content") or "")[:200],
                "memory_type": node.get("memory_type"),
                "derived_from": derived_from,
                "confidence": node.get("confidence"),
            }
        )
        current_id = derived_from
    return {"lineage": chain, "depth": len(chain)}


@api.get("/{memory_id}")
def get_memory_item(memory_id: str) -> dict[str, Any]:
    """get_node() uses _row_to_node() — output is already flat, no transformation needed."""
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        node = mem.get_node(memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return node
    with _rw_lock:
        backend = _get_backend()
        node = backend.get_node(memory_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return node


@api.post("/clear")
def clear_all() -> dict:
    """Clear all memories, reset vector index, re-seed patterns.

    Resets the in-process singleton so subsequent API calls return fresh data.
    Publishes graph_cleared event so connected viewers refresh.
    """
    with _rw_lock:
        from smartmemory_app.storage import _resolve_data_dir, _shutdown

        _shutdown()

        data_path = _resolve_data_dir()
        removed = 0
        if data_path.exists():
            for pattern in [
                "*.db",
                "*.db-shm",
                "*.db-wal",
                "*.db-journal",
                "*.usearch",
                "*.json",
                "*.jsonl",
                ".write.lock",
            ]:
                for f in data_path.glob(pattern):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass

        # Re-seed patterns
        from smartmemory_app.setup import _seed_data_dir

        _seed_data_dir()

        # Flush pending enrichment jobs INSIDE lock — prevents a concurrent
        # ingest from enqueuing a job between clear and flush
        from smartmemory_app.async_enrichment import reset_queue

        reset_queue()

        # Publish replacement only after the new data directory is ready. A
        # process-separated worker compares this marker before every read/write
        # and closes any SQLite handle still pointing at the unlinked old file.
        from smartmemory_app.store_generation import bump_store_generation

        bump_store_generation()

    # Publish graph_cleared event (outside lock — emit is thread-safe)
    try:
        from smartmemory_app.event_sink import get_event_sink

        sink = get_event_sink()
        sink.emit(
            "span",
            {
                "component": "graph",
                "operation": "clear_all",
                "name": "graph.clear_all",
                "nuclear": True,
            },
        )
    except Exception:
        pass

    return {"cleared": removed}


class InternalGraphEvent(BaseModel):
    """A graph mutation already committed by the process-separated worker.

    PLAT-PUSH-SSE-1: deletes and clears are representable. Before, the bridge
    accepted adds only, so a worker-side delete never reached the viewer and
    the graph drifted out of sync with the store.
    """

    operation: Literal["add_node", "add_edge", "delete_node", "delete_edge", "clear"]
    data: dict[str, Any]


class InternalEventsRequest(BaseModel):
    events: list[InternalGraphEvent]


@api.post("/_internal/events")
def publish_internal_events(body: InternalEventsRequest, request: Request) -> dict:
    """Bridge loopback Tier-2 notifications into the daemon's viewer sink."""
    client_host = request.client.host if request.client is not None else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(
            status_code=403,
            detail="Internal event publication is restricted to loopback clients.",
        )

    from smartmemory_app.event_sink import get_event_sink

    sink = get_event_sink()
    for event in body.events:
        data = dict(event.data)
        if event.operation in ("add_node", "delete_node") and not (
            data.get("memory_id") or data.get("item_id")
        ):
            raise HTTPException(
                status_code=422, detail=f"{event.operation} event requires an id"
            )
        if event.operation in ("add_edge", "delete_edge") and not (
            data.get("source_id") and data.get("target_id")
        ):
            raise HTTPException(
                status_code=422,
                detail=f"{event.operation} event requires source_id and target_id",
            )
        sink.emit(
            "span",
            {
                **data,
                "component": "graph",
                "operation": event.operation,
                "name": f"graph.{event.operation}",
            },
        )
    return {"accepted": len(body.events)}


@api.post("/reindex")
def reindex() -> dict:
    """Re-embed all memories with the current embedding model.

    Use after changing embedding provider/model to rebuild the vector index
    without losing any data. Blocks writes during re-indexing.
    """
    import json
    import os
    import sqlite3
    import time

    with _rw_lock:
        from smartmemory.plugins.embedding import EmbeddingService
        from smartmemory_app.storage import _resolve_data_dir

        data_dir = str(_resolve_data_dir())
        svc = EmbeddingService()

        # Detect dimensions from a test embedding
        test_vec = svc.embed("dimension probe")
        dims = len(test_vec)

        # Read all memory nodes from SQLite
        db_path = os.path.join(data_dir, "memory.db")
        db = sqlite3.connect(db_path)
        rows = db.execute(
            "SELECT item_id, properties FROM nodes WHERE memory_type IS NOT NULL AND memory_type != 'Version'"
        ).fetchall()
        db.close()

        if not rows:
            return {"reindexed": 0, "dims": dims, "provider": svc.provider}

        # Delete old vector index — will be recreated at new dimension
        from smartmemory.stores.vector.backends.usearch import UsearchVectorBackend

        backend = UsearchVectorBackend(
            persist_directory=data_dir, collection_name="memory"
        )

        t0 = time.time()
        embedded = 0
        skipped = 0
        for item_id, props_json in rows:
            props = json.loads(props_json) if props_json else {}
            content = props.get("content", props.get("label", ""))
            if not content:
                skipped += 1
                continue
            try:
                vec = svc.embed(content[:512])
                backend.upsert(
                    item_id=item_id,
                    embedding=vec.tolist(),
                    metadata={"content": content[:200]},
                )
                embedded += 1
            except Exception:
                skipped += 1

        backend._save()
        elapsed = time.time() - t0

    return {
        "reindexed": embedded,
        "skipped": skipped,
        "total": len(rows),
        "dims": dims,
        "provider": svc.provider,
        "elapsed_s": round(elapsed, 1),
    }


@api.post("/reextract")
def reextract_entities() -> dict:
    """Re-run entity extraction on all stored memories and create entity nodes.

    Use after upgrading from a version that didn't create entity nodes on SQLite.
    Reads each memory, runs EntityRuler (spaCy + seed patterns), creates entity
    nodes and CONTAINS_ENTITY/MENTIONED_IN edges via add_dual_node.
    """
    import json
    import os
    import sqlite3
    import time

    with _rw_lock:
        from smartmemory_app.storage import _resolve_data_dir, get_memory

        data_dir = str(_resolve_data_dir())
        mem = get_memory()

        # Read all user memory nodes (skip entity/relation/Version)
        db_path = os.path.join(data_dir, "memory.db")
        db = sqlite3.connect(db_path)
        user_types = (
            "semantic",
            "episodic",
            "procedural",
            "pending",
            "zettel",
            "reasoning",
            "opinion",
            "observation",
            "decision",
        )
        placeholders = ",".join("?" * len(user_types))
        rows = db.execute(
            f"SELECT item_id, properties, memory_type FROM nodes WHERE memory_type IN ({placeholders})",
            user_types,
        ).fetchall()
        db.close()

        if not rows:
            return {"extracted": 0, "entities_created": 0, "total": 0, "elapsed_s": 0}

        # Get EntityRuler stage from the pipeline
        from smartmemory.pipeline.stages.entity_ruler import EntityRulerStage, _get_nlp
        from smartmemory.pipeline.state import PipelineState

        nlp = _get_nlp()
        pattern_manager = getattr(mem, "_entity_ruler_patterns", None)
        ruler = EntityRulerStage(nlp=nlp, pattern_manager=pattern_manager)

        # Build a minimal pipeline config for entity_ruler
        pipeline_config = mem._build_pipeline_config()

        t0 = time.time()
        extracted = 0
        entities_created = 0
        skipped = 0
        backend = mem._graph.backend

        for item_id, props_json, memory_type in rows:
            props = json.loads(props_json) if props_json else {}
            content = props.get("content", "")
            if not content or len(content.strip()) < 3:
                skipped += 1
                continue

            # Check if this memory already has entity edges
            existing_edges = backend.get_edges_for_node(item_id)
            has_entities = any(
                e.get("edge_type") in ("CONTAINS_ENTITY", "MENTIONED_IN")
                for e in existing_edges
            )
            if has_entities:
                skipped += 1
                continue

            # Run EntityRuler on the content
            try:
                state = PipelineState(text=content, memory_type=memory_type)
                state = ruler.execute(state, pipeline_config)
                entities = state.ruler_entities or []

                if not entities:
                    skipped += 1
                    continue

                # Build entity_nodes for add_dual_node
                entity_nodes = []
                for ent in entities:
                    name = ent.get("name", "")
                    etype = ent.get("entity_type", "concept")
                    if not name:
                        continue
                    entity_nodes.append(
                        {
                            "entity_type": etype,
                            "properties": {
                                "name": name,
                                "confidence": ent.get("confidence", 0.85),
                                "source": "reextract",
                            },
                        }
                    )

                if entity_nodes:
                    # Create entity nodes + edges (memory node already exists)
                    for en in entity_nodes:
                        ename = en["properties"]["name"]
                        etype = en["entity_type"]
                        canonical_key = f"{ename.lower()}::{etype.lower()}"

                        # Find or create entity node
                        existing_eid = backend._find_entity_by_canonical_key(
                            canonical_key
                        )
                        if existing_eid:
                            eid = existing_eid
                        else:
                            import uuid

                            eid = str(uuid.uuid4())
                            backend.add_node(
                                eid,
                                {
                                    "content": ename,
                                    "name": ename,
                                    "entity_type": etype,
                                    "canonical_key": canonical_key,
                                    "memory_type": "entity",
                                },
                                memory_type="entity",
                            )
                            entities_created += 1

                        backend.add_edge(item_id, eid, "CONTAINS_ENTITY", {})
                        backend.add_edge(eid, item_id, "MENTIONED_IN", {})

                    extracted += 1
            except Exception as e:
                log.warning("Re-extraction failed for %s: %s", item_id, e)
                skipped += 1

        elapsed = time.time() - t0

    return {
        "extracted": extracted,
        "entities_created": entities_created,
        "skipped": skipped,
        "total": len(rows),
        "elapsed_s": round(elapsed, 1),
    }


# ── DIST-DAEMON-1: Memory operation endpoints ──────────────────────────────


class IngestRequest(BaseModel):
    content: str
    memory_type: str = "episodic"
    context: Optional[dict] = None
    properties: Optional[dict] = None  # user-supplied key-value properties
    profile_name: Optional[str] = (
        None  # accepted for service contract alignment, ignored in lite
    )
    extractor_name: Optional[str] = (
        None  # DIST-OBSIDIAN-LITE-1: SDK contract; ignored in lite (env-driven tier split decides)
    )
    cwd: Optional[str] = (
        None  # HOOK-RECALL-RELEVANCE-1 G3.B: workspace_id derivation source
    )
    workspace_id: Optional[str] = None  # explicit override; else derived from cwd


@api.post("/ingest")
def ingest_endpoint(body: IngestRequest) -> dict:
    """Ingest content through the pipeline. Two-tier when LLM key available.

    Tier 1 (sync, ~4ms): spaCy + EntityRuler → returns item_id immediately.
    Tier 2 (async, ~740ms): background LLM extraction if API key is set.

    HOOK-RECALL-RELEVANCE-1 G3.B: stamps `metadata.workspace_id` on the item
    so future workspace-scoped recall can filter. Derivation priority:
      explicit body.workspace_id > derive_workspace_id(body.cwd) >
      SMARTMEMORY_WORKSPACE_ID env var > None (legacy untagged).
    """
    from smartmemory_app.recall_format import derive_workspace_id

    memory_type = body.memory_type
    if body.context and "memory_type" in body.context:
        memory_type = body.context["memory_type"]

    # Auto-stamp workspace_id so this item is filterable in scoped recall.
    workspace_id = body.workspace_id or derive_workspace_id(body.cwd)
    properties = dict(body.properties or {})
    if workspace_id and "workspace_id" not in properties:
        properties["workspace_id"] = workspace_id

    # DIST-LITE-QUIET-1: the daemon is a generic local HTTP surface, not CLI-private —
    # so it stays producer-neutral. Each PRODUCER declares its own origin in the request
    # context (the `smartmemory add` CLI sends context.origin="cli:add"). A client that
    # omits it gets origin=None → core attributes it 'unknown' honestly, rather than this
    # endpoint mislabeling every caller as tier-1 CLI content (origin drives visibility
    # AND precedence). [Codex review, 2026-06-07]
    origin = (body.context or {}).get("origin")

    from smartmemory_app.config import llm_key_present
    from smartmemory_app.remote_backend import RemoteBackendError
    from fastapi import HTTPException

    has_llm = llm_key_present()

    if has_llm:
        # Two-tier: Tier 1 sync (spaCy), enqueue Tier 2 (LLM) via SQLite queue.
        # A separate worker process drains the queue — no threading issues.
        with _rw_lock:
            from smartmemory_app.storage import ingest

            try:
                result = ingest(
                    body.content,
                    memory_type,
                    sync=False,
                    properties=properties,
                    origin=origin,
                )
            except RemoteBackendError as e:
                raise HTTPException(
                    status_code=502, detail=f"Hosted SmartMemory API error: {e}"
                )
            item_id = result["item_id"] if isinstance(result, dict) else result
            raw_ids = result.get("entity_ids", {}) if isinstance(result, dict) else {}
            entity_ids = {k.lower(): v for k, v in raw_ids.items()} if raw_ids else {}
            already_queued = (
                result.get("queued", False) if isinstance(result, dict) else False
            )
            if not already_queued:
                from smartmemory_app.enrichment_queue import enqueue

                enqueue(item_id, entity_ids)
        return {"item_id": item_id}
    else:
        # No LLM key — Tier-1 only (spaCy). This is a real capability downgrade:
        # no entity extraction, no enrichment, weaker semantic index. Per
        # no-silent-degradation, say so — both in the daemon log (once) and to
        # the caller (every time, so the CLI can surface it).
        global _llm_warned
        if not _llm_warned:
            log.warning(
                "Ingesting without an LLM API key — Tier-2 entity extraction and "
                "enrichment are DISABLED (Tier-1 spaCy only). Add a key with "
                "`smartmemory setup` to enable full extraction."
            )
            _llm_warned = True
        with _rw_lock:
            from smartmemory_app.storage import ingest

            try:
                # Tier-1 ONLY (spaCy + EntityRuler). MUST pass sync=False: the default
                # sync=True runs the full core pipeline including llm_extract, which
                # hard-requires a cloud LLM key and 500s on a keyless lite install
                # (ValueError: No API key found → Stage 'llm_extract' failed). There is
                # no Tier-2 enqueue here — no key means no enrichment worker to drain it.
                result = ingest(
                    body.content,
                    memory_type,
                    sync=False,
                    properties=properties,
                    origin=origin,
                )
            except RemoteBackendError as e:
                raise HTTPException(
                    status_code=502, detail=f"Hosted SmartMemory API error: {e}"
                )
            item_id = result["item_id"] if isinstance(result, dict) else result
        return {
            "item_id": item_id,
            "warning": "No LLM key configured — stored with Tier-1 (spaCy) extraction "
            "only; entity extraction and enrichment are disabled. "
            "Run `smartmemory setup` to add a key.",
        }


class SearchRequest(BaseModel):
    multi_hop: bool = False
    max_hops: int = 3
    hop_strategy: str | None = None
    since: str | None = None
    until: str | None = None
    query: str
    top_k: int = 5
    filters: Optional[dict] = None  # property filters (e.g. {"project": "atlas"})
    # Lite uses FTS5 lexical fill plus vector search; enable_hybrid does not select the lite dispatch path.
    enable_hybrid: bool = True
    memory_type: Optional[str] = (
        None  # DIST-OBSIDIAN-LITE-1: SDK contract; folded into filters
    )


def _search_items(body: SearchRequest) -> list[dict]:
    """Run the shared semantic-search path for search and ask requests."""
    from fastapi import HTTPException

    # DIST-OBSIDIAN-LITE-1: fold memory_type into filters so storage.search sees
    # a single filter dict regardless of which contract surface the caller used.
    filters = dict(body.filters or {})
    if body.memory_type:
        filters.setdefault("memory_type", body.memory_type)

    with _rw_lock:
        from smartmemory_app.storage import search
        from smartmemory_app.remote_backend import RemoteBackendError

        try:
            results = search(
                body.query,
                body.top_k,
                filters=filters or None,
                **{
                    k: v
                    for k, v in {
                        "since": body.since,
                        "until": body.until,
                        "multi_hop": True if body.multi_hop else None,
                        "max_hops": body.max_hops if body.multi_hop else None,
                        "hop_strategy": body.hop_strategy,
                    }.items()
                    if v is not None
                },
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except RemoteBackendError as e:
            raise HTTPException(
                status_code=502, detail=f"Hosted SmartMemory API error: {e}"
            )
    return results


@api.post("/search")
def search_endpoint(body: SearchRequest) -> dict:
    """Search memories. Returns {items: [...]} matching post-CORE-CRUD-LIST service contract."""
    from smartmemory.search import search_window_coverage

    if body.hop_strategy is not None and body.hop_strategy not in (
        "consensus",
        "relevance",
        "semantic",
    ):
        raise HTTPException(
            status_code=400,
            detail="hop_strategy must be consensus, relevance, semantic",
        )
    result = {"items": _search_items(body)}
    if body.hop_strategy is not None and not body.multi_hop:
        result["inert_parameters"] = {"hop_strategy": "multi_hop is false"}
    coverage = search_window_coverage(body.since, body.until)
    if coverage:
        result["coverage"] = coverage
    return result


class AskRequest(BaseModel):
    question: str
    limit: int = 5


_STRUCTURAL_EDGE_TYPES = frozenset(
    {"GROUNDED_IN", "CONTAINS_ENTITY", "MENTIONED_IN", "HAS_VERSION"}
)


def _get_neighbor_payload(memory_id: str) -> dict:
    """Return the same neighbor-and-edge payload exposed by ``/{id}/neighbors``."""
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.get_neighbors(memory_id)
    with _rw_lock:
        backend = _get_backend()
        edges = backend.get_edges_for_node(memory_id)

    seen: set[tuple[str, str, str]] = set()
    neighbors = []
    for edge in edges:
        source_id = edge.get("source_id")
        target_id = edge.get("target_id")
        link_type = edge.get("edge_type") or edge.get("link_type")
        if not link_type:
            continue
        if source_id == memory_id and target_id and target_id != memory_id:
            direction, other_id = "outgoing", target_id
        elif target_id == memory_id and source_id and source_id != memory_id:
            direction, other_id = "incoming", source_id
        else:
            continue
        key = (other_id, str(link_type), direction)
        if key in seen:
            continue
        seen.add(key)
        neighbors.append(
            {"item_id": other_id, "link_type": link_type, "direction": direction}
        )
    return {"neighbors": neighbors, "edges": edges}


def _node_label(item_id: str) -> str:
    """Resolve a graph-node ID to its human-readable label when available."""
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        node = mem.get_node(item_id)
    else:
        with _rw_lock:
            node = _get_backend().get_node(item_id)
    if not isinstance(node, dict):
        return item_id
    return str(node.get("label") or node.get("name") or node.get("content") or item_id)


def _ask_relations(hits: list[dict]) -> list[dict[str, str]]:
    """Collect one-hop entity relation edges for the memories used as evidence."""
    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        memory_id = hit.get("item_id") or hit.get("id")
        if not isinstance(memory_id, str) or not memory_id:
            continue
        neighbors = _get_neighbor_payload(memory_id).get("neighbors", [])
        for neighbor in neighbors:
            entity_id = neighbor.get("item_id") if isinstance(neighbor, dict) else None
            if not isinstance(entity_id, str) or not entity_id:
                continue
            for edge in _get_neighbor_payload(entity_id).get("edges", []):
                if not isinstance(edge, dict):
                    continue
                source_id = edge.get("source_id")
                target_id = edge.get("target_id")
                relation_type = edge.get("edge_type") or edge.get("link_type")
                if (
                    not isinstance(source_id, str)
                    or not isinstance(target_id, str)
                    or not isinstance(relation_type, str)
                    or memory_id in (source_id, target_id)
                    or relation_type.upper() in _STRUCTURAL_EDGE_TYPES
                ):
                    continue
                key = (source_id, relation_type, target_id)
                if key in seen:
                    continue
                seen.add(key)
                # DIST-LITE-9: labels are what a reader sees, ids are what a UI focuses.
                # `AskPanel` addresses the edge as `${source_id}->${target_id}:${type}`,
                # the same id the graph package's normalizer builds — a label pair cannot
                # address a graph element. Contract:
                # smart-memory-docs/docs/features/DIST-LITE-9/ask-contract.json
                relations.append(
                    {
                        "source": _node_label(source_id),
                        "type": relation_type,
                        "target": _node_label(target_id),
                        "source_id": source_id,
                        "target_id": target_id,
                    }
                )
    return relations


def _ask_prompt(question: str, evidence: list[dict], relations: list[dict]) -> str:
    """Format retrieved memories and graph edges as the model's grounded evidence."""
    memories = (
        "\n".join(f"- [{item['item_id']}] {item['content']}" for item in evidence)
        or "- (no matching memories)"
    )
    edges = (
        "\n".join(
            f"- {edge['source']} --{edge['type']}--> {edge['target']}"
            for edge in relations
        )
        or "- (no graph relations found)"
    )
    return (
        f"Question: {question}\n\n"
        f"Retrieved memories:\n{memories}\n\n"
        f"Graph relations:\n{edges}\n\n"
        "Answer using only this evidence. Respond in exactly this format:\n"
        "ANSWER: <one or two plain sentences that answer the question directly>\n"
        "REASONING: <concise reasoning that cites the memories and relations used>\n"
        "If the evidence is insufficient, say so plainly in ANSWER."
    )


def _split_answer(text: str) -> tuple[str, str]:
    """Split the model reply into (answer, reasoning) on the ANSWER:/REASONING: markers.

    Falls back to (first paragraph, remainder) when the model ignores the format,
    so the CLI never shows an empty answer.
    """
    body = text.strip()
    m = re.search(r"ANSWER:\s*(.*?)\s*(?:REASONING:\s*(.*))?$", body, re.S | re.I)
    if m and m.group(1).strip():
        return m.group(1).strip(), (m.group(2) or "").strip()
    parts = re.split(r"\n\s*\n", body, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


@api.post("/ask")
def ask_endpoint(body: AskRequest) -> dict:
    """Answer a question from semantic memories plus their one-hop graph relations."""
    if not llm_key_present():
        raise HTTPException(
            status_code=503,
            detail="`sm ask` requires a configured LLM key (for example GROQ_API_KEY). "
            "Run `smartmemory setup` or set a supported provider key.",
        )

    hits = _search_items(SearchRequest(query=body.question, top_k=body.limit))
    evidence = [
        {
            "item_id": str(hit.get("item_id") or hit.get("id") or ""),
            "content": str(hit.get("content") or ""),
        }
        for hit in hits
        if isinstance(hit, dict) and (hit.get("item_id") or hit.get("id"))
    ]
    relations = _ask_relations(hits)

    try:
        from smartmemory.utils.llm import call_llm

        _, answer = call_llm(
            system_prompt=(
                "You are SmartMemory's evidence-grounded question-answering assistant. "
                "Do not invent facts beyond the provided memories and graph relations."
            ),
            user_content=_ask_prompt(body.question, evidence, relations),
            max_output_tokens=500,
            temperature=0,
        )
    except Exception as exc:
        log.exception("sm ask LLM request failed")
        detail = f"Unable to answer with the configured LLM: {exc}"
        if isinstance(exc, ImportError) and (
            exc.name == "openai" or str(exc) == "openai package is required"
        ):
            detail = (
                "Python dependency 'openai' is missing; its SDK provides transport for "
                "Groq and other OpenAI-compatible providers. No OpenAI API key is required. "
                "Reinstall/upgrade: pip install --upgrade --force-reinstall smartmemory "
                "(core-library users: pip install 'smartmemory-core[llm]')."
            )
        raise HTTPException(status_code=502, detail=detail)
    if not isinstance(answer, str) or not answer.strip():
        raise HTTPException(
            status_code=502,
            detail="The configured LLM returned no answer; no fallback answer was generated.",
        )
    direct, reasoning = _split_answer(answer)
    return {
        "answer": direct,
        "reasoning": reasoning,
        "evidence": evidence,
        "relations": relations,
    }


# LAUNCH-METRICS-1: daemon-side ingest. Remote mode forwards to the configured
# service (authenticated) so hosted-funnel aggregation sees CLI events; local
# mode appends to a JSONL file in the data dir (local data stays local). A
# failed forward falls back to the local JSONL — never discarded. Best-effort.
@api.post("/launch/event")
def ingest_launch_event(body: dict) -> dict:
    import json as _json
    import time as _time
    import uuid as _uuid

    event_type = body.get("event_type")
    if not event_type:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="event_type required")
    props = body.get("props") or {}

    try:
        from smartmemory_app.config import get_api_key, load_config

        cfg = load_config()
    except Exception:
        cfg = None
    if cfg is not None and cfg.mode == "remote":
        api_key = ""
        try:
            api_key = get_api_key()
        except Exception:
            pass
        if api_key:
            try:
                import httpx

                r = httpx.post(
                    f"{cfg.api_url.rstrip('/')}/memory/launch/event",
                    json={"event_type": event_type, "props": props},
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=5.0,
                )
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                log.warning(
                    "launch_metrics: remote forward failed, falling back to local JSONL: %s",
                    exc,
                )
        else:
            log.warning(
                "launch_metrics: remote mode but no API key available; writing local JSONL only"
            )

    record = {
        "event_id": _uuid.uuid4().hex,
        "event_type": event_type,
        "props": props,
        "ts": _time.time(),
    }
    try:
        from smartmemory_app.storage import _resolve_data_dir

        data_path = _resolve_data_dir()
        data_path.mkdir(parents=True, exist_ok=True)
        path = data_path / "launch_events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(record) + "\n")
    except Exception as exc:
        log.warning("launch_metrics: daemon write failed: %s", exc)
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=str(exc))
    return {"event_id": record["event_id"], "event_type": event_type}


# recall endpoint moved above /{memory_id} to avoid wildcard route capture


# ── DIST-OBSIDIAN-LITE-1: PATCH + DELETE for SDK CRUD parity ───────────────


class UpdateRequest(BaseModel):
    """CORE-CRUD-UPDATE-1 contract; matches SDK MemoryAPI.update body."""

    content: Optional[str] = None
    metadata: Optional[dict] = None
    properties: Optional[dict] = None
    write_mode: Optional[str] = "merge"  # 'merge' | 'replace'


@api.patch("/{memory_id}")
def update_memory_item(memory_id: str, body: UpdateRequest) -> dict:
    """Update a memory's properties. Maps to SmartMemory.update_properties.

    DIST-OBSIDIAN-LITE-1: enables the Obsidian plugin's metadata-only re-ingest
    path (PATCH instead of POST) when running against the daemon.
    """
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        # RemoteMemory has no update_node — proxy mode is out of scope for
        # DIST-OBSIDIAN-LITE-1. Surface explicitly rather than AttributeError.
        raise HTTPException(
            status_code=501, detail="PATCH not available in remote-proxy mode"
        )
    with _rw_lock:
        # CORE-CRUD-UPDATE-1: properties wins over content/metadata conveniences
        props = dict(body.properties or {})
        if body.content is not None and "content" not in props:
            props["content"] = body.content
        if body.metadata is not None:
            # Flat-merge metadata into properties; deep-merge happens on the
            # service side, but daemon's storage is single-level by convention.
            for k, v in body.metadata.items():
                props.setdefault(k, v)
        try:
            mem.update_properties(
                memory_id, props, write_mode=body.write_mode or "merge"
            )
        except ValueError as e:
            # smart_memory.py:2012 → memory/pipeline/stages/crud.py:409 raises
            # ValueError("Node {item_id} not found in graph.") on missing item.
            # Match on the message so other ValueError categories (validation
            # failures, etc.) bubble as 500 rather than being mis-attributed
            # to a missing item.
            if "not found" in str(e).lower():
                raise HTTPException(status_code=404, detail="Memory not found")
            raise
    return {"item_id": memory_id, "updated": True}


@api.delete("/{memory_id}")
def delete_memory_item(memory_id: str) -> Response:
    """Delete a memory item.

    DIST-OBSIDIAN-LITE-1: lifted from the prior 405 ('Local viewer is read-only')
    policy. Single-user local deployment — no governance reason to refuse delete.
    Plugin uses this for the 'Danger: purge Obsidian-origin memories' command and
    for content-change re-ingest dedupe (workaround for CORE-INGEST-DEDUPE-1).

    Note: graph router's DELETE /graph/nodes/{id} stays 405 — that's a different
    surface (entity-node ops, not memory CRUD).
    """
    mem = _get_mem()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return Response(
            status_code=501, content="DELETE not available in remote-proxy mode"
        )
    with _rw_lock:
        # SmartMemory.delete() at smart_memory.py:2182 delegates to crud.delete()
        # at crud.py:287, which already cascades to vector store + Vec_* nodes.
        # Going through backend.remove_node() directly would leak vector entries.
        ok = mem.delete(memory_id)
    return Response(status_code=204 if ok else 404)
