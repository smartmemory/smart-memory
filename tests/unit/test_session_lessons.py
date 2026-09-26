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
    mem.find_decision_conflicts.return_value = []
    mem.search.return_value = []
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


@pytest.fixture
def sdk_response(lesson_env, monkeypatch):
    """Exercise core's real usage producer without any HTTP requests."""
    from smartmemory.utils.llm import call_llm, get_last_usage

    monkeypatch.setattr("smartmemory.plugins.extractors.reasoning.call_llm", call_llm)
    response = SimpleNamespace(
        model="openai/gpt-oss-120b",
        usage=SimpleNamespace(
            prompt_tokens=123,
            completion_tokens=45,
            total_tokens=168,
            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps([{"type": "conclusion", "content": LESSON}])
                )
            )
        ],
    )
    create = Mock(return_value=response)
    client = Mock()
    client.chat.completions.create = create
    factory = Mock(return_value=client)
    monkeypatch.setattr("smartmemory.utils.llm_client.openai_chat.OpenAI", factory)
    get_last_usage()
    yield response, create, factory
    get_last_usage()


def test_usage_receipt_from_provider_response(lesson_env, sdk_response):
    from smartmemory.utils.llm import get_last_usage

    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["lesson_ids"] == ["lesson-0"]
    assert lesson_env[1][0].content == LESSON
    assert receipt["lesson_usage"] == {
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "prompt_tokens": 123,
        "completion_tokens": 45,
        # Core discards cached-token details and SDK attempts. Never invent them.
        "cached_tokens": None,
        "call_count": None,
        "call_count_status": "unmeasured",
        "usage_source": "smartmemory.utils.llm.get_last_usage",
        "usage_scope": "final_response_only",
        "cost_usd": pytest.approx(0.00004545),
        "cost_status": "repository_price",
        "cost_source": {
            "table": "smartmemory.utils.token_tracking.COST_PER_1K_TOKENS",
            "model": "openai/gpt-oss-120b",
            "unit": "USD/1000 tokens",
            "prompt": 0.00015,
            "completion": 0.0006,
        },
    }
    sdk_response[1].assert_called_once()
    assert sdk_response[2].call_args.kwargs["max_retries"] == 5
    assert get_last_usage() is None


@pytest.mark.parametrize("provider_error", [False, True])
def test_unmeasured_usage_warns_without_stale_tokens(
    lesson_env, sdk_response, caplog, provider_error
):
    from smartmemory.utils.llm_client.openai_chat import set_last_usage

    set_last_usage(
        {"prompt_tokens": 999, "completion_tokens": 888, "model": "stale-ingestion"}
    )
    sdk_response[0].usage = None
    if provider_error:
        sdk_response[1].side_effect = RuntimeError("provider down")
    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["item_ids"] == ["chunk-1"]
    assert ("degradation" in receipt) == provider_error
    usage = receipt["lesson_usage"]
    assert usage["usage_source"] == "unmeasured"
    assert usage["model"] == "openai/gpt-oss-120b"
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cost_usd"):
        assert usage[key] is None
    assert usage["cost_status"] == "unpriced"
    assert usage["call_count"] is None
    assert any(
        record.levelname == "WARNING" and "token usage unmeasured" in record.message
        for record in caplog.records
    )
    sdk_response[1].assert_called_once()


def test_retry_usage_is_new_response_only(lesson_env, sdk_response):
    response, create, _ = sdk_response
    response.choices[0].message.content = "[]"
    enqueue()
    capture_worker.run()
    first = queue.jobs()[-1]
    assert first["degradation"]
    assert first["lesson_usage"]["prompt_tokens"] == 123
    response.choices[0].message.content = json.dumps(
        [{"type": "conclusion", "content": LESSON}]
    )
    response.usage.prompt_tokens = 17
    response.usage.completion_tokens = 9
    response.usage.total_tokens = 26
    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["unchanged"]
    assert receipt["lesson_ids"] == ["lesson-0"]
    assert "degradation" not in receipt
    assert receipt["lesson_usage"]["prompt_tokens"] == 17
    assert receipt["lesson_usage"]["completion_tokens"] == 9
    assert receipt["lesson_usage"]["cost_usd"] == pytest.approx(0.00000795)
    assert create.call_count == 2
    assert lesson_env[0].ingest_conversation_sync.call_count == 1
    enqueue()
    capture_worker.run()
    assert "lesson_usage" not in queue.jobs()[-1]
    assert create.call_count == 2


def test_unknown_model_is_unpriced(lesson_env, sdk_response):
    sdk_response[0].model = "unknown-provider-model"
    enqueue()
    capture_worker.run()
    usage = queue.jobs()[-1]["lesson_usage"]
    assert usage["model"] == "unknown-provider-model"
    assert usage["prompt_tokens"] == 123
    assert usage["cost_usd"] is None
    assert usage["cost_status"] == "unpriced"
    assert "cost_source" not in usage


def test_offline_has_no_usage_or_provider_call(lesson_env, sdk_response, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")
    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["degradation"]
    assert "lesson_usage" not in receipt
    sdk_response[1].assert_not_called()


def test_classifier_failure_keeps_transcript_done(lesson_env, monkeypatch):
    from smartmemory.models.decision import Decision

    mem, _, _ = lesson_env
    mem.find_decision_conflicts.return_value = [
        Decision(
            decision_id="old",
            content=LESSON,
            context_snapshot={"workspace_id": "ws-test"},
        )
    ]

    def fail(**kwargs):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr("smartmemory_app.lesson_lifecycle.call_llm", fail)
    enqueue()
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert receipt["item_ids"] == ["chunk-1"]
    assert receipt["lesson_ids"] == ["lesson-0"]
    assert "lifecycle classification" in receipt["degradation"]
    enqueue()
    capture_worker.run()
    retry = queue.jobs()[-1]
    assert retry["degradation"] == receipt["degradation"]
    assert retry["lesson_transitions"] == []
    assert mem.add_decision.call_count == 1


LIVE_S2 = Path(__file__).parents[1] / "fixtures/lesson_rules_live/s2-groq-response.json"


def test_hard_rule_typed_observation_still_becomes_lesson(lesson_env, monkeypatch):
    """Real Groq output types the RV bank rule as `observation` (DEMO-CC-UPLIFT-1 Stage 2a)."""
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kw: (None, LIVE_S2.read_text()),
    )
    enqueue()
    capture_worker.run()
    lessons = [record.content for record in lesson_env[1]]
    assert any("must be exactly `RV` + the original E2E ID" in text for text in lessons)
    assert any("NB-417" in text for text in lessons)
    assert any(
        "same clearing date" in text and "Two partial" in text for text in lessons
    )
    # Incidental context stays an observation.
    assert not any("has not written" in text for text in lessons)


def test_extraction_prompt_types_rules_as_conclusions(lesson_env):
    enqueue()
    capture_worker.run()
    prompt = lesson_env[2][0]["user_content"]
    assert "Every rule, constraint, requirement" in prompt
    assert "supporting facts" not in prompt
    # Bare rules ("ID must be exactly RV + original") were unreachable by a task
    # prompt that never names them; lessons must carry their own context.
    assert "self-contained: name the system" in prompt


@pytest.fixture(autouse=True)
def constraint_classifier_replay(monkeypatch):
    """Isolate RC5 classification from existing extraction/lifecycle recordings."""
    from pathlib import Path

    response = (
        Path(__file__).parents[1] / "fixtures/lesson_classifier/external-first.txt"
    ).read_text()
    monkeypatch.setattr(
        "smartmemory_app.lesson_classifier.call_llm", lambda **kwargs: (None, response)
    )
