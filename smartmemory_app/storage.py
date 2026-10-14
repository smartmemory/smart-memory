"""DIST-LITE-5: Storage — dual-mode dispatch (local SQLite or remote hosted API).

get_memory() returns either a local SmartMemory instance or a RemoteMemory instance
depending on config. All callers (server.py MCP tools, local_api.py viewer) go through
this module and duck-type on the result — each operation has an explicit branch
because the return types differ (MemoryItem vs dict, sort_by support, etc.).

Local deps (smartmemory-core, filelock) are hard dependencies — always available.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from filelock import FileLock

if TYPE_CHECKING:
    from smartmemory import SmartMemory
    from smartmemory_app.remote_backend import RemoteMemory

from smartmemory_app.config import (
    SmartMemoryConfig,
    UnconfiguredError,
    _detect_and_migrate,
    is_configured,
    load_config,
)


log = logging.getLogger(__name__)

_memory: "SmartMemory | None" = None
_data_path: Path | None = None  # resolved once on first init; reused by ingest()
_init_lock = threading.Lock()

_remote_memory: "RemoteMemory | None" = None
_remote_init_lock = threading.Lock()

WRITE_LOCK_TIMEOUT = (
    2.0  # seconds — daemon ingest only, worker uses SmartMemory.add() (no lock)
)


# --- Directory & lock resolution -------------------------------------------------


def _resolve_data_dir(explicit: str | None = None) -> Path:
    """Plugin-layer env var adapter. Core factory does not read env vars."""
    raw = explicit or os.environ.get("SMARTMEMORY_DATA_DIR")
    base = Path(raw) if raw else Path.home() / ".smartmemory"
    return base


def _get_lock_file(data_path: Path):
    return FileLock(str(data_path / ".write.lock"), timeout=WRITE_LOCK_TIMEOUT)


# --- Singleton lifecycle ---------------------------------------------------------


def _timed_startup_step(
    on_progress: Callable[[str], None],
    starting: str,
    finished: str,
    action: Callable[[], object],
) -> object:
    """Run one startup action with truthful, flushable progress around it."""
    on_progress(f"{starting}...")
    started = time.perf_counter()
    result = action()
    on_progress(f"{finished} ({time.perf_counter() - started:.1f}s)")
    return result


def _run_startup_warmups(
    on_progress: Callable[[str], None],
    ensure_spacy: Callable[[], object],
    require_embedding: Callable[[], object],
) -> None:
    """Run independent model prerequisites concurrently with truthful progress.

    Progress callbacks stay on the caller thread: both steps are announced before
    submission, and each completion is emitted only after its future resolves. All
    futures are inspected so one failure can never hide a sibling failure.
    """
    steps = (
        (
            "Loading language tools (spaCy)",
            "Language tools ready",
            ensure_spacy,
        ),
        (
            "Checking the local AI model",
            "Local AI model ready",
            require_embedding,
        ),
    )
    for starting, _finished, _action in steps:
        on_progress(f"{starting}...")

    started_at: dict[int, float] = {}
    failures: dict[int, BaseException] = {}
    with ThreadPoolExecutor(
        max_workers=len(steps), thread_name_prefix="smartmemory-warmup"
    ) as executor:
        futures = {}
        for index, (_starting, _finished, action) in enumerate(steps):
            started_at[index] = time.perf_counter()
            futures[executor.submit(action)] = index

        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
            except BaseException as exc:
                failures[index] = exc
            else:
                finished = steps[index][1]
                elapsed = time.perf_counter() - started_at[index]
                on_progress(f"{finished} ({elapsed:.1f}s)")

    ordered_failures = [failures[index] for index in sorted(failures)]
    if len(ordered_failures) == 1:
        raise ordered_failures[0]
    if ordered_failures:
        if all(isinstance(exc, Exception) for exc in ordered_failures):
            raise ExceptionGroup(
                "Startup prerequisite warmups failed", ordered_failures
            )
        raise BaseExceptionGroup(
            "Startup prerequisite warmups failed", ordered_failures
        )


def apply_runtime_config() -> SmartMemoryConfig:
    """Apply persisted provider settings before memory construction or worker spawn."""
    cfg = load_config()
    if not os.environ.get("SMARTMEMORY_EMBEDDING_PROVIDER"):
        os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = cfg.embedding_provider

    # CORE-LLM-GEMINI-1 (review finding 2): pin the LLM model from config so the
    # configured provider is honored. An explicit cfg.llm_model is respected for
    # any provider (previously ignored entirely); a gemini provider with no explicit
    # model gets the Gemini default — core's get_default_model() prioritises by
    # key-presence and (without this) never selected Gemini, so a gemini selection
    # silently routed elsewhere. Mirrors the embedding pin; an explicit
    # SMARTMEMORY_LLM_MODEL env var still wins.
    if not os.environ.get("SMARTMEMORY_LLM_MODEL"):
        _llm_model = cfg.llm_model or (
            "gemini/gemini-2.0-flash" if cfg.llm_provider == "gemini" else None
        )
        if _llm_model:
            os.environ["SMARTMEMORY_LLM_MODEL"] = _llm_model
    return cfg


def _get_local_memory(
    data_dir: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> "SmartMemory":
    """Return the local SmartMemory singleton, initialising on first call.

    Thread-safe via double-checked locking. Registers atexit shutdown on first init.
    Pins the embedding provider from config before creating the memory instance
    so that env var changes (e.g. OPENAI_API_KEY appearing) don't silently switch
    the provider and cause dimension mismatches.
    """
    global _memory, _data_path
    if _memory is not None:
        return _memory
    with _init_lock:
        if _memory is not None:  # double-checked
            return _memory
        cfg = apply_runtime_config()

        from smartmemory.ontology.pattern_manager import PatternManager
        from smartmemory.tools.factory import create_lite_memory
        from smartmemory_app.event_sink import get_event_sink
        from smartmemory_app.patterns import JSONLPatternStore

        # DIST-FULL-LOCAL-1 Phase 2b: apply coreference config to pipeline profile
        from smartmemory.pipeline.config import PipelineConfig

        # Core owns lite defaults and deferred placement. The wrapper only applies
        # its explicit coreference preference; ground/evolve remain opt-in.
        profile = PipelineConfig.lite()
        profile.coreference.enabled = cfg.coreference
        profile.extraction.entity_ruler.spacy_model = cfg.spacy_model
        profile.extraction.relation_ruler.spacy_model = cfg.spacy_model

        data_path = _resolve_data_dir(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        _data_path = data_path  # cache so ingest() uses the same path as the singleton
        pattern_manager = PatternManager(store=JSONLPatternStore(data_path))
        create_kwargs = {
            "data_dir": str(data_path),
            "entity_ruler_patterns": pattern_manager,
            "pipeline_profile": profile,
            "event_sink": get_event_sink(),  # DIST-LITE-3
        }
        if on_progress is None:
            # Direct library callers keep the core factory's established one-shot path.
            _memory = create_lite_memory(
                **create_kwargs,
                auto_download_models=False,
            )
        else:
            # The daemon owns the interactive startup surface. Split core's combined
            # prerequisite check into timed steps, then construct from the verified
            # local files. The final factory call repeats only cheap presence checks.
            from smartmemory.tools.factory import (
                _require_spacy_model,
                _require_embedding_model,
            )

            _run_startup_warmups(
                on_progress,
                lambda: _require_spacy_model(cfg.spacy_model),
                lambda: _require_embedding_model(allow_download=False),
            )
            _memory = _timed_startup_step(
                on_progress,
                "Loading the spaCy language model and opening saved memories",
                "Language model and saved memories ready",
                lambda: create_lite_memory(
                    **create_kwargs,
                    auto_download_models=False,
                ),
            )
        atexit.register(_shutdown)
        # DIST-LITE-WARMSTART-1: warm the local embedder in the background so the
        # user's first add() overlaps the ~12s model load instead of paying it inline.
        # Daemon thread, idempotent, opt out with SMARTMEMORY_NO_WARM=1.
        try:
            from smartmemory_app.warm import warm_models_background

            warm_models_background()
        except Exception:
            pass
        return _memory


def _get_remote_memory(cfg: SmartMemoryConfig) -> "RemoteMemory":
    """Return the RemoteMemory singleton. Double-checked locking, same pattern as local."""
    global _remote_memory
    if _remote_memory is not None:
        return _remote_memory
    with _remote_init_lock:
        if _remote_memory is not None:
            return _remote_memory
        from smartmemory_app.remote_backend import RemoteMemory

        _remote_memory = RemoteMemory(api_url=cfg.api_url, team_id=cfg.team_id)
        return _remote_memory


def get_memory(
    data_dir: str | None = None,
    on_progress: Callable[[str], None] | None = None,
):
    """Return the active memory backend (local or remote).

    Raises UnconfiguredError if no config exists and auto-migration fails.
    Auto-migration: if [local] deps are importable but no config exists, writes
    a local config automatically (upgrade path for existing installations).
    """
    if not is_configured():
        if not _detect_and_migrate():
            raise UnconfiguredError(
                "SmartMemory is not configured. Run: smartmemory setup"
            )
    cfg = load_config()
    if cfg.mode == "remote":
        return _get_remote_memory(cfg)
    return _get_local_memory(data_dir, on_progress=on_progress)


def _shutdown() -> None:
    """Flush state to disk on clean exit. Called by atexit.

    Verified API paths (confirmed against core source):
      CollectionAwareVectorBackend.save_all() — flushes every per-collection backend
                                                 (lite mode injects this registry, not
                                                 a bare UsearchVectorBackend — CORE-VEC-DELETE-1)
      UsearchVectorBackend._save()  — private flush, no public save() (legacy single-backend path)
      SQLiteBackend.close()         — via memory._graph.backend.close() (factory.py:79)
    """
    global _memory
    if _memory is None:
        return
    try:
        if hasattr(_memory, "_vector_backend") and _memory._vector_backend is not None:
            vb = _memory._vector_backend
            if hasattr(vb, "save_all"):
                vb.save_all()
            elif hasattr(vb, "_save"):
                vb._save()
        # Use SmartMemory.close() which shuts down evolution worker, ontology
        # store, and graph backend in the correct order.
        if hasattr(_memory, "close"):
            _memory.close()
        elif hasattr(_memory, "_graph") and hasattr(_memory._graph, "backend"):
            _memory._graph.backend.close()
    except Exception as exc:
        log.warning(
            "SmartMemory shutdown error — session data may not be fully persisted: %s",
            exc,
        )
    finally:
        _memory = None


# --- Helpers -------------------------------------------------------------------


def _normalize_ingest_result(result) -> str:
    """Normalize SmartMemory.ingest() return value to a plain item_id string.

    SmartMemory.ingest() returns Union[str, Dict[str, Any]]:
      str  — when sync=True (default in Lite mode): item_id directly
      dict — when sync=False: {"item_id": str, "queued": bool}
    Lite mode defaults to sync=True, but we normalize both cases defensively.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and "schema_version" not in result:
        return result.get("item_id") or str(result)
    # Receipt arm (N-02.activate.outer-readers): a save receipt reads as its subject id, never str(receipt).
    from smartmemory_app.save_return import resolve_save_return

    resolved = resolve_save_return(result, op="ingest").value
    return resolved if isinstance(resolved, str) else str(resolved)


def _split_recall_counts(top_k: int) -> tuple[int, int]:
    """Split recall budget between recency and semantic passes.

    Keep at least one slot for the recency pass so ``top_k=1`` still returns the
    most recent memory instead of issuing two zero-length searches.
    """
    requested = max(1, top_k)
    recent_k = max(1, (requested + 1) // 2)
    semantic_k = max(0, requested - recent_k)
    return recent_k, semantic_k


# --- Operations ------------------------------------------------------------------


def llm_extraction_warning() -> str | None:
    """Describe missing LLM extraction using the same route resolver as Lite saves.

    API-key presence is insufficient: core also supports local endpoints and
    subscription routes, and rejects keys that cannot serve the selected model.
    Call after runtime configuration has been applied by memory initialization.
    """
    from smartmemory.utils.llm import llm_route_available

    available, _ = llm_route_available()
    if available:
        return None
    return (
        "LLM entity/relation extraction unavailable (no usable provider configured); "
        "ruler extraction and local enrichers still run. "
        "Run `smartmemory setup` to configure an LLM provider."
    )


def ingest(
    content: str,
    memory_type: str = "episodic",
    sync: bool = True,
    properties: dict[str, str] | None = None,
    origin: str | None = None,
):
    """Ingest content into the active backend.

    Args:
        content: Text to ingest.
        memory_type: Memory type (episodic, semantic, etc.).
        sync: Both values save through discoverability and queue deferred core work.
              True returns an item_id string; False returns the core result dict
              including item_id, entity_ids, queued and run_id, plus an optional
              warning when core cannot route LLM extraction.
        properties: Optional user-supplied key-value properties stored in metadata.
        origin: Optional provenance tag (DIST-LITE-QUIET-1). Set by local write
            surfaces (CLI sm add → "cli:add"). Threaded into core ingest via context
            so the stored item is attributed instead of falling to origin='unknown'.

    Remote mode: delegates to RemoteMemory.ingest() — no file lock needed.
    Local mode: acquires filelock before calling SmartMemory.ingest() because
      usearch and entity_patterns.jsonl require cross-process coordination.
      On Timeout: raises filelock.Timeout — caller surfaces as error string.
    """
    mem = get_memory()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.ingest(content, memory_type)  # TODO: pass properties to remote API
    # Reserved keys that user properties must not overwrite.
    # DIST-LITE-QUIET-1 (Codex review): "origin" is reserved — it drives tier visibility
    # AND precedence guards, so it must be set only by the producer (the explicit `origin`
    # param), never via user-supplied properties (which would let a caller claim a
    # privileged origin, e.g. import:vault, and bypass attribution/tiering).
    _RESERVED = frozenset(
        {
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
    )
    ctx: dict = {"memory_type": memory_type}
    if origin:
        ctx["origin"] = origin
    if properties:
        dropped = _RESERVED.intersection(properties)
        if dropped:
            log.warning(
                "Dropped reserved ingest properties: %s", ", ".join(sorted(dropped))
            )
        # Flatten user properties into context so they become top-level node
        # properties (metadata keys merge into graph node properties dict).
        for k, v in properties.items():
            if k not in _RESERVED:
                ctx[k] = v
    # Local path — acquire write lock for cross-process coordination
    data_path = _data_path if _data_path is not None else _resolve_data_dir()
    lock = _get_lock_file(data_path)
    with lock:
        result = mem.ingest(content, context=ctx, sync=sync)
        if not sync:  # receipt arm reads as the legacy dict; legacy returns pass through unchanged
            from smartmemory_app.save_return import ingest_dict

            result = ingest_dict(result, op="ingest")
        if not sync and isinstance(result, dict):
            # Initialization has applied the effective provider/model settings.
            # Use core's placement resolver, not the foreground extraction status:
            # even a configured LLM is deferred and reports ruler_only at save time.
            warning = llm_extraction_warning()
            if warning:
                log.warning("%s", warning)
                return {**result, "warning": warning}
            return result
    return _normalize_ingest_result(result)


def persist_provenance(edits) -> dict:
    """Persist a `SessionEdits` batch (CORE-CODE-PROVENANCE-1 Phase 2a) under the
    SAME cross-process write lock as ingest(), so concurrent detached hooks for the
    same session serialize (one :Session node, intact edges). No-op in remote mode
    — provenance capture is local/lite only.

    Returns the persister's counts dict, or a `{"skipped": ...}` marker.
    """
    if not edits or not getattr(edits, "edits", None):
        return {"evidence": 0, "edges": 0, "skipped": "empty"}
    mem = get_memory()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        log.debug("persist_provenance: remote mode — skipping (local-only capture)")
        return {"evidence": 0, "edges": 0, "skipped": "remote"}
    from smartmemory.provenance.persister import ProvenancePersister

    data_path = _data_path if _data_path is not None else _resolve_data_dir()
    lock = _get_lock_file(data_path)
    with lock:
        return ProvenancePersister(mem).persist(edits)


_WILDCARD_PENDING_WINDOW = 200
_WILDCARD_CORE_CAP = 102_400
#: Reason the wrapper adds, beside core's coverage names, when a widening loop stopped at the bound it may ask for.
_FETCH_BOUND_REASON = "fetch_bound"


def _gated_fetch_cap() -> int | None:
    """The widest fetch one gated search may ask for, or ``None`` with ``sm2_search_visibility_gate`` OFF (legacy, unbounded).

    Two core bounds, the smaller wins: the budget of core's read gate (``search.lane_gate.DEFAULT_MAX_SCAN``), because a
    wider fetch cannot reach candidates the gate does not examine, and the intake read port's page limit
    (``save.read_port.MAX_PAGE_LIMIT``), because a gated search with a bound port asks the port for ``top_k`` rows and
    core refuses a wider ask (``PendingFacadeUnavailable``). Probed with ``switches.is_on`` so the legacy arm gains no
    OFF WARNING.
    """
    from smartmemory.configuration import switches
    from smartmemory.search.visibility_gate import SWITCH

    if not switches.is_on(SWITCH):
        return None
    from smartmemory.save.read_port import MAX_PAGE_LIMIT
    from smartmemory.search.lane_gate import DEFAULT_MAX_SCAN

    return min(DEFAULT_MAX_SCAN, MAX_PAGE_LIMIT)


class WildcardEnumerationOverflowError(RuntimeError):
    """Core's wildcard enumeration still returned a full window at the cap (U5.22, C1/C4)."""


def _result_dict(hit) -> dict:
    """A core read result as a response dict, its read_status carrier checked first (U5.22, C1/C8).

    Core's ``MemoryItem.to_dict()`` copies the response-only carrier with ``dict()``, so an unreadable carrier would
    escape as a raw ValueError/TypeError, and an empty or stateless dict would be copied as valid. Every result this
    module serializes goes through here: absent stays legacy, unreadable is ``MalformedReadStatusError`` (one WARNING).
    """
    from smartmemory.models.read_status import read_status_of

    from smartmemory_app.recall_format import validated_carrier

    item_id = (
        hit.get("item_id") if isinstance(hit, dict) else getattr(hit, "item_id", None)
    )
    validated_carrier(read_status_of(hit), item_id)
    return hit.to_dict() if hasattr(hit, "to_dict") else dict(hit)


def _core_wildcard_rows(mem, **core_kwargs) -> list[dict]:
    """Gate-ON wildcard: core's merged enumeration is the one authority for membership, payload and read_status.

    The wrapper's own snapshot scan reads the graph only. It cannot see a receipt-only (intake) item, a newer
    admission revision of a graph row, an admission tombstone or core's origin/lifecycle exclusions, so it is not
    merged with core's answer: core's answer is used whole. Exhaustion is decided from core's own page: a window that
    comes back full is doubled until it comes back short, so no returned row is left without its carrier. A full
    window at the cap is refused (one WARNING, typed error), never truncated. Gate OFF never calls this.
    """
    from smartmemory.search.visibility_gate import gated_partial_coverage

    window = _WILDCARD_PENDING_WINDOW
    while True:
        hits = mem.search("*", top_k=window, **core_kwargs)
        # U5.21 (C3): a short page is exhaustion only when core did not report its own answer as partial.
        partial = gated_partial_coverage(hits, where="wildcard search")
        if len(hits) > window:
            # C4: the bound caps every fetch. A callee that over-returns broke the window contract, so its page can
            # neither prove exhaustion nor be trusted as complete: refuse, never consume or truncate it.
            log.warning(
                "wildcard search refused: core returned %d hits for a window of %d, so the listing bound is broken",
                len(hits),
                window,
            )
            raise WildcardEnumerationOverflowError(
                f"wildcard enumeration returned {len(hits)} hits for a window of {window}"
            )
        if len(hits) < window and not partial:
            break
        if window >= _WILDCARD_CORE_CAP:
            if len(hits) < window:
                log.warning(
                    "wildcard search refused: core reported a partial answer at the cap of %d hits, "
                    "so the listing would be incomplete",
                    window,
                )
            else:
                log.warning(
                    "wildcard search refused: core still returned a full window of %d hits at the cap, "
                    "so the listing would be incomplete",
                    window,
                )
            raise WildcardEnumerationOverflowError(
                f"wildcard enumeration exceeds {_WILDCARD_CORE_CAP} hits"
            )
        window = min(window * 2, _WILDCARD_CORE_CAP)
    rows = []
    for hit in hits:
        payload = _result_dict(hit)
        rows.append({"confidence": 1.0, "stale": False, "reference": False, **payload})
    return rows


def _list_all_memories(mem) -> list[dict]:
    """Return all memory nodes (excluding entity/relation/Version nodes).

    Used by wildcard search (`*`). Filters to user memory items only —
    enrichment creates entity/relation nodes that should not appear in
    user-facing search results.
    """
    backend = mem._graph.backend
    snapshot = backend.serialize()
    _EXCLUDED_TYPES = {"Version", "entity", "relation", "pattern"}
    items = []
    for raw in snapshot.get("nodes", []):
        mt = raw.get("memory_type", "")
        if mt in _EXCLUDED_TYPES:
            continue
        props = raw.get("properties", {})
        nc = props.get("node_category", "")
        if nc in ("entity", "relation"):
            continue
        # Include user metadata so property filters work on wildcard results
        metadata = {
            k: v
            for k, v in props.items()
            if k
            not in {
                "content",
                "label",
                "memory_type",
                "node_category",
                "entity_type",
                "embedding",
                "category",
                "confidence",
                "stale",
                "reference",
            }
        }
        item = {
            "item_id": raw.get("item_id", ""),
            "content": props.get("content", props.get("label", "")),
            "memory_type": mt or props.get("memory_type", ""),
            "created_at": raw.get("created_at", props.get("created_at", "")),
            # CORE-PROPS-1: Include confidence, stale, reference as top-level keys
            "confidence": props.get("confidence", 1.0),
            "stale": props.get("stale", False),
            "reference": props.get("reference", False),
        }
        if metadata:
            item["metadata"] = metadata
        items.append(item)
    return items


# All explicit caller-settable SmartMemory.search parameters, plus documented kwargs.
# `limit` is deliberately excluded: this wrapper binds/clamps `top_k`, its sole result-count knob.
# The contract test enumerates the live facade signature; unknown keys still drop.
_ALLOWED = frozenset(
    {
        "conversation_context",
        "decompose_query",
        "channel_weights",
        "multi_hop",
        "max_hops",
        "budget_ms",
        "semantic_hops",
        "hop_strategy",
        "cleanup",
        "origin",
        "exclude_origins",
        "include_archived",
        "consolidation_first",
        "expertise",
        "attention_fusion",
        "include_consolidated",
        "include_superseded",
        "include_retracted",
        "as_of_date",
        "as_of_strict",
        "since",
        "until",
        "enable_hybrid",
        "use_ssg",
        "domain",
        "sort_by",
        "reranker",
        "metadata_filter",
        "include_reference",
        "memory_types",
    }
)


def search(
    query: str,
    top_k: int = 5,
    filters: dict[str, str] | None = None,
    include_reference: bool = False,
    memory_type: str | None = None,
    **search_kwargs,
) -> list[dict] | dict[str, list[dict]]:
    """Search memories by semantic similarity with optional property filters.

    Args:
        query: Search query string. Use "*" to return all memories.
        top_k: Maximum results.
        filters: Optional property filters (e.g. {"project": "atlas"}).
                 Applied as post-filter on search results.
        memory_type: Restrict to a single memory type (e.g. "decision").
        **search_kwargs: Recall tuning forwarded to the core engine
                 (decompose_query, channel_weights, multi_hop, max_hops,
                 budget_ms, semantic_hops). Unknown keys are dropped so
                 callers/contract drift never raise TypeError here.
    """
    top_k = max(1, min(top_k, 200))  # clamp to [1, 200]
    mem = get_memory()
    # Forward documented core options. Unknown extension keys still drop, while
    # the enumerating contract test prevents known facade parameters being lost.
    #
    # CORE-RETRACTED-RECALL-1 (2026-08-05): the lifecycle-visibility and
    # transaction-time params were MISSING from this list, so the MCP local
    # backend — whose only path to core is this function — silently dropped
    # every one of them. `include_superseded` and `as_of_date`/`as_of_strict`
    # had been dropped since PLAT-AUDITABLE-MEMORY-1 despite that feature's MCP
    # changelog claiming they were "forwarded through both backends"; a local-
    # mode agent asking for an as-of audit answer got plain present-day search
    # with no error. `include_retracted` would have been the fourth victim.
    #
    # Note the failure mode this allowlist creates: an unknown key is dropped
    # rather than raising, so a param is either wired here or it silently means
    # nothing. Anything added to SmartMemory.search() that callers can set MUST
    # be added here or explicitly documented as excluded beside `_ALLOWED`.
    core_kwargs = {
        k: v for k, v in search_kwargs.items() if k in _ALLOWED and v is not None
    }
    # `include_reference` is bound by this wrapper rather than captured in
    # search_kwargs, so it must be forwarded explicitly on the core search path.
    if include_reference:
        core_kwargs["include_reference"] = include_reference
    if memory_type:
        core_kwargs["memory_type"] = memory_type
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        if filters:
            raise NotImplementedError(
                "Property filters are not supported in remote mode. Use local mode or remove --<property> flags."
            )
        return mem.search(query, top_k, **core_kwargs)
    # Wildcard: return ALL memory nodes (not entity/relation/pattern nodes).
    # top_k is intentionally not applied — "*" means "list everything".
    if query.strip() == "*" and set(core_kwargs) <= {
        "include_reference",
        "include_superseded",
        "include_retracted",
    }:
        from smartmemory.search.visibility_gate import search_visibility_gate_on

        if search_visibility_gate_on():
            all_items = _core_wildcard_rows(mem, **core_kwargs)
        else:
            all_items = _list_all_memories(mem)
        if filters:
            all_items = [
                r
                for r in all_items
                if all(
                    r.get("metadata", {}).get(k) == v or r.get(k) == v
                    for k, v in filters.items()
                )
            ]
        if memory_type:
            all_items = [
                r
                for r in all_items
                if (r.get("memory_type") or r.get("metadata", {}).get("memory_type"))
                == memory_type
            ]
        # CORE-PROPS-1 Phase 6: exclude reference data from wildcard by default
        if not include_reference:
            all_items = [r for r in all_items if not r.get("reference", False)]
        # CORE-RETRACTED-RECALL-1 / CORE-SUPERSEDE-DIALECT-SPLIT-1: this branch
        # never reaches core `search()`, so core's lifecycle filters cannot run —
        # wildcard would otherwise be the one local-mode surface still handing back
        # withdrawn and replaced beliefs by default, which is exactly what those
        # features exist to stop. Applied here to match the default core applies,
        # and honouring the same opt-ins so `memory_search("*",
        # include_retracted=True)` still works.
        _want_superseded = bool(search_kwargs.get("include_superseded"))
        _want_retracted = bool(search_kwargs.get("include_retracted"))
        if not (_want_superseded and _want_retracted):

            def _status(r: dict) -> str | None:
                s = r.get("status") or r.get("metadata", {}).get("status")
                return s if isinstance(s, str) else None

            def _keep(r: dict) -> bool:
                if not _want_superseded:
                    meta = r.get("metadata", {}) or {}
                    if (
                        r.get("superseded")
                        or meta.get("superseded")
                        or _status(r) == "superseded"
                    ):
                        return False
                if not _want_retracted and _status(r) == "retracted":
                    return False
                return True

            all_items = [r for r in all_items if _keep(r)]
        return all_items
    from smartmemory.search.results import carry_partial
    from smartmemory.search.visibility_gate import gated_partial_coverage

    if not filters:
        results = mem.search(query, top_k=top_k, **core_kwargs)
        partial = gated_partial_coverage(results, where="local search")
        if isinstance(results, dict):
            return {
                key: [_result_dict(r) for r in items] for key, items in results.items()
            }
        # U5.21 (C8): the call's completeness rides on the returned list (still a plain list to every caller), so a
        # partial answer with no visible row is never read downstream as "nothing found".
        return carry_partial([_result_dict(r) for r in results], partial)
    # With filters: fetch a wider window, then post-filter. If still short,
    # widen progressively to avoid missing sparse matches.
    cap = _gated_fetch_cap()
    asked = 0
    partial: tuple[str, ...] = ()
    settled = False
    for multiplier in (5, 20, 50):
        fetch_k = top_k * multiplier
        if cap is not None:
            # U5.21 (C4): gate ON, no fetch is wider than the bound core's read gate can examine.
            fetch_k = min(fetch_k, cap)
            if fetch_k <= asked:
                break  # the last round already asked for the bound: a wider window cannot be asked for
            asked = fetch_k
        results = mem.search(query, top_k=fetch_k, **core_kwargs)
        partial = gated_partial_coverage(results, where="local filtered search")
        items = [_result_dict(r) for r in results]
        matched = [
            r
            for r in items
            if all(
                r.get("metadata", {}).get(k) == v or r.get(k) == v
                for k, v in filters.items()
            )
        ]
        if len(matched) >= top_k or (len(results) < fetch_k and not partial):
            settled = True
            break  # enough matches found, or exhausted all results (core's partial answer is not exhaustion)
    if cap is not None and not settled:
        # C3: the widest window this call may ask for is spent and the answer is not proved complete. Said, never
        # presented as an exhausted search.
        log.warning(
            "local filtered search stopped at a fetch of %d: %d of %d wanted matched and candidates past that window "
            "were not examined",
            asked,
            len(matched),
            top_k,
        )
        partial = (*partial, _FETCH_BOUND_REASON)
    return carry_partial(matched[:top_k], partial)


def recall(
    cwd: str | None = None,
    top_k: int = 10,
    *,
    query: str | None = None,
    include_snapshot: bool = True,
    workspace_id: str | None = None,
    strict: bool | None = None,
    exclude_ids: list[str] | None = None,
) -> str:
    """HOOK-RECALL-RELEVANCE-1: workspace-scoped, ranked, deduped recall.

    Bug fixes vs legacy:
      - empty-content items skipped
      - dedup by item_id (then content) — collapses `[concept] smartmemory` × 3
      - origin tier filter (default {1, 2}) — excludes tier-4 system noise
      - workspace metadata filter — Lite SQLiteBackend can't scope at storage layer
      - optional CORE-SUMMARY-1 snapshot frame (≤7d) prepended for SessionStart
      - optional `query` for UserPromptSubmit semantic search
      - JSONL trace at ~/.smartmemory/hook-recall.jsonl

    Remote mode delegates to RemoteMemory.recall (same param contract).
    """
    from smartmemory.origin_policy import filter_by_tiers, get_default_tiers
    from smartmemory_app.recall_format import (
        _item_to_recall_dict,
        filter_hook_items,
        matching_lessons,
        _trace,
        derive_workspace_id,
        format_recall_lines,
        payload_ids,
        record_hook_degradation,
        recall_item_label,
        time_ms,
    )

    from smartmemory.search.results import carry_partial_text
    from smartmemory.search.visibility_gate import gated_partial_coverage

    t0 = time_ms()
    mem = get_memory()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.recall(
            cwd,
            top_k,
            query=query,
            include_snapshot=include_snapshot,
            workspace_id=workspace_id,
            strict=strict,
            **({"exclude_ids": exclude_ids} if exclude_ids else {}),
        )

    workspace_id = workspace_id or derive_workspace_id(cwd)
    if strict is None:
        strict = os.environ.get("SMARTMEMORY_RECALL_STRICT", "").lower() in (
            "1",
            "true",
            "yes",
        )

    # 1. Optional snapshot frame (graph-mirrored markdown from CORE-SUMMARY-1)
    excluded = set()
    frame = ""
    if include_snapshot:
        try:
            snaps = mem.search("", memory_type="snapshot", sort_by="recency", top_k=1)
            # The frame is one newest row: a partial answer only means the newest one may be unexamined, and the
            # WARNING from the reader names it. The frame is optional context, never the recall answer.
            gated_partial_coverage(snaps, where="orient snapshot lookup")
        except Exception as exc:  # noqa: BLE001
            # U5.21 (C1): record_hook_degradation reads core's logged mark off ``exc`` (gate ON only), so a refusal the
            # raising site already logged is not logged twice. Any other failure logs as before.
            record_hook_degradation("Orient lost snapshot context", exc)
            snaps = []
        snaps = filter_hook_items(snaps, excluded)
        if snaps:
            snap = snaps[0]
            # Defensive: only honor results that are actually snapshots.
            # `mem.search(memory_type="snapshot")` should filter, but a mocked
            # backend may not — and we don't want to render a non-snapshot as a frame.
            if getattr(snap, "memory_type", None) == "snapshot":
                snap_meta = getattr(snap, "metadata", None) or {}
                ws_match = workspace_id is None or snap_meta.get("workspace_id") in (
                    None,
                    workspace_id,
                )
                if ws_match and _snapshot_is_fresh(snap, max_days=7):
                    frame = format_recall_lines(
                        [_item_to_recall_dict(snap)], top_k=1
                    ).removeprefix("## SmartMemory Context\n")

    # Filter each channel before allocating its preferred slots. Widen here, at
    # the producing layer, when filtering/dedup leaves eligible slots unfilled.
    tiers = get_default_tiers("recall")
    recall_floor = float(os.environ.get("SMARTMEMORY_RECALL_FLOOR", "0.3"))

    def eligible(rows):
        rows = [
            r
            for r in filter_hook_items(rows, excluded)
            if _item_to_recall_dict(r)["item_id"] not in (exclude_ids or ())
        ]
        tier_rows = filter_by_tiers(rows, tiers)
        allowed = {id(r) for r in tier_rows}
        kept = []
        for r in rows:
            meta = _item_to_recall_dict(r)["metadata"]
            r_ws = meta.get("workspace_id") if isinstance(meta, dict) else None
            reason = None
            if getattr(r, "memory_type", "") == "snapshot":
                reason = "snapshot row covered by optional frame"
            elif id(r) not in allowed:
                reason = f"origin tier outside allowed tiers {tiers}"
            elif workspace_id and r_ws != workspace_id and (r_ws is not None or strict):
                reason = "workspace mismatch or missing strict workspace"
            elif getattr(r, "confidence", 1.0) < recall_floor:
                reason = f"confidence below floor {recall_floor}"
            elif getattr(r, "reference", False):
                reason = "reference exclusion"
            if reason:
                log.warning(
                    "recall dropped %s: %s",
                    recall_item_label(_item_to_recall_dict(r)),
                    reason,
                )
            else:
                kept.append(r)
        return kept

    try:
        lessons = eligible(
            matching_lessons(
                mem._graph.search_nodes({"memory_type": "decision"}), query
            )
        )
    except Exception as exc:
        record_hook_degradation("Recall lost decision lookup", exc)
        lessons = []

    requested = max(0, top_k)
    fetch_k = max(1, requested * (2 if query else 1))
    # U5.21 (C4): gate ON, every fetch (the first included) is clamped to what core's read gate can examine, and rows a
    # callee returns beyond the fetch are not used. OFF keeps the legacy unbounded doubling.
    cap = _gated_fetch_cap()
    if cap is not None:
        fetch_k = min(fetch_k, cap)

    def examined(found, bound: int, where: str) -> list:
        rows = list(found)
        if cap is not None and len(rows) > bound:
            log.warning(
                "%s returned %d rows for a fetch of %d: the extra rows are not used, so the fetch bound holds",
                where,
                len(rows),
                bound,
            )
            rows = rows[:bound]
        return rows

    recent_k, semantic_k = _split_recall_counts(requested)
    results = []
    partial_names: tuple[str, ...] = ()
    body = ""
    emitted = 0
    stopped = ""
    while requested:
        # U5.21 (C3): a short page is exhaustion only when core did not report that answer as partial.
        partial = False
        if query:
            found = mem.search(query, top_k=fetch_k)
            partial_names = gated_partial_coverage(found, where="recall search")
            partial = bool(partial_names)
            raw = examined(found, fetch_k, "recall search")
            results = eligible(raw)
            exhausted = len(raw) < fetch_k and not partial
        elif cwd:
            found_recent = mem.search("", top_k=fetch_k, sort_by="recency")
            found_semantic = mem.search(cwd, top_k=fetch_k)
            partial_recent = gated_partial_coverage(
                found_recent, where="recall recency search"
            )
            partial_semantic = gated_partial_coverage(
                found_semantic, where="recall workspace search"
            )
            partial_names = tuple(dict.fromkeys((*partial_recent, *partial_semantic)))
            partial = bool(partial_names)
            recent = examined(found_recent, fetch_k, "recall recency search")
            semantic = examined(found_semantic, fetch_k, "recall workspace search")
            recent_rows, semantic_rows = eligible(recent), eligible(semantic)
            # Preserve ceil(k/2) recency first, then floor(k/2) semantic.
            # Overflow from either channel can replace missing/duplicate slots.
            results = (
                recent_rows[:recent_k]
                + semantic_rows[:semantic_k]
                + recent_rows[recent_k:]
                + semantic_rows[semantic_k:]
            )
            exhausted = (len(recent) < fetch_k and not partial_recent) and (
                len(semantic) < fetch_k and not partial_semantic
            )
        else:
            found = mem.search("", top_k=fetch_k, sort_by="recency")
            partial_names = gated_partial_coverage(found, where="orient recency search")
            partial = bool(partial_names)
            raw = examined(found, fetch_k, "orient recency search")
            results = eligible(raw)
            exhausted = len(raw) < fetch_k and not partial

        body = format_recall_lines(
            [_item_to_recall_dict(r) for r in lessons + results],
            top_k=requested,
            lessons_first=not query,
        )
        emitted = body.count("\n- ") if body else 0
        if emitted >= requested or exhausted:
            break
        if cap is not None and fetch_k >= cap:
            # The bound is spent and the answer is not proved complete: a partial answer (core's gate examined only
            # part of what it could) or a full page at the bound. Either way deeper candidates were not examined.
            stopped = (
                "core reported a partial answer"
                if partial
                else "the page came back full"
            )
            break
        fetch_k = fetch_k * 2 if cap is None else min(fetch_k * 2, cap)

    if emitted < requested:
        if stopped:
            log.warning(
                "recall shortfall: requested=%s emitted=%d; stopped widening at a fetch of %d (%s), so eligible "
                "candidates past what core examined are not shown",
                _count_text(requested),
                emitted,
                fetch_k,
                stopped,
            )
        else:
            log.warning(
                "recall shortfall: requested=%s emitted=%d; eligible distinct candidates exhausted",
                _count_text(requested),
                emitted,
            )

    # 7. Compose: body + optional snapshot frame
    if body and frame:
        out = f"{body}\n\n{frame}"
    elif body:
        out = body
    elif frame:
        out = f"## SmartMemory Context\n\n{frame}"
    else:
        out = ""

    # 8. Trace (never raises)
    _trace(
        phase="recall" if query else "orient",
        payload=out,
        excluded_non_memory=len(excluded),
        ranked_ids=[
            recall_item_label(_item_to_recall_dict(r)) for r in lessons + results
        ]
        + payload_ids(frame),
        workspace_id=workspace_id,
        cwd=cwd,
        query=query,
        candidate_count=len(results),
        emitted=emitted,
        snapshot_used=bool(frame),
        latency_ms=time_ms() - t0,
    )

    # U5.21 (C8): an empty or short recall from a partial answer is not "no memories". The text it returns is the plain
    # str it always was (OFF: the same object) and its ``.partial`` names why core called the answer partial.
    return carry_partial_text(out, partial_names)


def _count_text(n: int) -> str:
    """A caller-supplied count for a log line: the digits when it is a machine-sized int, else its bit length.

    ``%d`` of a 5000-digit int raises ``ValueError`` under Python's int-to-str limit, which would turn a WARNING into a
    crash. Same text as ``%d`` for every ordinary count.
    """
    return str(n) if n.bit_length() <= 64 else f"<an integer of {n.bit_length()} bits>"


def _snapshot_is_fresh(snap, max_days: int = 7) -> bool:
    """True if the snapshot's created_at is within max_days. Defaults open on parse failure."""
    from datetime import datetime, timezone

    meta = getattr(snap, "metadata", None) or {}
    created_str = meta.get("created_at") if isinstance(meta, dict) else None
    if not created_str:
        return True  # no timestamp recorded → don't suppress
    try:
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).days
        return age <= max_days
    except (ValueError, AttributeError):
        return True


def get(item_id: str) -> dict:
    """Get a single memory by item_id. Returns dict in both modes.

    Local returns MemoryItem (needs .to_dict()); remote returns dict | None directly.
    """
    mem = get_memory()
    from smartmemory_app.remote_backend import RemoteMemory

    if isinstance(mem, RemoteMemory):
        return mem.get(item_id) or {}  # already a dict
    item = mem.get(item_id)
    return _result_dict(item) if item else {}
