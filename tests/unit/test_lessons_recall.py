"""Every hook channel shares non-memory and decision lifecycle filtering."""

import json
from types import SimpleNamespace

import pytest

from smartmemory_app import recall_format, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.remote_backend import RemoteMemory


def row(iid, memory_type="semantic", **kwargs):
    return dict(
        item_id=iid,
        memory_type=memory_type,
        content="refund clearing " + iid,
        origin="import:claude_code:lesson",
        metadata={"workspace_id": "test"},
        **kwargs,
    )


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("channel", ["recency", "semantic", "patterns", "snapshot"])
def test_filters_all_channels_and_prioritizes_lessons(
    monkeypatch, tmp_path, remote, channel
):
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(trace))
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "test")
    bad = [row(t, t) for t in ("entity", "relation", "pattern", "Version")]
    bad.append(row("category", node_category="entity"))
    good = row("episode", "episodic")
    lesson = row("lesson", "decision", session_date="2026-09-24")
    superseded = row("old", "decision", status="superseded")
    retracted = row("retracted", "decision", status="retracted")
    all_rows = bad + [good, superseded, retracted, lesson]
    if remote:
        mem = RemoteMemory(api_url="https://unused.invalid", team_id="test")
        monkeypatch.setattr(mem, "_request", lambda *a, **kw: all_rows)
    else:

        def adapt(r):
            r = dict(r)
            r.setdefault("confidence", 1.0)
            # Local status resides in metadata.
            r["metadata"] = {
                **r["metadata"],
                **{k: r[k] for k in ("status", "session_date") if k in r},
            }
            return SimpleNamespace(**r)

        rows = [adapt(r) for r in all_rows]
        mem = SimpleNamespace(
            search=lambda *a, **kw: rows[: kw["top_k"]],
            _graph=SimpleNamespace(
                search_nodes=lambda f: [adapt(lesson), adapt(superseded)]
            ),
        )
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    query = {
        "recency": None,
        "semantic": "refund clearing",
        "patterns": "patterns refund decisions",
        "snapshot": None,
    }[channel]
    out = storage.recall(
        "/project", top_k=10, query=query, include_snapshot=channel == "snapshot"
    )
    assert "[mem:lesson]" in out and "[session:2026-09-24]" in out
    assert out.index("[mem:lesson]") < out.index("[mem:episode]")
    for iid in [
        "entity",
        "relation",
        "pattern",
        "Version",
        "category",
        "old",
        "retracted",
    ]:
        assert f"[mem:{iid}]" not in out
    record = json.loads(trace.read_text().splitlines()[-1])
    assert record["excluded_non_memory"] == 5
    assert record["lesson_ids"] == ["lesson"]


def test_query_excerpt_uses_tail_and_original_order():
    head = "Unrelated introduction about old UI experiments. " * 60
    tail = "Refund reversals use RV plus original E2E id. Same-day partial refunds conflict."
    block = recall_format.format_recall_lines(
        [row("long", content_override="unused")], 1
    )
    block = block.replace("refund clearing long", head + tail)
    out = recall_format.budget_blocks(
        [(block, None)], 60, query="refund reversals partial E2E"
    )
    assert tail in out
    assert "Unrelated introduction" not in out
    assert "…[excerpt, mem:long]" in out
    assert len(out) <= 240


def test_lessons_atomic_and_all_layers_prioritized(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    lifecycle = MemoryLifecycle("layers")
    episode = recall_format.format_recall_lines([row("e")], 1)
    lesson = recall_format.format_recall_lines([row("d", "decision")], 1)
    out = lifecycle._format_orient_block(episode, lesson)
    assert out.startswith("## Lessons & decisions")
    assert out.index("[mem:d]") < out.index("[mem:e]")
    assert "[decision]" not in recall_format.budget_blocks([(lesson, None)], 10)


def test_unrelated_and_inactive_decisions_do_not_match():
    assert not recall_format.matching_lessons([row("d", "decision")], "garden")
    assert not recall_format.matching_lessons(
        [row("d", "decision", status="pending")], "refund"
    )


def test_remote_decision_api_shape_scope_and_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace"))
    remote = RemoteMemory(api_url="https://unused.invalid", team_id="test")
    calls = []

    def request(method, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/memory/decisions":
            return {
                "decisions": [
                    {
                        "decision_id": "api-lesson",
                        "content": "Refund clearing requires RV ids.",
                        "status": "active",
                        "context_snapshot": {
                            "workspace_id": "test",
                            "session_date": "2026-09-24",
                        },
                    },
                    {
                        "decision_id": "other",
                        "content": "refund",
                        "status": "retracted",
                    },
                ]
            }
        return []

    monkeypatch.setattr(remote, "_request", request)
    out = remote.recall(
        query="refund", workspace_id="test", strict=True, include_snapshot=False
    )
    assert "[decision] [mem:api-lesson]" in out
    assert "[mem:other]" not in out
    assert calls[-1][1]["workspace_id"] == "test"
    monkeypatch.setattr(
        remote,
        "_request",
        lambda method, path, **kw: {"error": "offline"}
        if path == "/memory/decisions"
        else [row("episode")],
    )
    assert "[mem:episode]" in remote.recall(query="refund", include_snapshot=False)
    assert "lost remote decision lookup" in caplog.text
