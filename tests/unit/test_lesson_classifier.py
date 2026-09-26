"""Offline RC5 classifier validation, prefix fallback, and CLI batch contracts."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from smartmemory_app.cli import lifecycle_reclassify
from smartmemory_app.lesson_classifier import PROMPT, classify_lessons
from smartmemory_app.session_lessons import capture_lessons

FIXTURES = Path(__file__).parents[1] / "fixtures/lesson_classifier"


@pytest.fixture
def replay(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "ws")
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    calls = []

    def install(name):
        def call(**kwargs):
            calls.append(kwargs)
            if name == "http-error":
                raise RuntimeError("HTTP 503")
            return None, (FIXTURES / f"{name}.txt").read_text()

        monkeypatch.setattr("smartmemory_app.lesson_classifier.call_llm", call)

    return install, calls


def test_prompt_transport_and_separate_usage(replay, monkeypatch):
    from smartmemory.utils.llm_client.openai_chat import set_last_usage

    install, calls = replay
    install("external-first")
    original = __import__(
        "smartmemory_app.lesson_classifier", fromlist=["call_llm"]
    ).call_llm

    def measured(**kwargs):
        set_last_usage(
            dict(model="openai/gpt-oss-120b", prompt_tokens=100, completion_tokens=10)
        )
        return original(**kwargs)

    monkeypatch.setattr("smartmemory_app.lesson_classifier.call_llm", measured)
    receipt = {"lesson_usage": {"prompt_tokens": 7}}
    assert classify_lessons(["Partner rule", "Internal validation"], receipt) == [
        "constraint",
        "finding",
    ]
    assert receipt["lesson_usage"] == {"prompt_tokens": 7}
    assert receipt["lesson_classifier_usage"]["prompt_tokens"] == 100
    assert len(calls) == 1
    call = calls[0]
    assert call["user_content"] == PROMPT + "0. Partner rule\n1. Internal validation"
    assert call["api_base"] == "https://api.groq.com/openai/v1"
    assert call["temperature"] == 0
    assert "response_format" not in call
    assert "response_model" not in call


@pytest.mark.parametrize(
    "name",
    ["invalid", "out-of-range", "boolean", "missing", "wrong-type", "http-error"],
)
def test_invalid_response_fails(replay, name):
    replay[0](name)
    with pytest.raises((ValueError, RuntimeError)):
        classify_lessons(["A lesson"], {})


@pytest.mark.parametrize(
    "name,tagged,expected,source",
    [
        ("internal", True, "finding", "classifier"),
        ("external-first", False, "constraint", "classifier"),
        ("http-error", True, "constraint", "model_prefix"),
        ("invalid", False, "finding", "model_prefix"),
        ("out-of-range", True, "constraint", "model_prefix"),
    ],
)
def test_capture_authority_and_failure(
    replay, monkeypatch, caplog, name, tagged, expected, source
):
    replay[0](name)
    text = "Parser must reject invalid input with ValueError."
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kwargs: (
            None,
            json.dumps(
                [
                    {
                        "type": "conclusion",
                        "content": ("CONSTRAINT: " if tagged else "") + text,
                    }
                ]
            ),
        ),
    )
    mem = Mock()
    mem._graph.search_nodes.return_value = []
    mem.find_decision_conflicts.return_value = []
    mem.search.return_value = []
    mem.add_decision.return_value = SimpleNamespace(decision_id="d1")
    result = capture_lessons(
        mem,
        dict(session_id="s", workspace_id="ws", transcript_path="/tmp/s"),
        [dict(role="user", content=text)],
        [],
    )
    assert result["lessons_complete"]
    assert "lesson_classifier_usage" in result
    stored = mem.add_decision.call_args.kwargs
    assert stored["content"] == text
    assert stored["context_snapshot"]["lesson_kind"] == expected
    assert stored["context_snapshot"]["lesson_kind_source"] == source
    if source == "model_prefix":
        assert any(
            r.levelname == "WARNING" and "falling back to model_prefix" in r.message
            for r in caplog.records
        )
    retry = capture_lessons(
        mem,
        dict(session_id="s", workspace_id="ws", transcript_path="/tmp/s"),
        [dict(role="user", content=text)],
        [],
    )
    assert "lesson_classifier_usage" not in retry


def make_item(i, **meta):
    return SimpleNamespace(
        item_id=f"d{i}",
        content=f"Lesson {i}",
        memory_type="decision",
        origin="import:claude_code:lesson",
        confidence=1,
        reference=False,
        metadata={
            "workspace_id": "ws",
            "status": "active",
            "lesson_kind": "constraint",
            "context_snapshot": {
                "workspace_id": "ws",
                "lesson_kind": "constraint",
                "session_id": "s",
            },
            **meta,
        },
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_reclassify_scope_and_writes(replay, monkeypatch, dry_run):
    replay[0]("external-first")
    mem = Mock()
    mem._graph.search_nodes.return_value = [
        make_item(0),
        make_item(1),
        make_item(2, workspace_id="other"),
        make_item(3, status="retracted"),
    ]
    monkeypatch.setattr("smartmemory_app.storage.get_memory", lambda: mem)
    result = CliRunner().invoke(lifecycle_reclassify, ["--dry-run"] if dry_run else [])
    assert result.exit_code == 0, result.output
    assert "Before: constraint=2" in result.output
    assert "constraint=1, finding=1" in result.output
    assert "d0: Lesson 0" in result.output
    assert "d1: Lesson 1" not in result.output
    assert mem.update_properties.call_count == (0 if dry_run else 2)
    if not dry_run:
        props = mem.update_properties.call_args.args[1]
        assert props["lesson_kind"] == "finding"
        assert props["lesson_kind_source"] == "classifier"
        assert props["context_snapshot"]["session_id"] == "s"
        assert props["context_snapshot"]["lesson_kind"] == "finding"


def test_reclassify_failed_batch_untouched(replay, monkeypatch):
    responses = iter(["external-first", "out-of-range"])
    monkeypatch.setattr(
        "smartmemory_app.lesson_classifier.call_llm",
        lambda **kwargs: (None, (FIXTURES / f"{next(responses)}.txt").read_text()),
    )
    mem = Mock()
    mem._graph.search_nodes.return_value = [make_item(i) for i in range(4)]
    monkeypatch.setattr("smartmemory_app.storage.get_memory", lambda: mem)
    result = CliRunner().invoke(lifecycle_reclassify, ["--batch-size", "2"])
    assert result.exit_code != 0
    assert "batch 2; this batch was not written" in result.output
    assert [c.args[0] for c in mem.update_properties.call_args_list] == ["d0", "d1"]
