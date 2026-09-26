"""Raw response replay through capture and a reopened Lite store."""

import json
from pathlib import Path

import pytest

from smartmemory_app import storage
from smartmemory_app.rules_card import build_rules_card
from smartmemory_app.session_lessons import capture_lessons

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rules_card"


@pytest.mark.parametrize(
    ("response", "source"),
    [
        ("response.json", "classifier"),
        ("response-live-tagged.json", "classifier"),
        ("response-live-untagged.json", "classifier"),
    ],
)
def test_capture_card_and_lifecycle_on_reopened_lite_store(
    tmp_path, monkeypatch, response, source
):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    for key, value in {
        "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SMARTMEMORY_WORKSPACE_ID": "ws",
        "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
        "SMARTMEMORY_EMBEDDING_BACKEND": "onnx",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SMARTMEMORY_NO_WARM": "1",
        "GROQ_API_KEY": "fixture-only",
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "OPENAI_BASE_URL",
        "SMARTMEMORY_CAPTURE_OFFLINE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kwargs: (None, (FIXTURES / response).read_text()),
    )
    mem = create_lite_memory(
        str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
    )
    try:
        receipt = capture_lessons(
            mem,
            dict(
                session_id="capture", workspace_id="ws", transcript_path="/tmp/session"
            ),
            json.loads((FIXTURES / "session.json").read_text()),
            [],
            "2026-09-26",
        )
        assert "degradation" not in receipt, receipt
        assert len(receipt["lesson_ids"]) == 3
    finally:
        mem.close()
    mem = create_lite_memory(
        str(tmp_path / "data"), pipeline_profile=PipelineConfig.tier1()
    )
    try:
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        for index, iid in enumerate(receipt["lesson_ids"]):
            item = mem.get(iid)
            assert item.metadata["lesson_kind"] == (
                "constraint" if index < 2 else "finding"
            )
            assert item.metadata["lesson_kind_source"] == source
            assert not item.content.startswith("CONSTRAINT:")
        card = build_rules_card()
        assert "UTF-8" in card and "AP-409" in card and "queue" not in card
        mem.retract_decision(
            receipt["lesson_ids"][0], "Partner withdrew the format policy."
        )
        card = build_rules_card()
        assert "UTF-8" not in card and "AP-409" in card
        monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "elsewhere")
        assert build_rules_card() == ""
    finally:
        mem.close()


@pytest.fixture(autouse=True)
def constraint_classifier_replay(monkeypatch):
    """Isolate RC5 classification from existing extraction/lifecycle recordings."""
    from pathlib import Path

    response = (
        Path(__file__).parents[1] / "fixtures/lesson_classifier/external-two.txt"
    ).read_text()
    monkeypatch.setattr(
        "smartmemory_app.lesson_classifier.call_llm", lambda **kwargs: (None, response)
    )


def test_reclassify_persists_in_reopened_store(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    from smartmemory_app.cli import lifecycle_reclassify
    from smartmemory_app.session_lessons import ORIGIN

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only")
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    response = (FIXTURES.parent / "lesson_classifier/internal.txt").read_text()
    monkeypatch.setattr(
        "smartmemory_app.lesson_classifier.call_llm", lambda **kwargs: (None, response)
    )
    mem = create_lite_memory(str(tmp_path), pipeline_profile=PipelineConfig.tier1())
    try:
        decision = mem.add_decision(
            content="Parser must reject negative inputs.",
            origin=ORIGIN,
            context_snapshot={"workspace_id": "ws", "lesson_kind": "constraint"},
        )
        mem.update_properties(
            decision.decision_id, {"workspace_id": "ws", "lesson_kind": "constraint"}
        )
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        dry = CliRunner().invoke(lifecycle_reclassify, ["--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "finding=1" in dry.output
        assert mem.get(decision.decision_id).metadata["lesson_kind"] == "constraint"
        result = CliRunner().invoke(lifecycle_reclassify, [])
        assert result.exit_code == 0, result.output
        assert "finding=1" in result.output
    finally:
        mem.close()
    mem = create_lite_memory(str(tmp_path), pipeline_profile=PipelineConfig.tier1())
    try:
        item = mem.get(decision.decision_id)
        assert item.metadata["lesson_kind"] == "finding"
        assert item.metadata["lesson_kind_source"] == "classifier"
        assert (
            mem.get_decision(decision.decision_id).context_snapshot["lesson_kind"]
            == "finding"
        )
        assert build_rules_card() == ""
    finally:
        mem.close()
