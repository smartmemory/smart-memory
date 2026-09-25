"""T6: real parser/extractors with an injected hosted-LLM boundary."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartmemory_app import capture_queue as queue, capture_worker
from smartmemory_app.session_lessons import ORIGIN, capture_lessons

FIXTURE = Path(__file__).parents[1] / "fixtures/session_final_lesson.jsonl"
LESSON = (
    "Nordbank refunds require reversal E2E id RV plus the original E2E id. "
    "Only one reversal per original reference per clearing date is allowed, "
    "so same-day partial refunds conflict."
)


@pytest.fixture
def lesson_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq")
    for key in ("LLM_PROVIDER", "LLM_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    mem = Mock()
    records = []
    mem._graph.search_nodes.side_effect = lambda filters: records
    mem._graph.find_by_source_path.return_value = None

    def add(content, **kwargs):
        iid = f"lesson-{len(records)}"
        records.append(SimpleNamespace(item_id=iid, content=content, metadata=kwargs))
        return SimpleNamespace(decision_id=iid)

    mem.add_decision.side_effect = add
    mem.ingest_conversation_sync.return_value = SimpleNamespace(
        chunks_failed=0,
        chunks_ingested=1,
        chunk_results=[SimpleNamespace(item_id="chunk-1")],
    )
    monkeypatch.setattr(capture_worker, "_memory", lambda: mem)
    calls = []

    def llm(**kwargs):
        calls.append(kwargs)
        assert LESSON in kwargs["user_content"]  # beyond generic 4k prefix
        return None, json.dumps(
            [
                {
                    "type": "observation",
                    "content": "The clearing contract was checked against the existing adapter.",
                },
                {"type": "conclusion", "content": LESSON},
            ]
        )

    monkeypatch.setattr("smartmemory.plugins.extractors.reasoning.call_llm", llm)
    return mem, records, calls


def enqueue():
    return queue.enqueue("nordbank-session", str(FIXTURE), "ws-test", "/tmp/nordbank")


def test_lesson_created_and_rerun_idempotent(lesson_env):
    mem, records, calls = lesson_env
    job = enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["lesson_ids"] == ["lesson-0"]
    assert calls[0]["api_key"] == "fake-groq"
    assert calls[0]["model"] == "openai/gpt-oss-120b"
    assert records[0].content == LESSON
    kwargs = mem.add_decision.call_args.kwargs
    assert kwargs["origin"] == ORIGIN
    assert kwargs["evidence_ids"] == ["chunk-1"]
    assert kwargs["context_snapshot"]["turn_range"] == [17, 18]
    assert kwargs["context_snapshot"]["workspace_id"] == "ws-test"
    capture_worker.import_capture(job)
    assert mem.add_decision.call_count == 1
    # Growth/recovery without a matching receipt also preserves session identity.
    capture_lessons(mem, job, [], ["chunk-1"])
    assert mem.add_decision.call_count == 1


@pytest.mark.parametrize("failure", ["empty", "error", "offline"])
def test_extraction_degrades_but_import_done(lesson_env, monkeypatch, caplog, failure):
    def llm(**kwargs):
        if failure == "error":
            raise RuntimeError("provider down")
        return None, "[]"

    monkeypatch.setattr("smartmemory.plugins.extractors.reasoning.call_llm", llm)
    if failure == "offline":
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")
    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["item_ids"] == ["chunk-1"]
    assert "nordbank-session" in receipt["degradation"]
    assert "lost session lessons" in caplog.text
    assert not lesson_env[0].add_decision.called


def test_max_eight_lessons(lesson_env, monkeypatch):
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    {"type": "conclusion", "content": f"Constraint {i}: " + LESSON}
                    for i in range(12)
                ]
            ),
        ),
    )
    enqueue()
    capture_worker.run()
    assert len(queue.jobs()[-1]["lesson_ids"]) == 8


def test_retry_degraded_receipt_adds_lessons_without_reimport(lesson_env, monkeypatch):
    job = enqueue()
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")
    capture_worker.run()
    assert queue.jobs()[-1]["degradation"]
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE")
    receipt = capture_worker.import_capture(job)
    assert receipt["unchanged"]
    assert receipt["lesson_ids"] == ["lesson-0"]
    assert lesson_env[0].ingest_conversation_sync.call_count == 1
