"""Tests for smartmemory_app.storage singleton and operations."""

import pytest
from unittest.mock import MagicMock, patch


def _reset_singleton():
    """Reset the storage module singletons between tests."""
    import smartmemory_app.storage as storage

    storage._memory = None
    storage._remote_memory = None


@pytest.fixture(autouse=True)
def reset_storage():
    _reset_singleton()
    yield
    _reset_singleton()


def test_get_memory_singleton(tmp_path):
    """get_memory() returns the same instance on repeated calls."""
    import smartmemory_app.storage as storage

    mock_mem = MagicMock()
    mock_pm = MagicMock()
    mock_pm.get_patterns.return_value = {}
    mock_pm.add_patterns.return_value = 0
    mock_store = MagicMock()

    with (
        patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path),
        patch("smartmemory_app.patterns.JSONLPatternStore", return_value=mock_store),
        patch(
            "smartmemory.ontology.pattern_manager.PatternManager", return_value=mock_pm
        ),
        patch("smartmemory.tools.factory.create_lite_memory", return_value=mock_mem),
    ):
        m1 = storage.get_memory()
        m2 = storage.get_memory()
    assert m1 is m2, "get_memory() must return the same singleton"


def test_get_memory_registers_atexit(tmp_path):
    """get_memory() registers _shutdown with atexit on first init."""
    import smartmemory_app.storage as storage

    mock_mem = MagicMock()
    mock_pm = MagicMock()
    mock_pm.get_patterns.return_value = {}
    mock_pm.add_patterns.return_value = 0
    mock_store = MagicMock()

    with (
        patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path),
        patch("smartmemory_app.patterns.JSONLPatternStore", return_value=mock_store),
        patch(
            "smartmemory.ontology.pattern_manager.PatternManager", return_value=mock_pm
        ),
        patch("smartmemory.tools.factory.create_lite_memory", return_value=mock_mem),
        patch("atexit.register") as mock_register,
    ):
        storage.get_memory()
        mock_register.assert_called_once_with(storage._shutdown)


def test_local_memory_disables_entity_grounding_by_default(tmp_path):
    """The local CLI profile preserves default ingest quality without network grounding."""
    from smartmemory_app import storage
    from smartmemory_app.config import SmartMemoryConfig

    config = SmartMemoryConfig(mode="local", coreference=True)
    mock_mem = MagicMock()
    mock_pm = MagicMock()
    mock_store = MagicMock()

    with (
        patch("smartmemory_app.storage.load_config", return_value=config),
        patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path),
        patch("smartmemory_app.patterns.JSONLPatternStore", return_value=mock_store),
        patch(
            "smartmemory.ontology.pattern_manager.PatternManager", return_value=mock_pm
        ),
        patch(
            "smartmemory.tools.factory.create_lite_memory", return_value=mock_mem
        ) as mock_create,
        patch("atexit.register"),
    ):
        storage._get_local_memory()

    profile = mock_create.call_args.kwargs["pipeline_profile"]
    assert profile.profile_name == "default"
    assert profile.coreference.enabled is True
    assert profile.extraction.llm_extract.enabled is True
    assert profile.enrich.enricher_names is None
    assert profile.enrich.wikidata.enabled is False
    assert profile.enrich.wikidata.sparql_enabled is False
    assert profile.evolve.run_evolution is False
    assert profile.evolve.run_clustering is False


def test_shutdown_calls_save_and_close():
    """_shutdown() calls save_all() and SmartMemory.close() on the memory instance.

    _shutdown() prefers CollectionAwareVectorBackend.save_all() (CORE-VEC-DELETE-1) —
    lite mode injects that registry, not a bare UsearchVectorBackend, and the registry
    has no _save() of its own. It prefers SmartMemory.close() (which orchestrates
    evolution worker, ontology store, and graph backend shutdown); only falls back to
    _graph.backend.close() when close() is unavailable.
    """
    import smartmemory_app.storage as storage

    mock_vector = MagicMock()
    mock_mem = MagicMock()
    mock_mem._vector_backend = mock_vector
    storage._memory = mock_mem

    storage._shutdown()

    mock_vector.save_all.assert_called_once()
    mock_vector._save.assert_not_called()
    mock_mem.close.assert_called_once()


def test_shutdown_falls_back_to_save_when_no_save_all():
    """_shutdown() calls _save() directly for the legacy single-backend injection
    path, i.e. a vector backend that has no save_all() (only a bare UsearchVectorBackend,
    not the CollectionAwareVectorBackend registry)."""
    import smartmemory_app.storage as storage

    mock_vector = MagicMock(spec=["_save"])
    mock_mem = MagicMock()
    mock_mem._vector_backend = mock_vector
    storage._memory = mock_mem

    storage._shutdown()

    mock_vector._save.assert_called_once()


def test_shutdown_clears_singleton():
    """_shutdown() sets _memory to None after running."""
    import smartmemory_app.storage as storage

    storage._memory = MagicMock()
    storage._shutdown()
    assert storage._memory is None


def test_shutdown_noop_if_memory_none():
    """_shutdown() does nothing and does not raise if _memory is None."""
    import smartmemory_app.storage as storage

    storage._memory = None
    storage._shutdown()  # must not raise


def test_normalize_ingest_result_str():
    """_normalize_ingest_result returns str directly."""
    from smartmemory_app.storage import _normalize_ingest_result

    assert _normalize_ingest_result("abc-123") == "abc-123"


def test_normalize_ingest_result_dict():
    """_normalize_ingest_result extracts item_id from dict."""
    from smartmemory_app.storage import _normalize_ingest_result

    assert (
        _normalize_ingest_result({"item_id": "abc-123", "queued": False}) == "abc-123"
    )


def test_get_memory_raises_unconfigured_error():
    """get_memory() raises UnconfiguredError when not configured and migration fails."""
    import smartmemory_app.storage as storage
    from smartmemory_app.config import UnconfiguredError

    with (
        patch("smartmemory_app.storage.is_configured", return_value=False),
        patch("smartmemory_app.storage._detect_and_migrate", return_value=False),
    ):
        with pytest.raises(UnconfiguredError, match="smartmemory setup"):
            storage.get_memory()


def test_ingest_remote_branch_skips_lock(tmp_path):
    """ingest() in remote mode delegates to RemoteMemory without acquiring a lock."""
    import smartmemory_app.storage as storage
    from smartmemory_app.remote_backend import RemoteMemory

    mock_mem = MagicMock(spec=RemoteMemory)
    mock_mem.ingest.return_value = "remote-item-id"

    with (
        patch("smartmemory_app.storage.get_memory", return_value=mock_mem),
        patch("smartmemory_app.storage._get_lock_file") as mock_lock,
    ):
        result = storage.ingest("remote content")

    mock_mem.ingest.assert_called_once_with("remote content", "episodic")
    mock_lock.assert_not_called()
    assert result == "remote-item-id"


def test_get_remote_memory_singleton(tmp_path):
    """_get_remote_memory() returns the same RemoteMemory instance on repeated calls."""
    import smartmemory_app.storage as storage
    from smartmemory_app.config import SmartMemoryConfig

    cfg = SmartMemoryConfig(
        mode="remote", api_url="https://api.example.com", team_id="t1"
    )
    # RemoteMemory is lazily imported inside _get_remote_memory — patch at source module
    with patch("smartmemory_app.remote_backend.RemoteMemory") as MockRemote:
        m1 = storage._get_remote_memory(cfg)
        m2 = storage._get_remote_memory(cfg)
    assert m1 is m2
    MockRemote.assert_called_once()  # constructor called only once


def test_search_accepts_mcp_recall_kwargs():
    """search() must not raise TypeError when called with the exact MCP memory_search shape.

    Before the fix, storage.search() only accepted (query, top_k, filters,
    include_reference).  The MCP server calls it with memory_type, enable_hybrid,
    decompose_query, multi_hop, max_hops, budget_ms — any of those caused a
    TypeError and broke memory_search in local mode entirely.

    Asserts:
    - No TypeError is raised
    - The underlying mem.search() receives memory_type in its kwargs (forwarded)
    - The underlying mem.search() receives enable_hybrid (documented core kwarg)
    """
    import smartmemory_app.storage as storage

    received_kwargs = {}

    class FakeResult:
        def to_dict(self):
            return {"item_id": "x", "content": "hello", "memory_type": "decision"}

    class FakeMem:
        def search(self, query, **kwargs):
            received_kwargs.update(kwargs)
            return [FakeResult()]

    with patch("smartmemory_app.storage.get_memory", return_value=FakeMem()):
        results = storage.search(
            "q",
            top_k=3,
            memory_type="decision",
            enable_hybrid=True,  # documented core search option
            decompose_query=False,
            multi_hop=False,
            max_hops=3,
            budget_ms=1500,
        )

    # Call must not raise — if we reach here, no TypeError occurred.
    assert isinstance(results, list)

    # memory_type forwarded into core_kwargs
    assert received_kwargs.get("memory_type") == "decision"

    # SEARCH-HOP-STRATEGY-SURFACE-1 closes known option drops.
    assert received_kwargs["enable_hybrid"] is True


def test_search_forwards_lifecycle_and_asof_params():
    """CORE-RETRACTED-RECALL-1: the visibility / time-travel params must reach core.

    This function is the MCP LOCAL backend's only path to core, so a param missing
    from the allowlist does not raise — it silently means nothing. All four were
    missing: `include_superseded` and `as_of_date`/`as_of_strict` since
    PLAT-AUDITABLE-MEMORY-1 (whose MCP changelog claimed they were forwarded), and
    `include_retracted` would have been the fourth.

    The user-visible failure: a local-mode agent asks for an as-of audit answer and
    receives plain present-day search results, with nothing indicating the request
    was ignored.
    """
    import smartmemory_app.storage as storage

    received_kwargs = {}

    class FakeResult:
        def to_dict(self):
            return {"item_id": "x", "content": "hello", "memory_type": "decision"}

    class FakeMem:
        def search(self, query, **kwargs):
            received_kwargs.update(kwargs)
            return [FakeResult()]

    with patch("smartmemory_app.storage.get_memory", return_value=FakeMem()):
        storage.search(
            "q",
            top_k=3,
            include_superseded=True,
            include_retracted=True,
            as_of_date="2026-01-01T00:00:00Z",
            as_of_strict=True,
        )

    assert received_kwargs.get("include_retracted") is True
    assert received_kwargs.get("include_superseded") is True
    assert received_kwargs.get("as_of_date") == "2026-01-01T00:00:00Z"
    assert received_kwargs.get("as_of_strict") is True


def test_wildcard_search_hides_superseded_and_retracted_by_default():
    """CORE-RETRACTED-RECALL-1: the "*" branch never reaches core, so it must filter itself.

    `search("*")` short-circuits to _list_all_memories() and returns without calling
    mem.search(), so core's lifecycle filters cannot run. Without an equivalent filter
    here, wildcard was the one local-mode surface still handing back withdrawn and
    replaced beliefs by default.
    """
    import smartmemory_app.storage as storage

    rows = [
        {"item_id": "live", "content": "a", "metadata": {"status": "active"}},
        {"item_id": "gone", "content": "b", "metadata": {"status": "retracted"}},
        {"item_id": "old", "content": "c", "metadata": {"status": "superseded"}},
        {"item_id": "flagged", "content": "d", "metadata": {"superseded": True}},
    ]

    with (
        patch("smartmemory_app.storage.get_memory", return_value=MagicMock()),
        patch("smartmemory_app.storage._list_all_memories", return_value=list(rows)),
    ):
        default = storage.search("*")
        with_retracted = storage.search("*", include_retracted=True)
        with_both = storage.search("*", include_retracted=True, include_superseded=True)

    assert [r["item_id"] for r in default] == ["live"]
    assert sorted(r["item_id"] for r in with_retracted) == ["gone", "live"]
    assert sorted(r["item_id"] for r in with_both) == ["flagged", "gone", "live", "old"]


def test_ingest_acquires_lock(tmp_path):
    """ingest() acquires FileLock before calling mem.ingest() in local mode."""
    import smartmemory_app.storage as storage

    mock_mem = MagicMock()
    mock_mem.ingest.return_value = "item-123"

    mock_lock_instance = MagicMock()
    mock_lock_instance.__enter__ = MagicMock(return_value=None)
    mock_lock_instance.__exit__ = MagicMock(return_value=False)

    with (
        # get_memory() now checks is_configured() — patch it to return mock directly
        patch("smartmemory_app.storage.get_memory", return_value=mock_mem),
        patch("smartmemory_app.storage._resolve_data_dir", return_value=tmp_path),
        # _get_lock_file() does the lazy filelock import — patch at that boundary
        patch(
            "smartmemory_app.storage._get_lock_file", return_value=mock_lock_instance
        ),
    ):
        result = storage.ingest("test content")

    mock_lock_instance.__enter__.assert_called_once()
    mock_mem.ingest.assert_called_once_with(
        "test content", context={"memory_type": "episodic"}, sync=True
    )
    assert result == "item-123"
