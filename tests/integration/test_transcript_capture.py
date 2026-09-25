"""Real JSONL importer, detached capture worker, SQLite and local ONNX recall.

Fixture shape inspected from a ~/.claude/projects/ JSONL: parentUuid,
isSidechain, cwd, sessionId, timestamp, type, uuid, message.role/content text
blocks. All fixture prose/identifiers were authored; no private text copied.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from smartmemory_app import capture_queue as queue, storage
from smartmemory_app.cli import cli
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.recall_format import derive_workspace_id

pytestmark = pytest.mark.integration
LESSON = "Before retrying a payment request, retain its original idempotency key to prevent duplicate charges."


@pytest.fixture
def capture_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = tmp_path / "transcript.jsonl"
    rows = [
        json.loads(line)
        for line in (Path(__file__).parents[1] / "fixtures/session_end_lesson.jsonl")
        .read_text()
        .splitlines()
    ]
    for row in rows:
        row["cwd"] = str(cwd)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(cwd), path


def finish():
    result = CliRunner().invoke(cli, ["lifecycle", "drain", "--timeout", "60"])
    assert result.exit_code == 0, (result.output, queue.jobs())
    assert json.loads(result.output)["queued"] == 0


def read_chunks():
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    mem = create_lite_memory(
        str(queue.capture_dir().parent), pipeline_profile=PipelineConfig.tier1()
    )
    return mem, [
        row
        for row in mem._graph.search_nodes({"origin": "import:claude_code"})
        if "turn_range" in row.metadata
    ]


def test_mid_session_lesson_recall_duplicate_and_growth(capture_env, monkeypatch):
    cwd, path = capture_env
    body = {"session_id": "capture-lesson", "cwd": cwd, "transcript_path": str(path)}
    result = CliRunner().invoke(cli, ["lifecycle", "persist"], input=json.dumps(body))
    assert result.exit_code == 0
    finish()
    mem, chunks = read_chunks()
    try:
        assert len(chunks) == 1
        ids = {chunk.item_id for chunk in chunks}
        assert chunks[0].metadata["workspace_id"] == derive_workspace_id(cwd)
        assert chunks[0].metadata["turn_range"] == [0, 10]
        assert chunks[0].metadata["session_id"] == "capture-lesson"
        assert chunks[0].metadata["transcript_path"] == str(path)
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        recalled = storage.recall(
            cwd,
            query="How can payment retries avoid charging customers twice?",
            include_snapshot=False,
        )
        assert LESSON in recalled
        assert LESSON not in storage.recall(
            cwd + "-other", query="payment retries", include_snapshot=False
        )
    finally:
        mem.close()

    MemoryLifecycle("capture-lesson").persist(cwd, str(path))
    finish()
    assert queue.jobs()[-1]["unchanged"]
    # Append enough turns to extend beyond the importer's character chunk limit.
    with path.open("a") as handle:
        for i in range(10, 23):
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "capture-lesson",
                        "uuid": str(i),
                        "message": {
                            "role": "assistant",
                            "content": "Additional rollout verification details. " * 30,
                        },
                    }
                )
                + "\n"
            )
    MemoryLifecycle("capture-lesson").persist(cwd, str(path))
    finish()
    mem, grown = read_chunks()
    try:
        assert len(grown) > 1
        assert ids <= {chunk.item_id for chunk in grown}
        keys = [chunk.metadata["source_path"] for chunk in grown]
        assert len(keys) == len(set(keys))
        assert any(LESSON in chunk.content for chunk in grown)
        assert max(chunk.metadata["turn_range"][1] for chunk in grown) == 23
    finally:
        mem.close()
