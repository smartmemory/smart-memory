"""HOOK-INGEST-READ-BUGS-1: hook notes keep their type, origin and full entity pipeline, and skip the auto-challenge.

Real Lite store (temp dir), real ``lifecycle`` and ``storage.ingest`` writers. One table: what each writer stores
(``memory_type``, origin), whether the pipeline planned work (the work-graph boxes: ``pending`` items bypass the
pipeline, so no boxes), and whether core's contradiction search (the auto-challenge) ran. Owner 2026-10-26: hook
origins keep the full entity pipeline and stored type; only the challenge is skipped for them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FACT_TEXT = "Alice Johnson is the CTO of Acme Corporation and deployed Redis on Kubernetes in Berlin."


@pytest.fixture
def lite_env(tmp_path, monkeypatch):
    import smartmemory_app.storage as storage

    data = tmp_path / "data"
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    for prefix in ("GRAPH_DB", "FALKORDB", "REDIS", "CACHE_REDIS", "MONGODB"):
        monkeypatch.setenv(f"{prefix}_HOST", "127.0.0.1")
        monkeypatch.setenv(f"{prefix}_PORT", "1")
    for name in ("SMARTMEMORY_COSTLESS_ORIGINS", "SMARTMEMORY_WORKSPACE_ID", "SMARTMEMORY_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("smartmemory.pipeline.work_graph.spawn.spawn_worker_if_idle", lambda _data_dir: False)
    storage._memory = None
    storage._data_path = None
    yield data
    storage._shutdown()
    storage._memory = None
    storage._data_path = None


@pytest.fixture
def challenger_calls(monkeypatch):
    """Spy on the challenger seam only: records ``should_challenge`` and ``AssertionChallenger.challenge`` calls."""
    from smartmemory.reasoning import challenger

    calls: list[tuple[str, str | None]] = []
    real_should, real_challenge = challenger.should_challenge, challenger.AssertionChallenger.challenge

    def should(content, memory_type=None, source=None, metadata=None):
        calls.append(("should_challenge", memory_type))
        return real_should(content=content, memory_type=memory_type, source=source, metadata=metadata)

    def challenge(self, content, *args, **kwargs):
        calls.append(("challenge", kwargs.get("memory_type")))
        return real_challenge(self, content, *args, **kwargs)

    monkeypatch.setattr(challenger, "should_challenge", should)
    monkeypatch.setattr(challenger.AssertionChallenger, "challenge", challenge)
    return calls


def _lifecycle():
    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    return MemoryLifecycle("hirb-session", LifecycleConfig())


def _observe(tmp_path):
    _lifecycle().observe(tool_name="Bash", tool_input={"command": "whoami"}, tool_result=FACT_TEXT, cwd=str(tmp_path))


def _learn(tmp_path):
    _lifecycle().learn(tool_name="Bash", error=FACT_TEXT, cwd=str(tmp_path))


def _distill(tmp_path):
    lifecycle = _lifecycle()
    lifecycle._current_user_turn = f"Who is the CTO? {FACT_TEXT}"
    lifecycle.distill(FACT_TEXT, cwd=str(tmp_path))


def _sm_add(tmp_path):
    from smartmemory_app import storage

    storage.ingest(FACT_TEXT, memory_type="episodic", origin="cli:add")


def _semantic_fact(tmp_path):
    from smartmemory_app import storage

    storage.ingest(FACT_TEXT, memory_type="semantic", origin="cli:add")


def _stored(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT memory_type, json_extract(properties, '$.origin') FROM nodes "
            "WHERE json_extract(properties, '$.content') LIKE '%Alice Johnson%' "
            "AND memory_type IN ('episodic', 'semantic', 'pending')"
        ).fetchall()


def _box_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("work_nodes", "wg_box") if t in tables)


# label, write, (stored memory_type, stored origin, pipeline planned work, challenge reached). Distill stores
# ``pending`` yet still runs the pipeline today: the type reaches core only through ``context``, which the
# pending-bypass check in ``_ingest_inner`` does not read. Passing ``memory_type=`` from the wrapper would move it
# onto the bypass, so the wrapper keeps passing it through context (see CHANGELOG).
ROWS = [
    ("hook_observe", _observe, ("episodic", "hook:observe", True, False)),
    ("hook_learn", _learn, ("episodic", "hook:learn", True, False)),
    ("lifecycle_distill", _distill, ("pending", "lifecycle:distill", True, False)),
    ("sm_add_episodic", _sm_add, ("episodic", "cli:add", True, False)),
    ("semantic_fact_non_hook", _semantic_fact, ("semantic", "cli:add", True, True)),
]


@pytest.mark.parametrize(("label", "write", "expected"), ROWS, ids=[r[0] for r in ROWS])
def test_hook_notes_keep_type_and_pipeline_and_skip_challenge(
    label, write, expected, lite_env, tmp_path, challenger_calls
):
    write(tmp_path)
    db = lite_env / "memory.db"
    stored = _stored(db)
    assert len(stored) == 1, f"{label}: stored {stored}"
    reached = any(name == "challenge" for name, _ in challenger_calls)
    actual = (*stored[0], _box_count(db) > 0, reached)
    assert actual == expected, f"{label}: {actual} challenger calls {challenger_calls}"
