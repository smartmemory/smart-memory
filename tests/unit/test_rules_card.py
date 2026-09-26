"""Rules-card capture, eligibility, budgets, and fresh-session contracts."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartmemory_app import storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.lifecycle_config import LifecycleConfig
from smartmemory_app.recall_format import payload_ids
from smartmemory_app.rules_card import build_rules_card
from smartmemory_app.session_lessons import ORIGIN, capture_lessons

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rules_card"


def lesson(iid, content="Acme Pay accepts UTF-8.", **meta):
    return SimpleNamespace(
        item_id=iid,
        content=content,
        memory_type="decision",
        origin=ORIGIN,
        confidence=1.0,
        reference=False,
        metadata={
            "workspace_id": "ws",
            "status": "active",
            "lesson_kind": "constraint",
            **meta,
        },
    )


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "ws")
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.delenv("SMARTMEMORY_RECALL_STRICT", raising=False)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    mem = Mock()
    mem._graph.search_nodes.return_value = []
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    return mem


@pytest.mark.parametrize(
    ("response", "source", "promoted", "mixed_tags"),
    [
        ("response.json", "model", 1, False),
        ("response-live-tagged.json", "model", 0, False),
        ("response-live-tagged.json", "model", 0, True),
        ("response-live-untagged.json", "regex_fallback", 0, False),
    ],
)
def test_capture_replays_raw_fixture_and_persists_kind(
    environment, monkeypatch, caplog, response, source, promoted, mixed_tags
):
    mem = environment
    monkeypatch.setenv("GROQ_API_KEY", "fixture-only")
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    for key in ("LLM_PROVIDER", "LLM_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    calls = []

    def replay(**kwargs):
        calls.append(kwargs)
        raw = json.loads((FIXTURES / response).read_text())
        if mixed_tags:
            # One tagged rule must suppress fallback for other regex-shaped rules.
            raw[1]["content"] = raw[1]["content"].removeprefix("CONSTRAINT: ")
        return None, json.dumps(raw)

    monkeypatch.setattr("smartmemory.plugins.extractors.reasoning.call_llm", replay)
    mem.find_decision_conflicts.return_value = []
    mem.search.return_value = []
    mem.add_decision.side_effect = [
        SimpleNamespace(decision_id=f"d{i}") for i in range(3)
    ]
    result = capture_lessons(
        mem,
        dict(session_id="capture", workspace_id="ws", transcript_path="/tmp/session"),
        json.loads((FIXTURES / "session.json").read_text()),
        ["chunk"],
        "2026-09-26",
    )
    assert result["lessons_complete"]
    assert result.get("rules_promoted", 0) == promoted
    assert len(calls) == 1
    assert "Prefix a conclusion with CONSTRAINT:" in calls[0]["user_content"]
    stored = [c.kwargs for c in mem.add_decision.call_args_list]
    assert len(stored) == 3
    assert all("CONSTRAINT:" not in row["content"] for row in stored)
    assert [row["context_snapshot"]["lesson_kind"] for row in stored] == [
        "constraint",
        "finding" if mixed_tags else "constraint",
        "finding",
    ]
    assert all(
        row["context_snapshot"]["lesson_kind_source"] == source for row in stored
    )
    assert all(
        call.args[1]["lesson_kind_source"] == source
        for call in mem.update_properties.call_args_list
    )
    warnings = [
        record
        for record in caplog.records
        if "model tagged no constraints" in record.getMessage()
    ]
    if source == "regex_fallback":
        assert len(warnings) == 1
        assert warnings[0].levelname == "WARNING"
        assert warnings[0].getMessage() == (
            "model tagged no constraints; regex fallback classified 2 of 3"
        )
    else:
        assert warnings == []
    if response == "response.json":
        assert stored[1]["context_snapshot"]["turn_range"] == [1, 2]
    assert [c.args[1]["lesson_kind"] for c in mem.update_properties.call_args_list] == [
        "constraint",
        "finding" if mixed_tags else "constraint",
        "finding",
    ]


def test_card_eligibility_and_legacy_warning(environment, caplog):
    legacy = lesson("legacy", "Acme Pay requires ASCII identifiers.")
    del legacy.metadata["lesson_kind"]
    incidental = lesson("incidental", "The adapter was inspected.")
    del incidental.metadata["lesson_kind"]
    environment._graph.search_nodes.return_value = [
        lesson("active", session_date="2026-09-26"),
        legacy,
        incidental,
        lesson("old", status="superseded"),
        lesson("gone", status="retracted"),
        lesson("foreign", workspace_id="other"),
        lesson("finding", "The adapter must use a queue.", lesson_kind="finding"),
        lesson("explicit-null", "The adapter must use a queue.", lesson_kind=None),
    ]
    card = build_rules_card()
    assert payload_ids(card) == ["active", "legacy"]
    assert card.startswith("## Rules learned in this project")
    assert "[session:2026-09-26]" in card
    warnings = [r.message for r in caplog.records if "legacy lessons" in r.message]
    assert warnings == ["Rules card classified 2 legacy lessons with rule regex"]
    environment._graph.search_nodes.assert_called_once_with(
        {"memory_type": "decision", "origin": ORIGIN}
    )


def test_scope_and_recall_filters(environment, monkeypatch):
    missing = lesson("missing", "Required upload format.", workspace_id=None)
    low = lesson("low", "Required download format.")
    low.confidence = 0.1
    reference = lesson("reference", "Required export format.")
    reference.reference = True
    unrelated = lesson("unrelated")
    unrelated.origin = "user"
    entity = lesson("entity", node_category="entity")
    environment._graph.search_nodes.return_value = [
        missing,
        low,
        reference,
        unrelated,
        entity,
    ]
    assert payload_ids(build_rules_card()) == ["missing"]
    monkeypatch.setenv("SMARTMEMORY_RECALL_STRICT", "1")
    assert build_rules_card() == ""


def test_dedupe_keeps_newest_and_sorts_created_time(environment):
    environment._graph.search_nodes.return_value = [
        lesson("old", "Acme  Pay accepts UTF-8.", session_date="2026-08-01"),
        lesson("new", "ACME Pay accepts UTF-8.", session_date="2026-09-26"),
        lesson("middle", "Acme Pay limits batches.", created_at="2026-09-20T12:00:00Z"),
    ]
    assert payload_ids(build_rules_card()) == ["new", "middle"]


def test_budget_keeps_atomic_rules_and_warns(environment, caplog):
    environment._graph.search_nodes.return_value = [
        lesson(
            "large",
            "Acme Pay requires " + "complete content " * 100,
            session_date="2026-09-26",
        ),
        lesson("small", session_date="2026-09-25"),
    ]
    card = build_rules_card(budget=40)
    assert payload_ids(card) == ["small"]
    assert len(card) <= 160
    assert "Rules card dropped 1 lessons" in caplog.text
    assert build_rules_card(budget=0) == ""


def test_remote_warns_without_graph_access(environment, monkeypatch, caplog):
    from smartmemory_app.remote_backend import RemoteMemory

    remote = object.__new__(RemoteMemory)
    monkeypatch.setattr(storage, "get_memory", lambda: remote)
    assert build_rules_card() == ""
    assert "Orient lost rules card" in caplog.text
    assert "remote backend" in caplog.text


def test_orient_card_budget_and_fresh_session_exclusions(environment, monkeypatch):
    environment._graph.search_nodes.return_value = [
        lesson("card", session_date="2026-09-26")
    ]
    recall = Mock(
        return_value="## SmartMemory Context\n- [semantic] [mem:context] context"
    )
    monkeypatch.setattr(storage, "recall", recall)
    monkeypatch.setattr(MemoryLifecycle, "_cache_embedding", lambda *args: None)
    config = LifecycleConfig(orient_budget=20)
    first = MemoryLifecycle("session", config)
    output = first.orient("/tmp/project")
    assert output.index("Rules learned") < output.index("SmartMemory Context")
    assert payload_ids(output) == ["card", "context"]
    fresh = MemoryLifecycle("session", config)
    fresh.recall("Explain batch processing", "/tmp/project")
    assert recall.call_args.kwargs["exclude_ids"] == ["card"]
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "other")
    fresh.recall("Explain another project", "/tmp/other")
    assert "exclude_ids" not in recall.call_args.kwargs


def test_local_recall_excludes_before_top_k(environment):
    environment._graph.search_nodes.return_value = [
        lesson("card", "Acme Pay batches require UTF-8.")
    ]
    environment.search.return_value = [
        lesson("card"),
        lesson("other", "Acme Pay limits batches."),
    ]
    assert payload_ids(
        storage.recall(
            query="Acme Pay batches",
            top_k=1,
            include_snapshot=False,
            exclude_ids=["card"],
        )
    ) == ["other"]


@pytest.mark.parametrize("enabled", [True, False])
def test_orient_survives_card_failure_or_disabled(
    environment, monkeypatch, caplog, enabled
):
    card = Mock(side_effect=RuntimeError("store unavailable"))
    monkeypatch.setattr("smartmemory_app.rules_card.build_rules_card", card)
    monkeypatch.setattr(
        storage,
        "recall",
        lambda *a, **kw: "## SmartMemory Context\n- [semantic] [mem:ok] context",
    )
    lc = MemoryLifecycle("failure", LifecycleConfig(rules_card_enabled=enabled))
    assert "[mem:ok]" in lc.orient()
    assert lc._rules_card_ids == []
    assert card.called == enabled
    assert ("Orient lost rules card" in caplog.text) == enabled


def test_config_round_trip():
    config = LifecycleConfig()
    assert config.rules_card_enabled and config.rules_card_budget == 2000
    assert config.orient_budget == 1500
    custom = config.apply_overrides(
        {"rules_card_enabled": False, "rules_card_budget": 345}
    )
    serialized = asdict(custom)
    serialized["recall_strategy"] = custom.recall_strategy.value
    assert LifecycleConfig.from_config(serialized) == custom


def test_remote_recall_receives_card_exclusions(environment, monkeypatch):
    from smartmemory_app.remote_backend import RemoteMemory

    remote = object.__new__(RemoteMemory)
    remote.search = Mock(
        return_value=[
            {
                "item_id": "card",
                "memory_type": "decision",
                "content": "Acme Pay batches require UTF-8.",
                "origin": ORIGIN,
                "metadata": {"workspace_id": "ws"},
            },
            {
                "item_id": "other",
                "memory_type": "semantic",
                "content": "Batch adapter context.",
                "origin": "cli:add",
                "metadata": {"workspace_id": "ws"},
            },
        ]
    )
    remote._request = Mock(return_value={"decisions": []})
    monkeypatch.setattr(storage, "get_memory", lambda: remote)
    card = storage.recall(
        query="Acme Pay batches", top_k=1, include_snapshot=False, exclude_ids=["card"]
    )
    assert payload_ids(card) == ["other"]


def test_status_surfaces_card_config(environment, monkeypatch):
    import asyncio
    from click.testing import CliRunner
    from smartmemory_app import cli, lifecycle_api

    config = {"rules_card_enabled": False, "rules_card_budget": 345}
    monkeypatch.setattr(cli, "_load_lifecycle_toml", lambda: config)
    result = CliRunner().invoke(cli.lifecycle_status)
    assert result.exit_code == 0
    assert "Rules card enabled: False" in result.output
    assert "Rules card budget: 345 tokens" in result.output
    monkeypatch.setattr(lifecycle_api, "_load_lifecycle_config", lambda: config)
    result = asyncio.run(lifecycle_api.status())
    assert result["rules_card_enabled"] is False
    assert result["rules_card_budget"] == 345
