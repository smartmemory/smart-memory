"""CORE-CODE-PROVENANCE-1 Phase 2a — S05 live capture re-route (wrapper, real lite backend).

Asserts the augmented `observe`: a live Edit/Write persists full-payload
code_provenance evidence anchored to the session, the episodic reflection still
exists AND now carries the correct `origin="hook:observe"` (the reserved-key bug
fix), provenance failure is isolated from the episodic write, and concurrent
detached-hook processes serialize on the cross-process write lock (one :Session
node).
"""
from __future__ import annotations

import multiprocessing as mp
import os

import pytest

from smartmemory.provenance.evidence import session_item_id


@pytest.fixture(scope="module")
def lite_mem(tmp_path_factory):
    """A real lite SmartMemory bound to a module-scoped data dir (skip if unavailable)."""
    import smartmemory_app.storage as st

    d = tmp_path_factory.mktemp("provdata")
    os.environ["SMARTMEMORY_DATA_DIR"] = str(d)
    os.environ["SMARTMEMORY_NO_WARM"] = "1"
    st._memory = None
    st._data_path = None
    try:
        mem = st.get_memory(data_dir=str(d))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"lite memory unavailable: {exc}")
    if not hasattr(mem, "ingest_structured"):
        pytest.skip("active backend lacks ingest_structured")
    return mem


def _lifecycle(session_id):
    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    return MemoryLifecycle(session_id, LifecycleConfig())


def _prov_rows(mem, session_id):
    rows = mem._graph.search_nodes({"memory_type": "code_provenance"})
    return [r for r in rows if r.metadata.get("session_id") == session_id]


class TestObserveReRoute:
    def test_observe_persists_full_payload_evidence(self, lite_mem):
        sid = "s_full"
        big = "def handler():\n    " + "x = 1\n    " * 40 + "return x\n"  # > 200 chars
        _lifecycle(sid).observe(
            tool_name="Write",
            tool_input={"file_path": "svc/handler.py", "content": big},
            tool_result="ok",
            transcript_path="/proj/.claude/s_full.jsonl",
            cwd="/proj",
        )
        rows = _prov_rows(lite_mem, sid)
        assert len(rows) == 1
        node = rows[0]
        assert node.content == big  # FULL untruncated block (not [:200])
        assert node.metadata["file_path"] == "svc/handler.py"
        assert node.metadata["source_path"] == "/proj/.claude/s_full.jsonl"
        assert node.metadata["op"] == "write"
        assert node.metadata.get("tool_ref") is None and node.metadata.get("line_no") is None
        # anchored to the :Session node
        assert lite_mem.get(session_item_id("cc", sid)) is not None

    def test_episodic_reflection_kept_with_correct_origin(self, lite_mem):
        sid = "s_origin"
        _lifecycle(sid).observe(
            tool_name="Edit",
            tool_input={"file_path": "a.py", "old_string": "x", "new_string": "y = 2\n"},
            tool_result="ok",
            transcript_path="/proj/.claude/s_origin.jsonl",
        )
        episodics = lite_mem._graph.search_nodes({"memory_type": "episodic"})
        observe_items = [e for e in episodics if getattr(e, "origin", None) == "hook:observe"]
        # the bug fix: origin is hook:observe (tier 3), NOT "unknown"
        assert observe_items, "episodic hook:observe reflection missing (origin bug?)"
        assert all(getattr(e, "origin", None) != "unknown" for e in observe_items)

    def test_provenance_failure_isolated_from_episodic(self, lite_mem, monkeypatch):
        sid = "s_iso"

        def _boom(_edits):
            raise RuntimeError("forced provenance failure")

        monkeypatch.setattr("smartmemory_app.storage.persist_provenance", _boom)
        lc = _lifecycle(sid)
        before = lc._observation_count
        # must not raise — provenance error is caught, episodic write survives
        lc.observe(
            tool_name="Write",
            tool_input={"file_path": "b.py", "content": "z = 3\n"},
            tool_result="ok",
            transcript_path="/proj/.claude/s_iso.jsonl",
        )
        assert lc._observation_count == before + 1  # episodic write + state save happened
        # and no provenance row was persisted for this session
        assert _prov_rows(lite_mem, sid) == []


# ── cross-process concurrency (the cross-process write lock) ──────────────────
def _writer(data_dir: str, session_id: str, file_idx: int):
    os.environ["SMARTMEMORY_DATA_DIR"] = data_dir
    os.environ["SMARTMEMORY_NO_WARM"] = "1"
    import smartmemory_app.storage as st

    st._memory = None
    st._data_path = None
    from smartmemory.provenance.evidence import AuthorshipEvidence, SessionEdits

    ev = AuthorshipEvidence.make(
        source="cc", source_path="/t/s.jsonl", session_id=session_id,
        file_path=f"f{file_idx}.py", op="write", block_text=f"x{file_idx} = {file_idx}\n",
    )
    se = SessionEdits(source="cc", source_path="/t/s.jsonl", session_id=session_id,
                      cwd=None, repo=None, edits=[ev])
    st.persist_provenance(se)


def test_concurrent_detached_hooks_one_session_node(tmp_path):
    """Two processes persisting the same session concurrently → exactly one
    :Session node and both evidence rows (the cross-process FileLock serializes)."""
    conc_dir = tmp_path / "concdata"
    conc_dir.mkdir()
    sid = "s_conc"

    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(conc_dir), sid, i)) for i in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0, f"writer exited {p.exitcode}"

    # Read the store independently (fresh core memory, no wrapper singleton).
    from smartmemory.tools.factory import create_lite_memory

    reader = create_lite_memory(data_dir=str(conc_dir))
    try:
        sessions = reader._graph.search_nodes({"memory_type": "session"})
        mine = [s for s in sessions if s.item_id == session_item_id("cc", sid)]
        assert len(mine) == 1, "concurrent hooks must not duplicate the :Session node"
        prov = [r for r in reader._graph.search_nodes({"memory_type": "code_provenance"})
                if r.metadata.get("session_id") == sid]
        assert len(prov) == 2  # both writers' evidence rows
    finally:
        reader.close()
