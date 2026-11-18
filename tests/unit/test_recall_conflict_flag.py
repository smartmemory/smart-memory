"""Real local MCP -> wrapper -> SQLite core conflict reads."""

import pytest
from smartmemory.models.memory_item import MemoryItem
from smartmemory.errors import RecallConflictError
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_app import storage
from smartmemory_mcp.backends.local import LocalBackend


def test_local_mcp_uses_real_store_for_warning_and_strict_refusal(
    tmp_path, monkeypatch
):
    memory = create_lite_memory(
        str(tmp_path / "test_rcf_mcp"),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    try:
        # Bind the actual Lite instance at the existing application singleton seam.
        monkeypatch.setattr(storage, "get_memory", lambda: memory)
        for n in [3, 5]:
            memory.add(
                MemoryItem(
                    item_id=f"test_rcf_{n}",
                    content=f"Acme permits {n} retries.",
                    memory_type="semantic",
                    origin="cli:add",
                )
            )
        backend = LocalBackend()
        hits = backend.search("Acme", top_k=10, enable_hybrid=False, reranker="none")
        assert {i["item_id"] for i in hits} == {"test_rcf_3", "test_rcf_5"}
        assert all(i["conflict"] for i in hits)
        with pytest.raises(RecallConflictError) as error:
            backend.search(
                "Acme",
                top_k=10,
                enable_hybrid=False,
                reranker="none",
                fail_if_conflicted=True,
            )
        assert error.value.pairs == [("test_rcf_3", "test_rcf_5")]
    finally:
        memory.close()
