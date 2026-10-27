"""CLI-SEARCH-FACT-COLLAPSE-1: `sm search` and `sm ask` rank source memories, not rows.
2.0 (CLI-2.0-SEARCH-FACT-COLLAPSE-1): plus the multi-source parent rule rows at the end of ``ROWS``.

Real Lite store (real MiniLM, no LLM), the daemon's HTTP surface and the CLI renderer. Relation
edges are seeded directly with ``fact`` + ``fact_embedding`` + ``source_item`` the way the extraction
stage writes them, because fact rows only exist after LLM extraction. Before the fix a memory's
fact rows filled the Lite top-5 and pushed the second memory out.
"""

import logging

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import cli as cli_module
from smartmemory_app import local_api, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS
from smartmemory_app.search_collapse import collapse_to_parents

# id -> (content, created_at or None for "now"). Memory text never matches the queries below:
# only the facts do, so a fact that matches has to promote its parent.
MEMORIES = {
    "m-680": ("Policy memo, version 7. Ops note.", None),
    "m-appr": ("Thread from Tuesday, Dana replied.", None),
    "m-old": ("Archive note one.", "2026-08-01T00:00:00+00:00"),
    "m-new": ("Archive note two.", "2026-10-01T00:00:00+00:00"),
}
# (edge type, source memory id or None for a legacy edge, fact text)
FACTS = [
    ("CHG_A", "m-680", "minimum credit score cutoff changed from 640 to 680"),
    ("CHG_B", "m-680", "minimum credit score cutoff changed to 680 on retail loans"),
    ("CHG_C", "m-680", "minimum credit score cutoff raised due to delinquencies"),
    ("CHG_D", "m-680", "minimum credit score cutoff review owner is compliance"),
    ("APPR", "m-appr", "credit score cutoff 680 approved by Dana Ortiz"),
    ("OLD", "m-old", "Priya reviews ledger schema change for old billing"),
    ("NEW", "m-new", "Priya reviews ledger schema change for new billing"),
    ("LEGACY", None, "Priya reviews ledger schema change for legacy billing"),
]
CUTOFF = "minimum credit score cutoff"
LEDGER = "who reviews ledger schema change"


def _ids(items):
    return [i["item_id"] for i in items]


def _fact_texts(item):
    return [c["content"] for c in item.get("children", [])]


APPROVAL = "credit score cutoff 680 approved by Dana Ortiz"


# Each check takes (output, caplog text): `items` for /search, the CLI's stdout, `evidence` for /ask,
# the prompt text the LLM was given for the LLM-path ask.
def _crowded_today(items, log):
    # collapse none = core's rows: the memory plus its facts take the slots, the approval is out
    assert "m-appr" not in _ids(items)
    assert sum(i["memory_type"] == "fact" for i in items) >= 3
    # and not one new key: collapse none is byte-for-byte core's rows
    assert not [i for i in items if {"children", "collapse", "facts"} & set(i)]


def _one_row_per_memory(items, log):
    ids = _ids(items)
    assert len(ids) == len(set(ids)) and not any(i.startswith("fact::") for i in ids)
    assert {"m-680", "m-appr"} <= set(ids)  # the approval is promoted by its fact
    parent = next(i for i in items if i["item_id"] == "m-680")
    assert 1 <= len(parent["children"]) <= 3  # nested, capped
    assert "facts" not in parent and parent["collapse"]["kind"] == "fact"
    assert all("cutoff" in t for t in _fact_texts(parent))
    assert _fact_texts(next(i for i in items if i["item_id"] == "m-appr")) == [APPROVAL]
    assert (
        parent["content"] == MEMORIES["m-680"][0]
    )  # parent text, fetched by scoped get


def _parent_rank_is_best_child(items, log):
    assert _ids(items)[0] == "m-680"  # its facts out-score the approval's single fact


def _window_after_old(items, log):
    # since > m-old: its fact is out of window and must not admit m-old
    assert "m-old" not in _ids(items) and "m-new" in _ids(items)


def _window_before_new(items, log):
    assert "m-new" not in _ids(items) and "m-old" in _ids(items)


def _legacy_standalone(items, log):
    legacy = [i for i in items if i["memory_type"] == "fact"]
    assert [i["content"] for i in legacy] == [FACTS[-1][2]]
    assert log.count("carry no source_item") == 1 and "1 fact row" in log


def _cli_layout(output, log):
    lines = output.splitlines()
    assert sum(line.lstrip().startswith("[") and "m-680" in line for line in lines) == 1
    assert any(
        line.startswith("    [fact] minimum credit score cutoff") for line in lines
    )
    assert any("m-appr" in line for line in lines)


def _ask_extractive(evidence, log):
    ids = [e["item_id"] for e in evidence]
    assert len(ids) == len(set(ids)) and {"m-680", "m-appr"} <= set(ids)
    appr = next(e for e in evidence if e["item_id"] == "m-appr")
    assert APPROVAL in _fact_texts(appr) and "facts" not in appr
    assert appr["collapse"]["promoted"] is True


def _ask_llm_prompt(prompt, log):
    assert prompt.count("[m-680]") == 1 and "[fact::" not in prompt
    assert f"[m-appr] {MEMORIES['m-appr'][0]}\n  - fact: {APPROVAL}" in prompt


def _out_of_window_fact_admits_nothing(items, log):
    # the fact is outside the window and its parent is readable: the parent must still stay out
    assert _ids(items) == ["m-in"]


# Rows as core would hand them over: an out-of-window fact of m-out, an in-window memory.
WINDOW_POOL = [
    {
        "item_id": "fact::a",
        "memory_type": "fact",
        "content": "f",
        "metadata": {"source_item": "m-out", "created_at": "2026-08-01T00:00:00+00:00"},
    },
    {
        "item_id": "m-in",
        "memory_type": "episodic",
        "content": "in",
        "metadata": {"created_at": "2026-10-01T00:00:00+00:00"},
    },
]


# CLI-2.0-SEARCH-FACT-COLLAPSE-1: a 2.0 edge can have several producers (G22 `source_items`, writers
# append). Core sets `source_item` to the list's last entry (the most recent producer).
def _multi_fact(source_items, source_item):
    meta = {"source_item": source_item}
    if source_items is not None:
        meta["source_items"] = source_items
    return {
        "item_id": "fact::m",
        "memory_type": "fact",
        "content": "shared fact",
        "metadata": meta,
    }


def _multi_memory(item_id):
    return {"item_id": item_id, "memory_type": "episodic", "content": f"text {item_id}"}


def _children_by_parent(items, log):
    return [
        (i["item_id"], _fact_texts(i), i.get("collapse", {}).get("promoted"))
        for i in items
    ]


def _first_producer_in_pool(items, log):
    # m-y is listed before m-z and is in the pool: the fact joins it, not the most recent m-z
    assert _children_by_parent(items, log) == [
        ("m-y", ["shared fact"], False),
        ("m-z", [], None),
    ]


def _none_in_pool_most_recent(items, log):
    # no producer in the pool: the most recent (source_item = last entry) is promoted
    assert _children_by_parent(items, log) == [
        ("m-y", ["shared fact"], True),
        ("m-q", [], None),
    ]


def _single_source_item_as_on_1_5(items, log):
    # a 1.x edge (scalar only) groups under its source_item even when another memory is in the pool
    assert _children_by_parent(items, log) == [
        ("m-a", ["shared fact"], True),
        ("m-b", [], None),
    ]


VENDOR = "who owns the vendor risk register"
VENDOR_FACT = "Kofi Mensah owns the vendor risk register"


def _seeded_multi_source(items, log):
    # neither producer's text matches: the fact promotes the most recent producer (last in the stored
    # list), nested once; the earlier producer stays out
    ids = _ids(items)
    assert (
        "m-v2" in ids
        and "m-v1" not in ids
        and not any(i.startswith("fact::") for i in ids)
    ), ids
    v2 = next(i for i in items if i["item_id"] == "m-v2")
    assert (_fact_texts(v2), v2["collapse"]["promoted"]) == ([VENDOR_FACT], True)


# (label, via, request, check)
ROWS = [
    (
        "an out-of-window fact does not admit its parent",
        "collapse",
        {"since": "2026-09-01T00:00:00+00:00"},
        _out_of_window_fact_admits_nothing,
    ),
    (
        "collapse none keeps today's rows",
        "search",
        {"query": CUTOFF, "top_k": 5, "collapse": "none"},
        _crowded_today,
    ),
    (
        "top-5 is one row per source memory",
        "search",
        {"query": CUTOFF, "top_k": 5},
        _one_row_per_memory,
    ),
    (
        "parent rank = best child rank",
        "search",
        {"query": CUTOFF, "top_k": 5},
        _parent_rank_is_best_child,
    ),
    (
        "since drops the out-of-window parent",
        "search",
        {"query": LEDGER, "top_k": 5, "since": "2026-09-01T00:00:00+00:00"},
        _window_after_old,
    ),
    (
        "until drops the out-of-window parent",
        "search",
        {"query": LEDGER, "top_k": 5, "until": "2026-09-01T00:00:00+00:00"},
        _window_before_new,
    ),
    (
        "no source_item stays standalone, one WARNING",
        "search",
        {"query": LEDGER, "top_k": 5},
        _legacy_standalone,
    ),
    ("sm search prints each memory once, facts indented", "cli", [CUTOFF], _cli_layout),
    (
        "extractive ask: one evidence item per memory",
        "ask",
        {"question": CUTOFF},
        _ask_extractive,
    ),
    (
        "LLM ask: one prompt entry per memory, facts nested",
        "ask-llm",
        {"question": CUTOFF},
        _ask_llm_prompt,
    ),
    (
        "multi-source fact: parent = first source_items entry already in the pool",
        "multi",
        [
            _multi_fact(["m-x", "m-y", "m-z"], "m-z"),
            _multi_memory("m-z"),
            _multi_memory("m-y"),
        ],
        _first_producer_in_pool,
    ),
    (
        "multi-source fact: none in the pool -> the most recent producer",
        "multi",
        [_multi_fact(["m-x", "m-y"], "m-y"), _multi_memory("m-q")],
        _none_in_pool_most_recent,
    ),
    (
        "single source_item (1.x edge): grouped exactly as on 1.5",
        "multi",
        [_multi_fact(None, "m-a"), _multi_memory("m-b")],
        _single_source_item_as_on_1_5,
    ),
    # Last: it seeds more rows into the shared store.
    (
        "seeded source_items edge on Lite: none in the pool -> the most recent producer",
        "search-seeded",
        {"query": VENDOR, "top_k": 5},
        _seeded_multi_source,
    ),
]


@pytest.fixture()
def lite_store(tmp_path, monkeypatch):
    from smartmemory.plugins.embedding import create_embeddings
    from smartmemory.tools.factory import create_lite_memory

    for key in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    memory = create_lite_memory(
        str(tmp_path / "test_collapse_store"), spawn_worker=False
    )
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    monkeypatch.setattr(local_api, "get_memory", lambda: memory)
    backend = memory._graph.backend
    for entity in ("Dana", "score", "Priya", "billing"):
        backend.add_node(
            entity,
            {"content": entity, "memory_type": "concept", "origin": "user:fixture"},
        )
    for item_id, (content, stamp) in MEMORIES.items():
        props = {
            "content": content,
            "memory_type": "episodic",
            "origin": "user:fixture",
        }
        if stamp:
            props.update(created_at=stamp, transaction_time=stamp)
        backend.add_node(item_id, props)
    for edge_type, source, fact in FACTS:
        props = {
            "fact": fact,
            "fact_embedding": [float(x) for x in create_embeddings(fact)],
            "origin": "enricher:llm_extract",
        }
        if source:
            props["source_item"] = source
        ends = ("Dana", "score") if "cutoff" in fact else ("Priya", "billing")
        backend.add_edge(*ends, edge_type, props)
    with TestClient(local_api.api) as client:
        try:
            yield client
        finally:
            memory.close()


def test_search_and_ask_rank_source_memories_not_rows(lite_store, monkeypatch, caplog):
    import smartmemory.utils.llm as core_llm

    prompts = []
    monkeypatch.setattr(
        core_llm,
        "call_llm",
        lambda **kw: prompts.append(kw["user_content"])
        or ("m", "ANSWER: Dana Ortiz\nREASONING: r"),
    )

    def cli_request(method, path, **kwargs):
        response = lite_store.request(method, path.removeprefix("/memory"), **kwargs)
        response.raise_for_status()
        return response.json()

    def run(via, request):
        if via == "search":
            return lite_store.post("/search", json=request).json()["items"]
        if via == "collapse":
            from smartmemory_app.search_collapse import collapse_to_parents

            readable = {
                "item_id": "m-out",
                "content": "out",
                "created_at": "2026-08-01T00:00:00+00:00",
            }
            return collapse_to_parents(WINDOW_POOL, 5, lambda _id: readable, **request)
        if via == "multi":
            from smartmemory_app.search_collapse import collapse_to_parents

            return collapse_to_parents(request, 5, _multi_memory)
        if via == "search-seeded":
            from smartmemory.plugins.embedding import create_embeddings

            backend = storage.get_memory()._graph.backend
            # Raw producers, never matched on their own text. A real save would hide the fact until its
            # extraction finishes (2.0 pending_facade), and this fixture runs no worker.
            for item_id, text in (
                ("m-v1", "Archive note three."),
                ("m-v2", "Lunch order for Friday."),
            ):
                backend.add_node(
                    item_id,
                    {
                        "content": text,
                        "memory_type": "episodic",
                        "origin": "user:fixture",
                    },
                )
            for entity in ("Kofi", "register"):
                backend.add_node(
                    entity,
                    {
                        "content": entity,
                        "memory_type": "concept",
                        "origin": "user:fixture",
                    },
                )
            backend.add_edge(
                "Kofi",
                "register",
                "OWNS",
                {
                    "fact": VENDOR_FACT,
                    "fact_embedding": [
                        float(x) for x in create_embeddings(VENDOR_FACT)
                    ],
                    "origin": "enricher:llm_extract",
                    "source_items": ["m-v1", "m-v2"],
                },
            )
            return lite_store.post("/search", json=request).json()["items"]
        if via == "cli":
            monkeypatch.setattr(cli_module, "_memory_request", cli_request)
            result = CliRunner().invoke(cli_module.cli, ["search", *request])
            assert result.exit_code == 0, result.output
            return result.output
        if via == "ask":
            body = lite_store.post("/ask", json=request)
            assert body.status_code == 200, body.text
            return body.json()["evidence"]
        # The LLM path: a configured provider with a key present, the model call captured.
        monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "bogus-not-a-real-key")
        monkeypatch.setattr(local_api, "llm_key_present", lambda: True)
        monkeypatch.setattr(local_api, "_ask_relations", lambda hits: [])
        prompts.clear()
        body = lite_store.post("/ask", json=request)
        assert body.status_code == 200, body.text
        return prompts[0]

    failures = []
    for label, via, request, check in ROWS:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            try:
                output = run(via, request)
                check(output, caplog.text)
            except AssertionError as exc:
                failures.append((label, str(exc)[:300]))
    assert not failures, failures
    assert local_api.AskRequest(question="q").limit == 8


def _pool_row(item_id, source=None, score=0.5):
    meta = {"source_item": source} if source else {}
    kind = "fact" if source else "episodic"
    return {
        "item_id": item_id,
        "memory_type": kind,
        "content": f"text {item_id}",
        "score": score,
        "metadata": meta,
    }


# Pre-collapse pool, rank = 1-based position. m-a is in the pool with 4 facts (cap 3, total 4),
# m-b is promoted by one fact, m-c is unreadable, m-d matches with no facts.
SHAPE_POOL = [
    _pool_row("fact::a1", "m-a", 0.9),
    _pool_row("fact::a2", "m-a", 0.8),
    _pool_row("m-a", None, 0.7),
    _pool_row("fact::b1", "m-b", 0.6),
    _pool_row("fact::a3", "m-a", 0.5),
    _pool_row("fact::c1", "m-c", 0.4),
    _pool_row("fact::a4", "m-a", 0.3),
    _pool_row("m-d", None, 0.2),
]
NO_FLAGS = {"parent_matched": False, "promoted": False, "parent_unresolved": False}


def test_collapsed_rows_carry_children_and_collapse_blocks():
    """F3 of CORE-DOC-SUMMARY-POINTER-1: `children` and `collapse` are the public JSON names."""

    def fetch(item_id):
        return (
            {}
            if item_id == "m-c"
            else _pool_row(item_id) | {"content": f"read {item_id}"}
        )

    plain = collapse_to_parents(SHAPE_POOL, 10, fetch)
    collapsed = {r["item_id"]: r for r in plain}
    rows = [
        (
            "children shape, best first, rank is the pre-collapse pool position",
            [
                (c["item_id"], c["memory_type"], c["score"], c["rank"], c["kind"])
                for c in collapsed["m-a"]["children"]
            ],
            [
                ("fact::a1", "fact", 0.9, 1, "fact"),
                ("fact::a2", "fact", 0.8, 2, "fact"),
                ("fact::a3", "fact", 0.5, 5, "fact"),
            ],
        ),
        (
            "child content is the fact text",
            collapsed["m-a"]["children"][0]["content"],
            "text fact::a1",
        ),
        (
            "cap of 3 with children_total 4, parent was in the pool",
            collapsed["m-a"]["collapse"],
            {**NO_FLAGS, "parent_matched": True, "children_total": 4, "kind": "fact"},
        ),
        (
            "promoted: the parent was added only because a fact matched",
            collapsed["m-b"]["collapse"],
            {**NO_FLAGS, "promoted": True, "children_total": 1, "kind": "fact"},
        ),
        (
            "parent_unresolved: the best child stands for the group",
            (collapsed["fact::c1"]["item_id"], collapsed["fact::c1"]["collapse"]),
            (
                "fact::c1",
                {
                    **NO_FLAGS,
                    "parent_unresolved": True,
                    "children_total": 1,
                    "kind": "fact",
                },
            ),
        ),
        (
            "a memory with no matching fact keeps its row unchanged",
            collapsed["m-d"],
            SHAPE_POOL[-1],
        ),
        (
            "no `facts` or `standalone` key in any collapsed row",
            [k for r in plain for k in r if k in ("facts", "standalone")]
            + [k for r in plain for k in r.get("collapse", {}) if k == "standalone"],
            [],
        ),
    ]
    failures = [(label, got, want) for label, got, want in rows if got != want]
    assert not failures, failures
