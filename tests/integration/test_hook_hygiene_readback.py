"""Real local capture/readback with core 1.4.121, SQLite, and offline Tier 1 extraction."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from smartmemory_app import storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.recall_format import derive_workspace_id


@pytest.fixture
def local_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    mem = create_lite_memory(
        data_dir=str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
    )
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    monkeypatch.setattr(storage, "_data_path", tmp_path / "data")
    try:
        yield mem
    finally:
        mem.close()


@pytest.mark.parametrize(
    "phase,origin,mtype",
    [
        ("observe", "hook:observe", "episodic"),
        ("learn", "hook:learn", "episodic"),
        ("distill", "lifecycle:distill", "pending"),
        ("persist", "hook:persist", "episodic"),
    ],
)
def test_capture_origin_and_workspace_readback(
    local_memory, tmp_path: Path, phase: str, origin: str, mtype: str
) -> None:
    cwd = str(tmp_path / "project")
    lc = MemoryLifecycle("capture")
    lc._last_assistant_message = "Keep the full settlement reference."
    args = {
        "observe": ("Bash", {}, "done"),
        "learn": ("Bash", "failure"),
        "distill": ("Keep the full settlement reference.",),
        "persist": (),
    }
    getattr(lc, phase)(*args[phase], cwd=cwd)
    rows = local_memory._graph.search_nodes({"memory_type": mtype})
    assert len(rows) == 1
    stored = local_memory.get(rows[0].item_id)
    assert stored.origin == origin
    assert stored.metadata["workspace_id"] == derive_workspace_id(cwd)


@pytest.mark.parametrize("pinned", [False, True])
def test_distill_workspace_readback_across_git_worktrees(
    local_memory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned: bool,
) -> None:
    a, b = tmp_path / "repo", tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(a)], check=True, capture_output=True)
    # Orphan worktree: no commit and no mutation of the working repository.
    subprocess.run(
        ["git", "-C", str(a), "worktree", "add", "--orphan", "-b", "other", str(b)],
        check=True,
        capture_output=True,
    )
    assert derive_workspace_id(str(a)) != derive_workspace_id(str(b))
    if pinned:
        monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "logical-project")
    text = "Retain the complete settlement reference."
    MemoryLifecycle("distill").distill(text, cwd=str(a))
    rows = local_memory._graph.search_nodes({"memory_type": "pending"})
    assert len(rows) == 1 and rows[0].origin == "lifecycle:distill"
    assert rows[0].metadata["workspace_id"] == derive_workspace_id(str(a))
    at_a = storage.recall(cwd=str(a), include_snapshot=False)
    at_b = storage.recall(cwd=str(b), include_snapshot=False)
    assert text in at_a and f"[mem:{rows[0].item_id}]" in at_a
    assert (text in at_b) is pinned
