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
        ("response.json", "model"),
        ("response-live-tagged.json", "model"),
        ("response-live-untagged.json", "regex_fallback"),
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
