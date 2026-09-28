"""Fake LLM, real extraction, lifecycle receipt and retry contracts."""

import json
from types import SimpleNamespace

import pytest
from smartmemory.models.decision import Decision
from smartmemory.utils.llm_client.openai_chat import set_last_usage

from smartmemory_app import lesson_lifecycle as lifecycle
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


TEXT = "Replace the old support export limit of 250 rows with 1000 rows."
OLD = "Support export limit of 250 rows"


def change(**overrides):
    return dict(
        dict(
            change_id="C1",
            evidence_turn="T0",
            evidence=TEXT,
            old_rule=OLD,
            new_rule_or_null="Support export limit of 1000 rows",
            kind="replace",
        ),
        **overrides,
    )


def pair(oid, **overrides):
    return dict(
        dict(
            lesson=0,
            old_id=oid,
            change_id="C1",
            reason="This target encodes the old support export limit",
            primary=True,
            replaced_rule=OLD,
        ),
        **overrides,
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    mem = Memory()
    old = mem.add_decision(content=OLD, context_snapshot={"workspace_id": "ws"})
    job = dict(session_id="new", workspace_id="ws", transcript_path="/tmp/session")
    calls = []
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kw: (
            None,
            json.dumps(
                [
                    dict(
                        type="conclusion",
                        content="Support export limit is now 1000 rows.",
                    )
                ]
            ),
        ),
    )
    monkeypatch.setattr(
        "smartmemory_app.lesson_classifier.call_llm", lambda **kw: (None, "[]")
    )

    def install(changes, rows):
        responses = iter([changes, rows])

        def fake(**kw):
            calls.append(kw)
            set_last_usage(
                dict(model=lifecycle.MODEL, prompt_tokens=20, completion_tokens=7)
            )
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return None, json.dumps(response)

        monkeypatch.setattr(lifecycle, "call_llm", fake)

    return mem, old, job, calls, install


def capture(setup):
    mem, _, job, _, _ = setup
    return capture_lessons(mem, job, [dict(role="user", content=TEXT)], [])


def test_empty_step_one_skips_matching_and_retirement(setup):
    mem, old, _, calls, install = setup
    install([], AssertionError("must skip"))
    result = capture(setup)
    assert len(calls) == 1
    assert old.status == "active" and len(mem.decisions) == 2
    assert result["lifecycle_unverified_claims"] == 0
    assert result["lifecycle_verified_changes"] == []
    assert result["lesson_transitions"][0]["relation"] == "unrelated"
    assert set(json.loads(calls[0]["user_content"])) == {"SESSION_ONLY_QUOTABLE_SOURCE"}


def test_verified_change_multi_target_receipt_retry_and_requests(setup):
    mem, old, job, calls, install = setup
    targets = [old] + [
        mem.add_decision(content=t, context_snapshot={"workspace_id": "ws"})
        for t in (
            "EXPORT_LIMIT = 250 for support exports",
            "export_limit() returns 250 for support exports",
            "Support export test asserts a limit of 250 rows",
        )
    ]
    install([change()], [pair(d.decision_id) for d in targets])
    result = capture(setup)
    assert "degradation" not in result
    assert len(calls) == 2
    assert all(d.status == "superseded" for d in targets)
    assert len({d.superseded_by for d in targets}) == 1
    assert all(
        r["evidence"] == TEXT and r["evidence_turn"] == "T0" and r["change_id"] == "C1"
        for r in result["lesson_transitions"]
    )
    payload = json.loads(calls[1]["user_content"])
    assert set(payload) == {"VERIFIED_CHANGES", "COMPARISON_NOT_QUOTABLE"}
    assert payload["VERIFIED_CHANGES"] == [change()]
    assert payload["COMPARISON_NOT_QUOTABLE"]["pairs"][0]["content"] == OLD
    retry = capture_lessons(mem, job, [], [])
    assert retry["lesson_transitions"] == result["lesson_transitions"]
    assert retry["lifecycle_unverified_claims"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize("relation", ["replace", "replaces", "garbage", []])
def test_step_two_relation_field_is_ignored(setup, relation):
    _, old, _, _, install = setup
    install([change()], [pair(old.decision_id, relation=relation)])
    result = capture(setup)
    assert old.status == "superseded"
    assert result["lesson_transitions"][0]["relation"] == "supersedes"
    assert result["lifecycle_unverified_claims"] == 0


def test_null_change_id_is_unrelated_even_with_legacy_relation(setup):
    _, old, _, _, install = setup
    install([change()], [pair(old.decision_id, change_id=None, relation="replace")])
    result = capture(setup)
    assert old.status == "active"
    assert result["lesson_transitions"][0]["relation"] == "unrelated"
    assert result["lifecycle_unverified_claims"] == 0


def test_legacy_explicit_field_is_not_required(setup):
    _, old, _, _, install = setup
    install([change()], [pair(old.decision_id, explicit=False)])
    result = capture(setup)
    assert old.status == "superseded"
    assert result["lifecycle_unverified_claims"] == 0


def test_recorded_s66_step_two_row_derives_supersedes():
    # Minimal fixture copied from /tmp/fix4-lifecycle-S66-k5.jsonl, D response.
    quote = (
        "Replace the previous support export policy: the owner now approves "
        "1000 rows for every workspace, replacing the old 250-row limit."
    )
    recorded_change = {
        "change_id": "export_limit_update_1",
        "evidence_turn": "T0",
        "evidence": quote,
        "old_rule": "export limit is 250 rows per workspace",
        "new_rule_or_null": "export limit is 1000 rows per workspace",
        "kind": "replace",
    }
    recorded_row = {
        "lesson": 0,
        "old_id": "dec_8d641aa6a8f9",
        "change_id": "export_limit_update_1",
        "relation": "replace",
        "reason": "Test asserts that export_limit() returns 250 rows, directly encoding the old export‑limit rule.",
        "explicit": True,
        "primary": True,
        "replaced_rule": "export limit is 250 rows per workspace",
    }
    turns = [{"content": quote}]
    verified = lifecycle.verify_changes([recorded_change], turns, {})
    receipt = {}
    rows = lifecycle.validate_pairs(
        {
            (
                0,
                recorded_row["old_id"],
            ): "Added tests that assert export_limit() returns 250."
        },
        turns,
        [recorded_row],
        receipt,
        verified,
    )
    assert rows[0]["relation"] == "supersedes"
    assert rows[0]["evidence"] == quote
    assert rows[0]["evidence_turn"] == "T0"
    assert receipt.get("lifecycle_unverified_claims", 0) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence": "invented"},
        {"evidence": OLD},
        {"evidence": ""},
        {"evidence": None},
        {"evidence_turn": "T1"},
        {"evidence_turn": None},
        {"evidence_turn": []},
        {"change_id": None},
        {"change_id": []},
        {"change_id": ""},
        {"old_rule": None},
        {"old_rule": ""},
        {"kind": "other"},
        {"new_rule_or_null": None},
        {"kind": "withdraw"},
    ],
)
def test_invalid_change_dropped_warning_receipt(setup, caplog, overrides):
    mem, old, _, calls, install = setup
    install([change(**overrides)], [pair(old.decision_id)])
    result = capture(setup)
    assert old.status == "active" and len(mem.decisions) == 2
    assert len(calls) == 1
    assert result["lifecycle_unverified_claims"] == 1
    assert "change" in caplog.text and "rejected" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"change_id": "unknown"}, "unknown_change_id"),
        ({"change_id": []}, "unknown_change_id"),
        ({"replaced_rule": "Different policy with 250"}, "replaced_rule_mismatch"),
        ({"replaced_rule": ""}, "replaced_rule_mismatch"),
        ({"replaced_rule": None}, "replaced_rule_mismatch"),
        ({"replaced_rule": {}}, "replaced_rule_mismatch"),
        ({"reason": ""}, "missing_reason"),
        ({"reason": []}, "missing_reason"),
    ],
)
def test_bad_match_never_retires(setup, caplog, overrides, reason):
    _, old, _, _, install = setup
    install([change()], [pair(old.decision_id, **overrides)])
    result = capture(setup)
    assert old.status == "active"
    assert result["lesson_transitions"][0]["downgrade_reason"] == reason
    assert result["lifecycle_unverified_claims"] == 1
    assert old.decision_id in caplog.text and reason in caplog.text


def test_sibling_unrelated_and_unverified_id_rejected(setup):
    mem, old, _, _, install = setup
    sibling = mem.add_decision(
        content="Support export requires nonempty names",
        context_snapshot={"workspace_id": "ws"},
    )
    install(
        [change(), change(change_id="bad", evidence="invented")],
        [
            pair(old.decision_id, change_id="bad"),
            pair(sibling.decision_id, change_id=None),
        ],
    )
    result = capture(setup)
    assert old.status == sibling.status == "active"
    assert result["lifecycle_unverified_claims"] == 2
    assert all(r["relation"] == "unrelated" for r in result["lesson_transitions"])


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"lesson": []},
        {"lesson": True, "old_id": "bad"},
        {"lesson": 0, "old_id": "unknown"},
        "repeat",
    ],
)
def test_bad_pair_isolation(setup, caplog, bad):
    mem, old, _, _, install = setup
    other = mem.add_decision(content=OLD, context_snapshot={"workspace_id": "ws"})
    rows = [pair(old.decision_id), pair(other.decision_id)]
    rows.insert(0, dict(rows[0]) if bad == "repeat" else bad)
    install([change()], rows)
    result = capture(setup)
    assert other.status == "superseded"
    assert old.status == ("active" if bad == "repeat" else "superseded")
    assert result["lifecycle_unverified_claims"] == 1
    assert "WARNING" in caplog.text


def test_withdrawal(setup):
    _, old, _, _, install = setup
    install(
        [change(kind="withdraw", new_rule_or_null=None)],
        [pair(old.decision_id)],
    )
    result = capture(setup)
    assert old.status == "retracted"
    assert result["lesson_transitions"][0]["relation"] == "retracts"
    assert result["lesson_ids"] == []


def test_conflicting_shared_changes_do_not_bypass_guard(setup):
    mem, old, _, _, install = setup
    other = mem.add_decision(
        content="Different requirement", context_snapshot={"workspace_id": "ws"}
    )
    install(
        [change(), change(change_id="C2")],
        [pair(old.decision_id), pair(other.decision_id, change_id="C2")],
    )
    result = capture(setup)
    assert old.status == other.status == "active"
    assert all(
        r["downgrade_reason"] == "conflicting_relations"
        for r in result["lesson_transitions"]
    )
    assert result["lifecycle_unverified_claims"] == 2


def test_repeated_changes_rejected_but_valid_change_survives(setup):
    _, old, _, _, install = setup
    install(
        [change(), change(), change(change_id="C2")],
        [pair(old.decision_id, change_id="C2")],
    )
    result = capture(setup)
    assert old.status == "superseded"
    assert [r["change_id"] for r in result["lifecycle_verified_changes"]] == ["C2"]
    assert result["lifecycle_unverified_claims"] == 1


@pytest.mark.parametrize("step", [1, 2])
def test_call_failure_accounts_usage_and_keeps_old(setup, step):
    _, old, _, calls, install = setup
    install(RuntimeError("down") if step == 1 else [change()], RuntimeError("down"))
    result = capture(setup)
    assert "lifecycle classification" in result["degradation"]
    assert old.status == "active"
    assert len(calls) == step
    # Inspect measured lifecycle records; extraction has unmeasured fake usage.
    assert result["lesson_usage"]["calls"][-1]["prompt_tokens"] == 20


def test_no_cross_workspace_discovery(setup):
    _, old, _, calls, install = setup
    old.context_snapshot["workspace_id"] = "other"
    install([change()], [])
    assert capture(setup)["lesson_transitions"] == []
    assert calls == []


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
        ('"  "', ""),
    ],
)
def test_normalize_evidence(raw, expected):
    assert lifecycle.normalize_evidence(raw) == expected


def test_cited_turn_not_any_turn():
    receipt = {}
    turns = [dict(content="Other turn"), dict(content=TEXT)]
    assert lifecycle.verify_changes([change()], turns, receipt) == {}
    assert lifecycle.verify_changes([change(evidence_turn="T1")], turns, {})


def test_duplicate_refines_deferred_explicitly():
    assert "relation label" in lifecycle.MATCH_PROMPT
    assert "default and common answer is []" in lifecycle.CHANGE_PROMPT


@pytest.mark.parametrize(
    "primary,chosen", [([False, True, False], 1), ([False] * 3, 0), ([True] * 3, 0)]
)
def test_primary_and_conflict_guard(primary, chosen):
    pairs = {(i, "old"): OLD for i in range(3)}
    rows = [pair("old", lesson=i, primary=p) for i, p in enumerate(primary)]
    result = lifecycle.validate_pairs(
        pairs, [dict(content=TEXT)], rows, {}, {"C1": change()}
    )
    assert all(r["successor_lesson"] == chosen for r in result)
    rows[1]["change_id"] = "C2"
    result = lifecycle.validate_pairs(
        pairs,
        [dict(content=TEXT)],
        rows,
        {},
        {"C1": change(), "C2": change(kind="withdraw", new_rule_or_null=None)},
    )
    assert all(r["relation"] == "unrelated" for r in result)


def test_crash_resume_does_not_repeat_calls_or_resurrect(setup, monkeypatch):
    mem, old, job, calls, install = setup
    install([change()], [pair(old.decision_id)])
    save = lifecycle.save_plan
    writes = []

    def crash(path, plan):
        writes.append(True)
        if len(writes) == 2:
            raise RuntimeError("crash before checkpoint")
        save(path, plan)

    monkeypatch.setattr(lifecycle, "save_plan", crash)
    assert "degradation" in capture(setup)
    successor = mem.get_decision(old.superseded_by)
    successor.status = "retracted"
    result = capture_lessons(mem, job, [], [])
    assert "degradation" not in result
    assert successor.status == "retracted"
    assert mem.transitions == ["supersede"]
    assert len(calls) == 2


@pytest.mark.parametrize("raw", [None, {}, "invalid"])
def test_nonarray_change_response_warns_and_degrades(setup, raw):
    _, old, _, _, install = setup
    install(raw, [])
    result = capture(setup)
    assert "degradation" in result
    assert result["lifecycle_unverified_claims"] == 1
    assert old.status == "active"


def test_usage_sums_both_steps(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    responses = iter([[change()], [pair("old")]])

    def fake(**kw):
        set_last_usage(
            dict(model=lifecycle.MODEL, prompt_tokens=20, completion_tokens=7)
        )
        return None, json.dumps(next(responses))

    monkeypatch.setattr(lifecycle, "call_llm", fake)
    from smartmemory_app.session_lessons import _lesson_usage

    receipt = {
        "lesson_usage": _lesson_usage(
            dict(model=lifecycle.MODEL, prompt_tokens=10, completion_tokens=5),
            lifecycle.MODEL,
        )
    }
    lifecycle.classify_pairs(
        [dict(content=TEXT)],
        ["new rule"],
        [dict(lesson=0, old_id="old", content=OLD)],
        receipt,
    )
    assert receipt["lesson_usage"]["prompt_tokens"] == 50
    assert receipt["lesson_usage"]["completion_tokens"] == 19


def test_valid_replacement_leaves_complementary_sibling_active(setup):
    mem, old, _, _, install = setup
    sibling = mem.add_decision(
        content="Support export names must be nonempty strings",
        context_snapshot={"workspace_id": "ws"},
    )
    install(
        [change()],
        [
            pair(old.decision_id),
            pair(
                sibling.decision_id,
                change_id=None,
                replaced_rule=None,
            ),
        ],
    )
    result = capture(setup)
    assert old.status == "superseded"
    assert sibling.status == "active"
    assert result["lifecycle_unverified_claims"] == 0


@pytest.mark.parametrize(
    "quote",
    [
        '"Replace the old support export limit of 250 rows with 1000 rows."',
        "Replace the old support export limit of 250\nrows with 1000 rows.",
    ],
)
def test_change_typography_normalization(quote):
    result = lifecycle.verify_changes(
        [change(evidence=quote)], [dict(content=TEXT)], {}
    )
    assert result["C1"]["evidence"] == TEXT


def test_malformed_change_does_not_discard_valid_sibling(setup):
    _, old, _, _, install = setup
    install([None, change()], [pair(old.decision_id)])
    result = capture(setup)
    assert old.status == "superseded"
    assert result["lifecycle_unverified_claims"] == 1
