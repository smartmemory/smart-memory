"""HOOK-CAPTURE-NO-LLM-1: hook and transcript-capture notes make no LLM call, even with a key present.

Real Lite store, real spaCy rules extraction, real local ONNX embeddings, the real
work-graph worker drained in-process. The environment holds a (fake) Groq key, so an
LLM route IS available: the dangerous condition the live daemon was in. Every way
the pipeline reaches a model is trapped and counted (the extractor class, core's
single ``call_llm`` resolver, the supersession judge, the OpenAI SDK client, and any
outbound TCP connect). A user-source save is the control: it still plans the
deferred LLM ``extract`` box.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ENTITY_TEXT = (
    "Alice Johnson deployed Redis on Kubernetes in Berlin for Acme Corporation."
)


class LLMTrap(RuntimeError):
    """Raised by every trapped LLM entry point; the call is also recorded."""


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
    # Core's update-check thread would dial out (through a system proxy, too) and trip the connect trap.
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    # An LLM route exists: the condition the fix must hold under.
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_hook_capture_no_llm")
    for name in (
        "SMARTMEMORY_CAPTURE_OFFLINE",
        "SMARTMEMORY_CAPTURE_LLM",
        "SMARTMEMORY_COSTLESS_ORIGINS",
        "SMARTMEMORY_WORKSPACE_ID",
        "SMARTMEMORY_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    # No detached worker: the test drains the work graph in-process, under the trap.
    monkeypatch.setattr(
        "smartmemory.pipeline.work_graph.spawn.spawn_worker_if_idle",
        lambda _data_dir: False,
    )
    storage._memory = None
    storage._data_path = None
    yield data
    storage._shutdown()
    storage._memory = None
    storage._data_path = None


@pytest.fixture
def llm_calls(monkeypatch):
    """Trap every LLM entry point; the list holds one label per attempted call."""
    calls: list[str] = []

    def trap(label):
        def _trapped(*_args, **_kwargs):
            calls.append(label)
            raise LLMTrap(label)

        return _trapped

    from smartmemory.plugins.extractors.llm_single import LLMSingleExtractor

    monkeypatch.setattr(LLMSingleExtractor, "__init__", trap("LLMSingleExtractor"))
    monkeypatch.setattr("smartmemory.utils.llm._resolve_call_model", trap("call_llm"))
    monkeypatch.setattr(
        "smartmemory.reasoning.detection.supersession_judge.judge_supersession",
        trap("supersession_judge"),
    )
    try:
        import openai

        monkeypatch.setattr(openai.OpenAI, "__init__", trap("openai.OpenAI"))
        monkeypatch.setattr(openai.AsyncOpenAI, "__init__", trap("openai.AsyncOpenAI"))
    except ImportError:
        pass
    real_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            calls.append(f"connect:{address}")
            raise LLMTrap(f"egress {address}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return calls


def _lifecycle():
    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    return MemoryLifecycle("hcnl-session", LifecycleConfig())


def _observe(tmp_path):
    _lifecycle().observe(
        tool_name="Bash",
        tool_input={"command": "kubectl apply -f redis.yaml"},
        tool_result=ENTITY_TEXT,
        workspace_id="ws-hcnl",
        strict=True,
    )


def _learn(tmp_path):
    _lifecycle().learn(
        tool_name="Bash", error=ENTITY_TEXT, workspace_id="ws-hcnl", strict=True
    )


def _distill(tmp_path):
    _lifecycle().distill(
        "Alice Johnson confirmed it runs in Berlin for Acme Corporation.",
        workspace_id="ws-hcnl",
        user_turn=f"Who deployed Redis? {ENTITY_TEXT}",
        strict=True,
    )


def _capture(tmp_path):
    from smartmemory_app.capture_worker import import_capture

    turns = [
        ("user", "Who owns the cache rollout?"),
        ("assistant", ENTITY_TEXT),
        ("user", "Where does Bob Smith run the backups?"),
        (
            "assistant",
            "Bob Smith runs the PostgreSQL backups in Frankfurt for Acme Corporation.",
        ),
    ]
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "".join(
            json.dumps(
                {
                    "parentUuid": str(index - 1) if index else None,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": str(tmp_path),
                    "sessionId": "hcnl-capture",
                    "type": role,
                    "uuid": str(index),
                    "timestamp": f"2026-10-25T01:00:0{index}Z",
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
            for index, (role, text) in enumerate(turns)
        )
    )
    result = import_capture(
        {
            "transcript_path": str(path),
            "workspace_id": "ws-hcnl",
            "session_id": "hcnl-capture",
            "cwd": str(tmp_path),
        }
    )
    # Session lessons are an LLM step: costless capture skips them with a named degradation.
    assert "SMARTMEMORY_CAPTURE_LLM=1" in result["degradation"]
    assert result["chunks_ingested"] >= 1


def _user_add(tmp_path):
    from smartmemory_app import storage

    storage.ingest(ENTITY_TEXT, memory_type="semantic", origin="cli:add")


# label, origin of the stored note, write, costless (no LLM) expected
ROWS = [
    ("hook_observe", "hook:observe", _observe, True),
    ("hook_learn", "hook:learn", _learn, True),
    ("lifecycle_distill", "lifecycle:distill", _distill, True),
    ("transcript_capture", "import:claude_code", _capture, True),
    ("user_source_control", "cli:add", _user_add, False),
]


def _box_kinds(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        rows = []
        if "work_nodes" in tables:
            rows += conn.execute("SELECT kind, status FROM work_nodes").fetchall()
        if "wg_box" in tables:
            rows += conn.execute("SELECT kind, status FROM wg_box").fetchall()
    return rows


def _entity_edges(db: Path, origin: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM edges e JOIN nodes n ON n.item_id = e.source_id "
            "WHERE e.edge_type = 'CONTAINS_ENTITY' AND json_extract(n.properties, '$.origin') = ?",
            (origin,),
        ).fetchone()[0]


@pytest.mark.parametrize(
    ("label", "origin", "write", "costless"), ROWS, ids=[row[0] for row in ROWS]
)
def test_internal_notes_make_no_llm_call(
    label, origin, write, costless, lite_env, tmp_path, request
):
    from smartmemory_app.storage import apply_runtime_config
    from smartmemory.utils.llm import llm_route_available

    apply_runtime_config()
    assert llm_route_available()[0], (
        "precondition: an LLM route must be available for this test to mean anything"
    )
    db = lite_env / "memory.db"

    if not costless:
        write(tmp_path)
        kinds = {kind for kind, _ in _box_kinds(db)}
        assert "extract" in kinds, f"control lost its deferred LLM extraction: {kinds}"
        return

    calls = request.getfixturevalue("llm_calls")
    write(tmp_path)
    from smartmemory.pipeline.work_graph.worker import run_worker

    assert run_worker(lite_env, idle_exit=0.5, lease=30) == 0
    assert calls == [], f"{label}: LLM entry points reached: {calls}"
    boxes = _box_kinds(db)
    assert boxes, f"{label}: no work-graph run was planned"
    assert not [kind for kind, _ in boxes if kind == "extract"], (
        f"{label}: deferred LLM extract planned: {boxes}"
    )
    assert all(status in {"done", "skipped"} for _, status in boxes), (
        f"{label}: unfinished boxes {boxes}"
    )
    assert _entity_edges(db, origin) > 0, (
        f"{label}: rules extraction produced no entities"
    )


# Origin selection and its SMARTMEMORY_COSTLESS_ORIGINS rollback: core tests/unit/pipeline/test_costless_origin.py.
