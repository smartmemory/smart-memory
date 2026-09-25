"""Real local importer, decision writes/readback, fresh hooks; fake hosted LLM."""

import json
from pathlib import Path

import pytest

from smartmemory_app import capture_queue as queue, capture_worker, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.session_lessons import ORIGIN

pytestmark = pytest.mark.integration
PROMPT = "Add support for refunds, full and partial, triggered by Stripe's `charge.refunded` webhook, per docs/refunds-contract.md. Post them to the ledger and submit them for clearing."
LESSON = "Nordbank refunds require reversal E2E id RV plus the original E2E id. Only one reversal per original reference per clearing date is allowed, so same-day partial refunds conflict."


def test_real_store_lesson_import_and_fresh_hooks(tmp_path, monkeypatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    for key, value in {
        "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SMARTMEMORY_WORKSPACE_ID": "nordbank",
        "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
        "SMARTMEMORY_EMBEDDING_BACKEND": "onnx",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SMARTMEMORY_NO_WARM": "1",
        "GROQ_API_KEY": "fake-only",
        "SMARTMEMORY_HOOK_TRACE": str(tmp_path / "trace.jsonl"),
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("LLM_PROVIDER", "LLM_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)

    def make_memory():
        return create_lite_memory(
            str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
        )

    monkeypatch.setattr(capture_worker, "_memory", make_memory)

    def llm(**kwargs):
        assert LESSON in kwargs["user_content"]
        assert kwargs["user_content"].index(LESSON) > 4000
        return None, json.dumps(
            [
                {
                    "type": "observation",
                    "content": "The clearing contract specifies reversal identity and uniqueness.",
                },
                {"type": "conclusion", "content": LESSON},
            ]
        )

    monkeypatch.setattr("smartmemory.plugins.extractors.reasoning.call_llm", llm)
    fixture = Path(__file__).parents[1] / "fixtures/session_final_lesson.jsonl"
    queue.enqueue("nordbank-session", str(fixture), "nordbank", "/tmp/nordbank")
    capture_worker.run()
    receipt = queue.jobs()[-1]
    assert receipt["status"] == "done"
    assert "degradation" not in receipt, receipt
    mem = make_memory()
    try:
        lesson_id = receipt["lesson_ids"][0]
        item = mem.get(lesson_id)
        assert item.origin == ORIGIN
        assert item.derived_from in receipt["item_ids"]
        assert item.metadata["workspace_id"] == "nordbank"
        assert item.metadata["turn_range"] == [17, 18]
        assert item.metadata["transcript_path"] == str(fixture)
        assert item.metadata["session_id"] == "nordbank-session"
        assert item.metadata["evidence_ids"] == receipt["item_ids"]
        mem._graph.backend.add_node(
            "noise-entity",
            {
                "content": "refund clearing entity",
                "origin": "import:claude_code",
                "workspace_id": "nordbank",
            },
            memory_type="entity",
        )
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        lifecycle = MemoryLifecycle("fresh-session")
        orient = lifecycle.orient("/tmp/nordbank")
        recall = lifecycle.recall(PROMPT, "/tmp/nordbank")
        for payload in (orient, recall):
            assert LESSON in payload
            assert "[entity]" not in payload
            assert payload.startswith("## Lessons & decisions")
            assert "[session:2026-09-24]" in payload
        records = [
            json.loads(line)
            for line in (tmp_path / "trace.jsonl").read_text().splitlines()
        ]
        assert records[-1]["lesson_ids"] == [lesson_id]
        mem.retract_decision(lesson_id, "fixture retirement")
        assert f"[mem:{lesson_id}]" not in storage.recall(
            query=PROMPT, include_snapshot=False
        )
    finally:
        mem.close()
    queue.enqueue("nordbank-session", str(fixture), "nordbank", "/tmp/nordbank")
    capture_worker.run()
    assert queue.jobs()[-1]["lesson_ids"] == receipt["lesson_ids"]


def test_three_session_lifecycle_and_fresh_injection(tmp_path, monkeypatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory
    from smartmemory_app.session_lessons import capture_lessons
    from smartmemory_app.recall_format import format_recall_lines

    for key, value in {
        "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SMARTMEMORY_WORKSPACE_ID": "rules",
        "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
        "SMARTMEMORY_EMBEDDING_BACKEND": "onnx",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SMARTMEMORY_NO_WARM": "1",
        "GROQ_API_KEY": "fake-only",
        "SMARTMEMORY_HOOK_TRACE": str(tmp_path / "trace.jsonl"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    contents = [
        "Deployment rule X requires approval from team A.",
        "Deployment rule X requires approval from team B.",
        "Deployment rule X is wrong and withdrawn with no replacement.",
    ]
    receipts = []
    old_id = None
    for index, content in enumerate(contents):
        evidence = (
            content
            if index != 1
            else ("Deployment rule X changed from team A to team B. " + content)
        )
        monkeypatch.setattr(
            "smartmemory.plugins.extractors.reasoning.call_llm",
            lambda **kw: (None, json.dumps([dict(type="conclusion", content=content)])),
        )

        def classify(**kwargs):
            pairs = json.loads(kwargs["user_content"])["pairs"]
            assert any(p["old_id"] == old_id for p in pairs)
            return None, json.dumps(
                [
                    dict(
                        lesson=0,
                        old_id=old_id,
                        relation="supersedes" if index == 1 else "retracts",
                        evidence=evidence,
                        reason="Explicit session rule change",
                        explicit=True,
                    )
                ]
            )

        monkeypatch.setattr("smartmemory_app.lesson_lifecycle.call_llm", classify)
        mem = create_lite_memory(
            str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
        )
        try:
            receipt = capture_lessons(
                mem,
                dict(
                    session_id=f"s{index}",
                    workspace_id="rules",
                    transcript_path=f"/tmp/s{index}",
                ),
                [dict(role="user", content=evidence)],
                [],
            )
            assert "degradation" not in receipt, receipt
            receipts.append(receipt)
            if index == 0:
                old_id = receipt["lesson_ids"][0]
            elif index == 1:
                old = mem.get_decision(old_id)
                assert old.status == "superseded"
                assert old.superseded_by == receipt["lesson_ids"][0]
                assert evidence in old.context_snapshot["superseded_reason"]
                provenance = mem.get_decision_provenance(receipt["lesson_ids"][0])
                assert provenance["superseded"]
                # Former top result cannot consume the replacement's ranking slot.
                ranked = format_recall_lines(
                    [mem.get(old_id), mem.get(old.superseded_by)], top_k=1
                )
                assert contents[1] in ranked and contents[0] not in ranked
                old_id = receipt["lesson_ids"][0]
            else:
                assert mem.get_decision(old_id).status == "retracted"
                assert receipt["lesson_ids"] == []
        finally:
            mem.close()
        # New connection and lifecycle instance: no session-local state can mask stale rules.
        mem = create_lite_memory(
            str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
        )
        try:
            monkeypatch.setattr(storage, "get_memory", lambda: mem)
            lifecycle = MemoryLifecycle(f"fresh-{index}")
            for payload in (
                lifecycle.orient("/tmp/rules"),
                lifecycle.recall("Deployment rule X approval", "/tmp/rules"),
            ):
                if index < 2:
                    assert content in payload
                if index > 0:
                    assert contents[0] not in payload
                if index == 2:
                    assert contents[1] not in payload
            # A retry of a historical capture must never reactivate its lesson.
            replay = capture_lessons(
                mem,
                dict(session_id="s0", workspace_id="rules", transcript_path="/tmp/s0"),
                [],
                [],
            )
            assert replay["lesson_ids"] == receipts[0]["lesson_ids"]
            if index > 0:
                assert (
                    mem.get_decision(receipts[0]["lesson_ids"][0]).status
                    == "superseded"
                )
        finally:
            mem.close()
