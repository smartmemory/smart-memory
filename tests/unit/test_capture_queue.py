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
    # Exercises the opt-in LLM path (HOOK-CAPTURE-NO-LLM-1).
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
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
    # Exercises the opt-in LLM path (HOOK-CAPTURE-NO-LLM-1).
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        worker._memory()


def _dead_hook(root, name, *, payload=True, **extra):
    body = root / "payloads" / f"{name}.json"
    if payload:
        body.write_text(json.dumps({"session_id": "s", "tool_input": {"n": name}}))
    record = {
        "v": 1,
        "phase": "observe",
        "session_id": "s",
        "payload_path": str(body),
        "state_snapshot": None,
        "attempts": 5,
        "last_error": "No space left on device",
        "failed_at": 1.0,
        "queued_ns": 111,
        **extra,
    }
    (root / "dead" / f"{name}.json").write_text(json.dumps(record))


def test_requeue_hooks_table(capsys):
    root = queue.hook_queue_dir()
    _dead_hook(root, "a.observe")
    _dead_hook(root, "b.observe")
    _dead_hook(root, "nopayload.observe", payload=False)
    _dead_hook(root, "badphase.observe", phase="bogus")
    (root / "dead" / "spill-20261025T100001000000Z-1-observe.json").write_text("{")
    (root / "queue" / "dup.observe.json").write_text("{}")
    _dead_hook(root, "dup.observe")
    rows = [
        # names, dry_run, expected (action, name) list, queue after, dead after, exit
        (
            ["a.observe"],
            True,
            [("requeued", "a.observe.json")],
            ["dup.observe.json"],
            6,
            0,
        ),
        (
            ["a.observe.json", "ghost"],
            False,
            [("requeued", "a.observe.json"), ("unknown", "ghost.json")],
            ["a.observe.json", "dup.observe.json"],
            5,
            1,
        ),
        (
            None,
            False,
            [
                ("requeued", "b.observe.json"),
                ("skipped", "badphase.observe.json"),
                ("skipped", "dup.observe.json"),
                ("skipped", "nopayload.observe.json"),
                ("skipped", "spill-20261025T100001000000Z-1-observe.json"),
            ],
            ["a.observe.json", "b.observe.json", "dup.observe.json"],
            4,
            0,
        ),
    ]
    got = []
    for names, dry_run, expected, queued, dead, code in rows:
        capsys.readouterr()
        rc = queue.run_requeue_dead_hooks(names, dry_run)
        out = capsys.readouterr().out
        lines = [
            ln.split(":")[0].replace("would ", "").split()
            for ln in out.splitlines()[:-1]
        ]
        got.append(
            (
                [tuple(ln) for ln in lines] == expected,
                sorted(p.name for p in (root / "queue").glob("*.json")) == queued,
                len(list((root / "dead").glob("*.json"))) == dead,
                rc == code,
            )
        )
    assert got == [(True, True, True, True)] * len(rows)
    record = json.loads((root / "queue" / "a.observe.json").read_text())
    assert (record["queued_ns"], "attempts" in record, "last_error" in record) == (
        111,
        False,
        False,
    )
