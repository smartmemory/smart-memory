"""Saved Groq classifications replayed through a real persistent lite store."""

import json
from pathlib import Path

from smartmemory_app import storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.session_lessons import capture_lessons

FIXTURES = Path(__file__).parents[1] / "fixtures/lesson_lifecycle_live"


def test_live_responses_three_sessions(tmp_path, monkeypatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    from smartmemory_app.lifecycle_config import LifecycleConfig

    for key, value in {
        "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SMARTMEMORY_WORKSPACE_ID": "payments",
        "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
        "SMARTMEMORY_EMBEDDING_BACKEND": "onnx",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SMARTMEMORY_NO_WARM": "1",
        "GROQ_API_KEY": "fake-only",
        "SMARTMEMORY_HOOK_TRACE": str(tmp_path / "trace.jsonl"),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "OPENAI_BASE_URL",
        "SMARTMEMORY_CAPTURE_OFFLINE",
    ):
        monkeypatch.delenv(key, raising=False)

    sessions = json.loads((FIXTURES / "sessions.json").read_text())
    recorded_ids = [
        "dec_b37e696750b3",
        "dec_f2a4cb7237f7",
        "dec_58570d0b6e51",
        "dec_1fb3d26e6631",
    ]
    id_map = {}
    calls = []
    receipts = []

    def make_memory():
        return create_lite_memory(
            str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
        )

    for index, session in enumerate(sessions):
        kind_fixture = "external-two" if index == 1 else "external-first"
        kind_response = (
            FIXTURES.parent / "lesson_classifier" / f"{kind_fixture}.txt"
        ).read_text()
        monkeypatch.setattr(
            "smartmemory_app.lesson_classifier.call_llm",
            lambda **kwargs: (None, kind_response),
        )

        def extraction(**kwargs):
            return None, json.dumps(
                [dict(type="conclusion", content=text) for text in session["lessons"]]
            )

        def classifier(**kwargs):
            calls.append(index)
            assert index > 0
            rows = json.loads((FIXTURES / f"s{index}-response.json").read_text())
            pairs = {
                (p["lesson"], p["old_id"])
                for p in json.loads(kwargs["user_content"])["pairs"]
            }
            for row in rows:
                row["old_id"] = id_map[row["old_id"]]
            # Fixed S1 changes S2's eligible candidates: never revive the old rule.
            rows = [r for r in rows if (r["lesson"], r["old_id"]) in pairs]
            assert len(rows) == 3
            assert {(r["lesson"], r["old_id"]) for r in rows} == pairs
            return None, json.dumps(rows)

        monkeypatch.setattr(
            "smartmemory.plugins.extractors.reasoning.call_llm", extraction
        )
        monkeypatch.setattr("smartmemory_app.lesson_lifecycle.call_llm", classifier)
        mem = make_memory()
        job = dict(
            session_id=f"live-s{index}",
            workspace_id="payments",
            transcript_path=f"/tmp/live-s{index}",
        )
        try:
            receipt = capture_lessons(mem, job, session["turns"], [])
            assert "degradation" not in receipt, receipt
            receipts.append(receipt)
            if index == 0:
                id_map[recorded_ids[0]] = receipt["lesson_ids"][0]
            elif index == 1:
                id_map.update(zip(recorded_ids[1:], receipt["lesson_ids"]))
                assert all(
                    r["relation"] == "supersedes" for r in receipt["lesson_transitions"]
                )
                assert all(
                    r["evidence"] and not r["evidence"].startswith('"')
                    for r in receipt["lesson_transitions"]
                )
            else:
                assert all(
                    r["relation"] == "unrelated" for r in receipt["lesson_transitions"]
                )
            if index > 0:
                old = mem.get_decision(id_map[recorded_ids[0]])
                assert old.status == "superseded"
                assert old.superseded_by == receipts[1]["lesson_ids"][0]
                assert all(
                    mem.get_decision(i).status == "active"
                    for i in receipts[1]["lesson_ids"]
                )
                current = [mem.get_decision(id_map[i]).to_dict() for i in recorded_ids]
                if index == 1:
                    after_s1 = current
                else:
                    assert current == after_s1
            retry = capture_lessons(mem, job, [], [])
            assert retry["lesson_ids"] == receipt["lesson_ids"]
            assert retry["lesson_transitions"] == receipt["lesson_transitions"]
        finally:
            mem.close()
        if index == 0:
            continue
        mem = make_memory()
        try:
            monkeypatch.setattr(storage, "get_memory", lambda: mem)
            lifecycle = MemoryLifecycle(f"fresh-live-{index}")
            orient = lifecycle.orient("/tmp/payments")
            recall = lifecycle.recall(
                "payments deploys approval security team", "/tmp/payments"
            )
            successor = sessions[1]["lessons"][1]
            successor_id = receipts[1]["lesson_ids"][1]
            assert orient.startswith("## Rules learned in this project\n")
            card = orient.split("\n## ", 1)[0]
            assert successor in card
            assert f"[mem:{successor_id}]" in card
            assert successor not in recall
            assert f"[mem:{successor_id}]" not in recall
            recall_only = MemoryLifecycle(
                f"fresh-live-recall-only-{index}",
                config=LifecycleConfig(rules_card_enabled=False),
            ).recall("payments deploys approval security team", "/tmp/payments")
            assert successor in recall_only
            assert f"[mem:{successor_id}]" in recall_only
            for payload in (orient, recall, recall_only):
                assert f"[mem:{id_map[recorded_ids[0]]}]" not in payload
                assert sessions[0]["lessons"][0] not in payload
        finally:
            mem.close()
    assert calls == [1, 2]
