"""Fake LLM, real extraction, lifecycle receipt and retry contracts."""

import json
from types import SimpleNamespace

import pytest
from smartmemory.models.decision import Decision
from smartmemory.utils.llm_client.openai_chat import set_last_usage

from smartmemory_app.session_lessons import capture_lessons


class Memory:
    def __init__(self):
        self.decisions = {}
        self._graph = SimpleNamespace(search_nodes=self.search_nodes)
        self.transitions = []

    def search_nodes(self, filters):
        return [
            SimpleNamespace(
                item_id=d.decision_id,
                metadata={"context_snapshot": d.context_snapshot},
                content=d.content,
            )
            for d in self.decisions.values()
        ]

    def find_decision_conflicts(self, decision, limit):
        return list(self.decisions.values())[:limit]

    def search(self, **kwargs):
        return []

    def add_decision(self, **fields):
        fields.pop("origin", None)
        d = Decision(decision_id=Decision.generate_id(), **fields)
        self.decisions[d.decision_id] = d
        return d

    def get_decision(self, iid):
        return self.decisions.get(iid)

    def supersede_decision(self, iid, new, reason):
        old = self.decisions[iid]
        assert old.status == "active"
        old.status = "superseded"
        old.superseded_by = new.decision_id
        old.context_snapshot["superseded_reason"] = reason
        self.decisions[new.decision_id] = new
        self.transitions.append("supersede")

    def retract_decision(self, iid, reason):
        self.decisions[iid].status = "retracted"
        self.decisions[iid].context_snapshot["retraction_reason"] = reason
        self.transitions.append("retract")

    def update_properties(self, iid, context):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    mem = Memory()
    old = mem.add_decision(
        content="Rule X = A", context_snapshot={"workspace_id": "ws"}
    )
    job = dict(session_id="new", workspace_id="ws", transcript_path="/tmp/session")
    text = "Rule X changed to B. The old rule X = A no longer holds."
    calls = []

    def install(relation, evidence=None, explicit=True, failure=False):
        def extraction(**kwargs):
            set_last_usage(
                dict(model="openai/gpt-oss-120b", prompt_tokens=10, completion_tokens=5)
            )
            return None, json.dumps(
                [
                    dict(
                        type="conclusion",
                        content="Rule X = B for all production deployments.",
                    )
                ]
            )

        def classifier(**kwargs):
            calls.append(kwargs)
            assert kwargs["api_key"] == "fake"
            set_last_usage(
                dict(model="openai/gpt-oss-120b", prompt_tokens=20, completion_tokens=7)
            )
            if failure:
                raise RuntimeError("classifier down")
            return None, json.dumps(
                [
                    dict(
                        lesson=0,
                        old_id=old.decision_id,
                        relation=relation,
                        evidence=text if evidence is None else evidence,
                        reason="Session explicitly changed the rule",
                        explicit=explicit,
                    )
                ]
            )

        monkeypatch.setattr(
            "smartmemory.plugins.extractors.reasoning.call_llm", extraction
        )
        monkeypatch.setattr("smartmemory_app.lesson_lifecycle.call_llm", classifier)

    return mem, old, job, text, calls, install


@pytest.mark.parametrize(
    "relation,status,count",
    [
        ("supersedes", "superseded", 1),
        ("retracts", "retracted", 0),
        ("duplicate", "active", 1),
        ("refines", "active", 2),
        ("unrelated", "active", 2),
    ],
)
def test_relations_and_retry(setup, relation, status, count):
    mem, old, job, text, calls, install = setup
    install(relation)
    args = (mem, job, [dict(role="user", content=text)], [])
    result = capture_lessons(*args)
    assert "degradation" not in result, result
    assert result["lesson_usage"]["prompt_tokens"] == 30
    assert result["lesson_usage"]["completion_tokens"] == 12
    assert old.status == status
    assert sum(d.status == "active" for d in mem.decisions.values()) == count
    assert result["lesson_transitions"][0]["relation"] == relation
    if relation == "supersedes":
        assert old.superseded_by == result["lesson_ids"][0]
        assert text in old.context_snapshot["superseded_reason"]
    if relation == "duplicate":
        assert result["lesson_ids"] == [old.decision_id]
    again = capture_lessons(*args)
    assert again["lesson_ids"] == result["lesson_ids"]
    assert again["lesson_transitions"] == result["lesson_transitions"]
    assert len(calls) == 1
    assert len(mem.transitions) <= 1
    assert "lesson_usage" not in again


@pytest.mark.parametrize(
    "evidence,explicit", [("invented evidence", True), (None, False)]
)
def test_ambiguous_keeps_both(setup, caplog, evidence, explicit):
    mem, old, job, text, _, install = setup
    install("supersedes", evidence=evidence, explicit=explicit)
    with caplog.at_level("INFO"):
        result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert old.status == "active"
    assert len(mem.decisions) == 2
    assert result["lesson_transitions"][0]["relation"] == "unrelated"
    reason = "not_explicit" if not explicit else "evidence_not_found"
    assert result["lesson_transitions"][0]["downgrade_reason"] == reason
    assert reason in caplog.text
    assert "keeping both" in caplog.text


def test_failure_degrades_and_usage_sums(setup, caplog):
    mem, old, job, text, _, install = setup
    install("supersedes", failure=True)
    result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert "lifecycle classification" in result["degradation"]
    assert "classifier down" in caplog.text
    assert old.status == "active"
    assert len(mem.decisions) == 2
    usage = result["lesson_usage"]
    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 12
    assert usage["cost_usd"] == pytest.approx((30 * 0.00015 + 12 * 0.0006) / 1000)
    assert usage["usage_scope"] == "final_response_only"
    assert usage["call_count"] is None


def test_cross_workspace_excluded(setup):
    mem, old, job, text, calls, install = setup
    old.context_snapshot["workspace_id"] = "elsewhere"
    install("supersedes")
    result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert result["lesson_transitions"] == []
    assert calls == []
    assert old.status == "active"


def test_crash_after_transition_resumes_without_resurrection(setup, monkeypatch):
    from smartmemory_app import lesson_lifecycle

    mem, old, job, text, calls, install = setup
    install("supersedes")
    save = lesson_lifecycle.save_plan
    writes = []

    def crash(path, plan):
        writes.append(True)
        if len(writes) == 2:
            raise RuntimeError("crash before checkpoint")
        save(path, plan)

    monkeypatch.setattr(lesson_lifecycle, "save_plan", crash)
    args = (mem, job, [dict(role="user", content=text)], [])
    assert "degradation" in capture_lessons(*args)
    replacement = mem.get_decision(old.superseded_by)
    replacement.status = "retracted"  # a subsequent session retired it
    result = capture_lessons(*args)
    assert "degradation" not in result
    assert replacement.status == "retracted"
    assert mem.transitions == ["supersede"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('  "a b"  ', "a b"),
        ("'a b'", "a b"),
        ("“a b”", "a b"),
        ("‘a b’", "a b"),
        ("`a b`", "a b"),
        ("platform‑team", "platform-team"),
        ("a‐b‑c‒d–e—f―g−h", "a-b-c-d-e-f-g-h"),
        ("a\n\t b\u00a0c", "a b c"),
        ("Ａ says “yes” and ‘no’", "A says \"yes\" and 'no'"),
        ('"  "', ""),
        ("“mismatched'", "\"mismatched'"),
    ],
)
def test_normalize_evidence(raw, expected):
    from smartmemory_app.lesson_lifecycle import normalize_evidence

    assert normalize_evidence(raw) == expected


@pytest.mark.parametrize(
    "primary,chosen", [([False, True, False], 1), ([False] * 3, 0), ([True] * 3, 0)]
)
def test_agreeing_successors_and_retry(setup, monkeypatch, caplog, primary, chosen):
    mem, old, job, text, _, _ = setup
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(type="conclusion", content=f"New deployment rule {i}")
                    for i in range(3)
                ]
            ),
        ),
    )
    monkeypatch.setattr(
        "smartmemory_app.lesson_lifecycle.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(
                        lesson=i,
                        old_id=old.decision_id,
                        relation="supersedes",
                        evidence='"' + text + '"',
                        explicit=True,
                        reason="Rule changed",
                        primary=p,
                    )
                    for i, p in enumerate(primary)
                ]
            ),
        ),
    )
    with caplog.at_level("INFO"):
        result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert "degradation" not in result
    assert old.superseded_by == result["lesson_ids"][chosen]
    assert len(mem.transitions) == 1
    assert all(mem.get_decision(i).status == "active" for i in result["lesson_ids"])
    assert all(
        r["successor_of_record"] == old.superseded_by and r["relation"] == "supersedes"
        for r in result["lesson_transitions"]
    )
    assert ("primary fallback" in caplog.text) == (sum(primary) != 1)
    again = capture_lessons(mem, job, [], [])
    assert again["lesson_transitions"] == result["lesson_transitions"]
    assert len(mem.transitions) == 1


@pytest.mark.parametrize("duplicate_targets", [False, True])
def test_multiple_old_targets(setup, monkeypatch, duplicate_targets, caplog):
    mem, old, job, text, _, install = setup
    install("supersedes")
    other = mem.add_decision(
        content=old.content if duplicate_targets else "Different rule",
        context_snapshot={"workspace_id": "ws"},
    )
    monkeypatch.setattr(
        "smartmemory_app.lesson_lifecycle.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(
                        lesson=0,
                        old_id=d.decision_id,
                        relation="supersedes",
                        evidence=text,
                        explicit=True,
                        reason="Changed",
                    )
                    for d in (old, other)
                ]
            ),
        ),
    )
    with caplog.at_level("INFO"):
        result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert "degradation" not in result
    if duplicate_targets:
        assert old.status == other.status == "superseded"
        assert old.superseded_by == other.superseded_by == result["lesson_ids"][0]
    else:
        assert old.status == other.status == "active"
        assert all(
            r["downgrade_reason"] == "conflicting_relations"
            for r in result["lesson_transitions"]
        )
        assert "conflicting_relations" in caplog.text


def test_conflicting_successors(setup, monkeypatch, caplog):
    mem, old, job, text, _, _ = setup
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(type="conclusion", content=f"Deployment conclusion {i}")
                    for i in range(2)
                ]
            ),
        ),
    )
    monkeypatch.setattr(
        "smartmemory_app.lesson_lifecycle.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(
                        lesson=i,
                        old_id=old.decision_id,
                        relation=relation,
                        evidence=text,
                        explicit=True,
                        reason="Changed",
                    )
                    for i, relation in enumerate(["supersedes", "retracts"])
                ]
            ),
        ),
    )
    with caplog.at_level("INFO"):
        result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert old.status == "active"
    assert len(mem.decisions) == 3
    assert all(
        r["downgrade_reason"] == "conflicting_relations"
        for r in result["lesson_transitions"]
    )
    assert "conflicting_relations" in caplog.text


@pytest.mark.parametrize(
    "evidence", ["“Rule X changed\n to B.”", "`Rule X changed to B.`"]
)
def test_normalized_match_is_stored(setup, evidence):
    mem, old, job, text, _, install = setup
    install("supersedes", evidence=evidence)
    result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert old.status == "superseded"
    assert result["lesson_transitions"][0]["evidence"] == "Rule X changed to B."


@pytest.mark.parametrize("evidence", ['" "', "Rule X changed to C."])
def test_empty_or_nonverbatim_evidence_rejected(setup, evidence):
    mem, old, job, text, _, install = setup
    install("supersedes", evidence=evidence)
    result = capture_lessons(mem, job, [dict(role="user", content=text)], [])
    assert old.status == "active"
    assert result["lesson_transitions"][0]["downgrade_reason"] == "evidence_not_found"


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
