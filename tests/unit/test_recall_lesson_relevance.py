"""DEMO-CC-UPLIFT-1 Stage 2a regression: weakly matching lessons must not evict relevant recall.

Lesson texts, the flagship prompt and the chunk text are verbatim from the S20 payload probe
(`artifacts/history/payload-after-S20.json`). There, 8 lessons "matched" the prompt, 3 of them only on
the token `s` from "Stripe's", filled all 5 slots and cut the only T1a carrier (the S2 chunk) to 9th.
"""

from types import SimpleNamespace

import pytest

from smartmemory_app import recall_format, storage

QUERY = (
    "Add support for refunds, full and partial, triggered by Stripe's `charge.refunded` "
    "webhook, per docs/refunds-contract.md. Post them to the ledger and submit them for "
    "clearing."
)
T1B = "Two partial reversals of the same payment can't be accepted on the same clearing date."
WEAK_LESSONS = {
    "s-login": "Everyone will have to log in again. Once a manager has either setting, refresh "
    'records and access tokens created without the claims are rejected as "missing".',
    "s-limiter": "SlidingWindowRateLimiter now checks its limits and windows when it's built, "
    "rejecting nonpositive or noninteger limits and windows, including bools.",
    "s-metrics": "`collect_metrics` now operates on a copy of the queue's records, providing "
    "immutable snapshots and preventing side-effects.",
    "docs-md": "Error messages do not include the rejected identifier value, following the "
    "no-PII logging rule in `docs/CONVENTIONS.md`.",
    "s-jitter": "A jitter function, if provided, must return a finite value; it's validated.",
    "s-keys": "A keyspace migration is required rather than a drop-in replacement; that's final.",
    "s-config": "All configuration errors raise ConfigError with the setting's name.",
}
CHUNK = (
    "User: We're certifying with Nordbank for refunds before we build them. Get a test file "
    "accepted: it must contain a partial reversal of EUR 30.00 and a partial reversal of EUR "
    "20.00. Assistant: a reversal's end-to-end ID must be exactly RV + the original E2E ID."
)


def node(iid, content, memory_type="decision"):
    return SimpleNamespace(
        item_id=iid,
        content=content,
        memory_type=memory_type,
        metadata={"workspace_id": "test", "status": "active"},
        confidence=1.0,
        reference=False,
        origin="import:claude_code:lesson",
    )


def test_query_words_drop_possessive_and_short_tokens():
    words = recall_format.query_words(QUERY)
    assert "s" not in words
    assert "md" not in words
    assert {"refunds", "partial", "clearing", "ledger"} <= words


def test_weak_single_token_lessons_do_not_match_and_strong_rank_first():
    lessons = [node(k, v) for k, v in WEAK_LESSONS.items()]
    lessons.insert(3, node("t1b", T1B))
    matched = recall_format.matching_lessons(lessons, QUERY)
    assert [recall_format._item_to_recall_dict(m)["item_id"] for m in matched] == [
        "t1b"
    ]


def test_single_word_query_still_matches_lesson():
    assert recall_format.matching_lessons([node("t1b", T1B)], "reversals")


def test_lessons_rank_by_overlap():
    weak = node("weak", "Partial captures are logged.")
    strong = node("strong", "Partial refunds post to the ledger before clearing.")
    matched = recall_format.matching_lessons([weak, strong], QUERY)
    assert [recall_format._item_to_recall_dict(m)["item_id"] for m in matched][
        0
    ] == "strong"


@pytest.fixture
def s20_recall(monkeypatch):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    monkeypatch.setattr("smartmemory.origin_policy.get_default_tiers", lambda _: {1, 2})
    monkeypatch.setattr(
        "smartmemory.origin_policy.filter_by_tiers", lambda rows, tiers: list(rows)
    )
    lessons = [node(k, v) for k, v in WEAK_LESSONS.items()]
    lessons.insert(3, node("t1b", T1B))
    semantic = [node("chunk", CHUNK, "pending")] + [
        node(f"other{i}", f"Unrelated episode {i}.", "episodic") for i in range(9)
    ]
    # A semantic hit that is also a decision must not be hoisted above the top chunk.
    semantic.insert(2, node("sem-decision", WEAK_LESSONS["s-login"] + " (again)"))
    mem = SimpleNamespace(
        search=lambda text, *, top_k, **kw: semantic[:top_k],
        _graph=SimpleNamespace(search_nodes=lambda filters: lessons),
    )
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    return lambda: storage.recall(
        cwd="/project",
        query=QUERY,
        top_k=5,
        include_snapshot=False,
        workspace_id="test",
        strict=False,
    )


def test_s20_relevant_chunk_survives_top_k(s20_recall):
    out = s20_recall()
    ids = recall_format.payload_ids(out)
    # Selection follows rank: matched lesson, then semantic hits in order. Display still
    # groups decision lines under "## Lessons & decisions".
    assert set(ids) == {"t1b", "chunk", "other0", "sem-decision", "other1"}
    for weak in WEAK_LESSONS:
        assert weak not in ids
