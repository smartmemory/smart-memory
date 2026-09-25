"""Durable capture transitions, recovery, and public CLI acknowledgement."""

import json
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from smartmemory_app import capture_queue as queue, capture_worker as worker
from smartmemory_app.lifecycle import MemoryLifecycle


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(queue, "spawn_worker", lambda: None)


def test_fifo_transitions_and_recovery(monkeypatch):
    first = queue.enqueue("same", "/first", "ws")
    second = queue.enqueue("same", "/second", "ws")
    queue.transition(first, "running")  # Simulate a worker killed after claim.
    seen = []
    monkeypatch.setattr(
        worker, "import_capture", lambda j: seen.append(j["transcript_path"]) or {}
    )
    worker.run()
    assert seen == ["/first", "/second"]
    assert queue.counts() == {"queued": 0, "done": 2, "error": 0}
    records = [
        json.loads(p.read_text())
        for p in sorted((queue.capture_dir() / "events").glob("*.json"))
    ]
    assert [
        r["status"] for r in records if r["capture_id"] == second["capture_id"]
    ] == ["queued", "running", "done"]


def test_torn_temporary_record_is_not_acknowledged():
    queue.enqueue("s", "/file", "ws")
    (queue.capture_dir() / "events" / ".torn.tmp").write_text('{"capture_id":')
    assert len(queue.jobs()) == 1


@pytest.mark.parametrize("outcome,code", [("done", 0), ("error", 1), ("queued", 2)])
def test_drain_cli_codes(outcome, code, monkeypatch):
    from smartmemory_app.cli import cli

    queue.enqueue("s", "/file", "ws")
    if outcome == "error":
        monkeypatch.setattr(
            worker, "import_capture", Mock(side_effect=RuntimeError("bad transcript"))
        )
    else:
        monkeypatch.setattr(worker, "import_capture", lambda j: {})
    if outcome != "queued":
        monkeypatch.setattr(queue, "spawn_worker", worker.run)
    result = CliRunner().invoke(cli, ["lifecycle", "drain", "--timeout", "0.02"])
    assert result.exit_code == code, result.output
    assert json.loads(result.output) == {
        key: int(key == outcome) for key in ("queued", "done", "error")
    }
    if outcome == "error":
        assert "bad transcript" in queue.jobs()[0]["error"]


def test_missing_path_is_acknowledged_error(caplog):
    MemoryLifecycle("missing").persist()
    assert "missing transcript_path" in caplog.text
    assert queue.jobs()[0]["status"] == "error"


def test_spawn_failure_keeps_durable_job(monkeypatch, caplog):
    monkeypatch.setattr(
        queue, "spawn_worker", Mock(side_effect=OSError("cannot spawn"))
    )
    MemoryLifecycle("s").persist(cwd="/project", transcript_path="/file")
    assert queue.jobs()[0]["status"] == "queued"
    assert "cannot spawn" in caplog.text


def test_session_end_enqueues_without_last_response(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(queue, "spawn_worker", spawn)
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "pinned")
    MemoryLifecycle("session").persist(cwd="/project", transcript_path="/file")
    job = queue.jobs()[0]
    assert job["session_id"] == "session" and job["workspace_id"] == "pinned"
    assert job["transcript_path"] == "/file" and job["queued_at"]
    spawn.assert_called_once()


def test_detached_worker_stdio(monkeypatch):
    from smartmemory_app.capture_queue import spawn_worker

    # The fixture replaced the public function, so exercise the module source's
    # implementation through the saved original below.
    assert spawn_worker is not _spawn_worker
    popen = Mock()
    monkeypatch.setattr(queue.subprocess, "Popen", popen)
    _spawn_worker()
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] and kwargs["close_fds"]
    assert kwargs["stdin"] == queue.subprocess.DEVNULL


_spawn_worker = queue.spawn_worker


def test_duplicate_session_end_skips_completed_import(tmp_path, monkeypatch):
    from types import SimpleNamespace

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s",
                "message": {"content": "A durable lesson."},
            }
        )
        + "\n"
    )
    mem = Mock()
    mem._graph.find_by_source_path.return_value = None
    mem.ingest_conversation_sync.return_value = SimpleNamespace(
        chunks_failed=0,
        chunks_ingested=1,
        chunk_results=[SimpleNamespace(item_id="chunk")],
    )
    monkeypatch.setattr(worker, "_memory", lambda: mem)
    for _ in range(2):
        MemoryLifecycle("s").persist(transcript_path=str(transcript))
        worker.run()
    assert queue.counts() == {"queued": 0, "done": 2, "error": 0}
    mem.ingest_conversation_sync.assert_called_once()
    assert queue.jobs()[-1]["unchanged"]


def test_partial_chunk_failure_is_error(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s",
                "message": {"content": "A durable lesson."},
            }
        )
        + "\n"
    )
    mem = Mock()
    mem._graph.find_by_source_path.return_value = None
    mem.ingest_conversation_sync.return_value = SimpleNamespace(
        chunks_failed=1, chunk_results=[SimpleNamespace(error="chunk write failed")]
    )
    monkeypatch.setattr(worker, "_memory", lambda: mem)
    queue.enqueue("s", str(transcript), "ws")
    worker.run()
    assert queue.jobs()[0]["status"] == "error"
    assert "chunk write failed" in caplog.text
    mem.close.assert_called_once()


def test_groq_pin_never_uses_openai_key(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("LLM_API_KEY", "wrong-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", "sonnet")
    factory = Mock()
    monkeypatch.setattr("smartmemory.tools.factory.create_lite_memory", factory)
    worker._memory()
    from smartmemory.utils.llm import provider_pin

    assert provider_pin() == "groq"
    assert worker.os.environ["LLM_API_KEY"] == "test-groq"
    assert worker.os.environ["OPENAI_BASE_URL"] == "https://api.groq.com/openai/v1"
    assert worker.os.environ["SMARTMEMORY_LLM_MODEL"] == "openai/gpt-oss-120b"


def test_missing_groq_key_fails_closed(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        worker._memory()
