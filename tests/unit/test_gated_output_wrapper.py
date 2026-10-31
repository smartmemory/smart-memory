"""U5.21 wrapper search/recall consume core's gated output: a partial answer is not an exhausted one. # trace G:G44,G132,G149

Bug classes:
* C3 a short page is read as "nothing more". Core's scan budget can end a lane early and says so on
  ``SearchResults.diagnostics.coverage``. ``storage.search`` (filtered widening) and ``storage.recall`` (query, workspace
  and orient arms) keep widening only to the bound core can examine, then stop and SAY the answer is partial: the text
  or list they return carries ``.partial`` (``PartialText`` / ``PartialRows``) so no consumer reads it as "nothing".
* C4 every fetch is bounded BEFORE the call, the first one included, and rows a callee returns past the bound are
  not used. The bound is the smaller of core's gate budget and the intake port's page limit: the real core refuses a
  wider gated fetch, which a finite fake core never showed.
* C1 a refusal is logged once, by its origin. "Already logged" is read from the exception through
  ``smartmemory.refusal_logging`` only.

Every case runs the real core: a real lite SQLite store, the real pending read port and the real ``LaneReadGate`` (only
its ``max_scan`` is lowered). The only stand-ins are named where they appear: a recorder around the real ``search``,
a fault injected at the snapshot read, and a callee that over-returns (real core cannot be made to break its own bound).

WARNING lists are asserted EXACTLY as ordered (logger, level, message) tuples across EVERY logger (fix round 2, F4).
Rows whose callee is a stand-in assert the complete list (``full_log``). Rows on the real store assert the complete list
minus ``NOISE``: an explicit, closed list of (logger, message prefix) pairs the real facade logs for its environment, not
for this unit. Measured by ``u-U5.21/fix2/noise_probe.py``: 31-191 records per call, some of them non-deterministic (the
reranker's background load, a once-per-process Redis notice, the read-runner OFF notice). Core's own ``lane_gate`` budget
line is listed too: its exact text is pinned by core's ``test_search_gate_partial_coverage``. A record from any other
logger, or with any other text, fails the row.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from smartmemory.refusal_logging import mark_refusal_logged
from smartmemory.save.read_port import InMemoryPendingStore
from smartmemory.search import facade_readers
from smartmemory.search.lane_gate import LaneReadGate
from smartmemory.search.pending_facade import PendingFacadeUnavailable
from smartmemory.search.results import (
    PartialRows,
    PartialText,
    SearchDiagnostics,
    SearchResults,
)
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import recall_format, storage

GATE_LOG = "smartmemory.search.visibility_gate"
STORAGE_LOG = "smartmemory_app.storage"
FORMAT_LOG = "smartmemory_app.recall_format"
#: (logger, message prefix) the real facade logs for its environment on every call, never for this unit. Closed list.
NOISE = (
    ("smartmemory.search.rerank", "Reranker "),
    ("smartmemory.search.rerank", "Reranking took "),
    ("smartmemory.redis_gate", "activation boost disabled"),
    ("smartmemory.redis_gate", "retrieval tracking disabled"),
    ("smartmemory.search.read_runner_adapter", "switch sm2_read_runner_default OFF"),
    (
        "smartmemory.memory.pipeline.stages.search",
        "SmartGraph returned no usable candidates",
    ),
    (
        "smartmemory.memory.pipeline.stages.search",
        "Manual similarity scan rejected item ",
    ),
    ("smartmemory.smart_memory", "Strength ranking SKIPPED"),
    ("smartmemory.smart_memory", "Search refill cap"),
    ("smartmemory.models.rerank", "rerank_unscored"),
    ("smartmemory.search.pending_facade", "Extraction status unavailable for"),
    ("smartmemory.search.lane_gate", "Read gate scan budget spent in lane "),
)
PARTIAL = ("complete", "read_gate_scan_budget_spent")
CAP = 200  # min(lane_gate.DEFAULT_MAX_SCAN, read_port.MAX_PAGE_LIMIT)
FETCH_BOUND = "fetch_bound"  # the client's own reason: it stopped at the widest fetch it may ask for


def gate_warning(where, names="complete, read_gate_scan_budget_spent"):
    return (
        GATE_LOG,
        "WARNING",
        f"{where} returned a partial answer under the read gate: core reported {names}, so visible matches past what "
        "it examined are not shown",
    )


class World:
    def __init__(self, mem):
        self.mem = mem
        self.calls = []  # (text, top_k) of every core search, recorded by a passthrough around the real one
        self.budget = {"max_scan": None}


@pytest.fixture
def world(tmp_path, monkeypatch, caplog):
    """Six semantic rows matching "atlas budget" (kind=x), the first five retired. Scan budget 1 means core examines a
    retired row, stops, and says the answer is partial."""
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    mem = create_lite_memory(data_dir=str(tmp_path), spawn_worker=False)
    w = World(mem)
    real_gate = facade_readers.FacadeReadGate.lane_gate

    def lane_gate(self):
        bound = real_gate(self)
        if w.budget["max_scan"] is None:
            return bound
        return LaneReadGate(bound.gate, bound.fetch, max_scan=w.budget["max_scan"])

    monkeypatch.setattr(facade_readers.FacadeReadGate, "lane_gate", lane_gate)
    real_search = mem.search

    def recorder(text, **kwargs):
        w.calls.append((text, kwargs.get("top_k")))
        return real_search(text, **kwargs)

    monkeypatch.setattr(mem, "search", recorder)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
    for n in range(6):
        mem._graph.backend.add_node(
            f"test_u521_{n}",
            {
                "content": f"atlas budget note {n}",
                "origin": "user:test",
                "retired": n != 5,
                "created_at": "2026-01-01T00:00:00+00:00",
                "kind": "x",
                "metadata": {"kind": "x"},
            },
            memory_type="semantic",
        )
    mem.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
    caplog.set_level(logging.WARNING)
    try:
        yield w
    finally:
        mem.close()


def full_log(caplog):
    """The ENTIRE WARNING-or-worse list, every logger, in order: for rows whose callee is a stand-in."""
    return [(r.name, r.levelname, r.getMessage()) for r in caplog.records]


def answer_log(caplog):
    """The entire list minus the closed ``NOISE`` list: for rows that run the real facade."""
    return [
        t
        for t in full_log(caplog)
        if not any(t[0] == name and t[2].startswith(prefix) for name, prefix in NOISE)
    ]


def recall(**kw):
    return storage.recall(
        top_k=3, include_snapshot=False, workspace_id="test", strict=False, **kw
    )


def ids_in(text):
    return [
        line.split("[mem:")[1].split("]")[0]
        for line in str(text).splitlines()
        if "[mem:" in line
    ]


# -- recall: exhaustion is decided from core's coverage, the bound is enforced before every fetch ---------------------

SHORTFALL_BOUND = (
    "recall shortfall: requested=3 emitted=%d; stopped widening at a fetch of 200 (core reported a partial answer), so "
    "eligible candidates past what core examined are not shown"
)
SHORTFALL_EXHAUSTED = (
    "recall shortfall: requested=3 emitted=%d; eligible distinct candidates exhausted"
)
DROPPED = [
    (FORMAT_LOG, "WARNING", f"recall dropped test_u521_{n}: top_k cap (3)")
    for n in (3, 4, 5)
]

# (case, arm kwargs, scan budget, the (text, fetch) pairs core was asked, ids emitted, carrier names, WARNINGs)
# The ON/partial rows widen 6/12/24/48/96/192 and stop at 200: never the 384/768/1536 the old fake core allowed. The
# complete rows stop on the first short page (exhaustion proved). The orient arm has no lexical lane, so a lane budget cannot make it partial.
Q = "atlas budget"
RECALL_ROWS = [
    (
        "query on partial",
        {"query": Q},
        1,
        [(Q, k) for k in (6, 12, 24, 48, 96, 192, 200)],
        [],
        (*PARTIAL, FETCH_BOUND),
        [gate_warning("recall search")] * 7
        + [(STORAGE_LOG, "WARNING", SHORTFALL_BOUND % 0)],
    ),
    (
        "query on complete",
        {"query": Q},
        None,
        [(Q, 6)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
    (
        "workspace on partial",
        {"cwd": "/project"},
        1,
        [(t, k) for k in (3, 6, 12, 24, 48, 96, 192, 200) for t in ("", "/project")],
        ["test_u521_5"],
        ("read_gate_scan_budget_spent", FETCH_BOUND),
        [gate_warning("recall workspace search", "read_gate_scan_budget_spent")] * 8
        + [(STORAGE_LOG, "WARNING", SHORTFALL_BOUND % 1)],
    ),
    (
        "workspace on complete",
        {"cwd": "/project"},
        None,
        [("", 3), ("/project", 3)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
    (
        "orient on",
        {},
        1,
        [("", 3)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
]


@pytest.mark.parametrize(
    "case,arm,budget,asked,ids,names,warnings",
    RECALL_ROWS,
    ids=[r[0] for r in RECALL_ROWS],
)
def test_recall_widens_to_the_bound_and_says_when_the_answer_is_partial(
    world, caplog, case, arm, budget, asked, ids, names, warnings
):
    world.budget["max_scan"] = budget
    out = recall(**arm)
    assert world.calls == asked
    assert max(k for _, k in world.calls) <= CAP
    assert ids_in(out) == ids
    assert getattr(out, "partial", ()) == names
    assert type(out) is (PartialText if names else str)
    assert answer_log(caplog) == warnings


def test_a_partial_empty_recall_is_not_the_empty_string(world):
    """The F1 class on the text channel: '' to every str reader, but the carrier says core's answer was partial."""
    world.budget["max_scan"] = 1
    out = recall(query=Q)
    assert out == "" and out.partial == (*PARTIAL, FETCH_BOUND)


def test_a_huge_caller_count_is_rendered_bounded_and_the_refusal_still_logs(
    world, caplog
):
    """Caller values are never formatted unbounded: a 5000-digit top_k under the 4300-digit int limit still answers,
    clamps the fetch to the bound, and logs the exact WARNING with a placeholder where the digits would be."""
    world.budget["max_scan"] = 1
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        out = storage.recall(
            query=Q,
            top_k=10**5000,
            include_snapshot=False,
            workspace_id="test",
            strict=False,
        )
    finally:
        sys.set_int_max_str_digits(old)
    assert max(k for _, k in world.calls) == CAP
    assert out.partial == (*PARTIAL, FETCH_BOUND)
    assert answer_log(caplog) == [gate_warning("recall search")] * len(world.calls) + [
        (
            STORAGE_LOG,
            "WARNING",
            "recall shortfall: requested=<an integer of 16610 bits> emitted=0; stopped widening at a fetch of 200 "
            "(core reported a partial answer), so eligible candidates past what core examined are not shown",
        )
    ]


# -- recall: a stop at the bound is carried whatever caused it (fix round 2, F1) -------------------------------------
SHORTFALL_FULL = (
    "recall shortfall: requested=3 emitted=%d; stopped widening at a fetch of 200 (the page came back full), so "
    "eligible candidates past what core examined are not shown"
)


@pytest.fixture
def capped(tmp_path, monkeypatch, caplog):
    """Review r2 F1 on the real store: 201 visible semantic rows, newest first by one second each. The newest 200 sit
    below the recall confidence floor; the oldest (``test_cap_200``) is eligible but past the 200-row bound."""
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    mem = create_lite_memory(
        data_dir=str(tmp_path / "test_u521_cap"), spawn_worker=False
    )
    calls = []
    real_search = mem.search

    def recorder(text, **kwargs):
        calls.append(kwargs.get("top_k"))
        return real_search(text, **kwargs)

    monkeypatch.setattr(mem, "search", recorder)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(201):
        mem._graph.backend.add_node(
            f"test_cap_{n:03d}",
            {
                "content": f"atlas budget {n}",
                "origin": "user:test",
                "created_at": (base + timedelta(seconds=200 - n)).isoformat(),
                "confidence": 1.0 if n == 200 else 0.01,
            },
            memory_type="semantic",
        )
    mem.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
    caplog.set_level(logging.WARNING)
    try:
        yield mem, calls
    finally:
        mem.close()


def floor_drops(fetches, eligible=()):
    """The wrapper's exact per-row floor WARNINGs: each fetch re-reads its window, newest first."""
    return [
        (
            STORAGE_LOG,
            "WARNING",
            f"recall dropped test_cap_{n:03d}: confidence below floor 0.3",
        )
        for k in fetches
        for n in range(min(k, 201))
        if n != 200 and n not in eligible
    ]


ON_FETCHES = (3, 6, 12, 24, 48, 96, 192, 200)

# (case, the newest row made eligible or None, fetches, ids emitted, names carried, WARNINGs minus NOISE).
# ON stops at the bound on a FULL page (core's answer complete) and the answer carries ``fetch_bound``, empty or not.
CAP_ROWS = [
    (
        "on empty carries fetch_bound",
        None,
        ON_FETCHES,
        [],
        (FETCH_BOUND,),
        [*floor_drops(ON_FETCHES), (STORAGE_LOG, "WARNING", SHORTFALL_FULL % 0)],
    ),
    (
        "on non-empty carries fetch_bound",
        0,
        ON_FETCHES,
        ["test_cap_000"],
        (FETCH_BOUND,),
        [
            *floor_drops(ON_FETCHES, eligible=(0,)),
            (STORAGE_LOG, "WARNING", SHORTFALL_FULL % 1),
        ],
    ),
]


@pytest.mark.parametrize(
    "case,eligible,fetches,ids,names,warnings",
    CAP_ROWS,
    ids=[r[0] for r in CAP_ROWS],
)
def test_a_stop_at_the_bound_on_a_full_page_is_carried(
    capped, caplog, case, eligible, fetches, ids, names, warnings
):
    mem, calls = capped
    if eligible is not None:
        mem._graph.backend.set_properties(
            f"test_cap_{eligible:03d}", {"confidence": 1.0}
        )
    out = storage.recall(
        top_k=3, include_snapshot=False, workspace_id="test", strict=False
    )
    assert calls == list(fetches)
    assert ids_in(out) == ids
    assert getattr(out, "partial", ()) == names
    assert type(out) is (PartialText if names else str)
    assert answer_log(caplog) == warnings


class OverReturning:
    """A callee that ignores the fetch it was given. Real core cannot be made to break its own bound, so this is the one
    stand-in for the over-return rows: it returns three times the rows asked for. Its rows carry a user origin and a
    write date, so the formatter's own origin and freshness lines stay out of these rows' logs."""

    def __init__(self):
        self.calls = []
        self._graph = SimpleNamespace(search_nodes=lambda query: [])

    def search(self, text, *, top_k, **kwargs):
        self.calls.append((text, top_k))
        rows = [
            SimpleNamespace(
                item_id=f"o{n}",
                content=f"o{n}",
                memory_type="semantic",
                metadata={},
                confidence=1.0,
                reference=False,
                origin="user:test",
                created_at="2026-01-01T00:00:00+00:00",
            )
            for n in range(top_k * 3)
        ]
        return SearchResults(rows, SearchDiagnostics(coverage={"complete": True}))


class PartialCallee(OverReturning):
    """The wildcard "*" never spends the lexical scan budget on real core (it bypasses the scored lane), so a partial
    wildcard answer cannot be made to happen with a budget. This stand-in is the only way to hand the wrapper's widening
    loop core's own partial coverage on that path: one row inside the window, ``complete`` False."""

    def search(self, text, *, top_k, **kwargs):
        self.calls.append((text, top_k))
        row = SimpleNamespace(
            item_id="p0",
            content="p0",
            memory_type="semantic",
            metadata={},
            confidence=1.0,
            reference=False,
        )
        return SearchResults([row], SearchDiagnostics(coverage={"complete": False}))


FILTER_WARNING = (
    "local filtered search stopped at a fetch of %d: %d of %d wanted matched and candidates past that window were not "
    "examined"
)


class OverDicts(OverReturning):
    """The over-returning stand-in in the dictionary shape ``storage.search`` normalizes (review r2's ``Over``): three
    times the rows asked for, and only the LAST one carries ``marker=yes``, so a match is only ever past the fetch."""

    def search(self, text, *, top_k, **kwargs):
        self.calls.append((text, top_k))
        rows = [
            {
                "item_id": f"test_over_{n}",
                "content": "atlas",
                "memory_type": "semantic",
                "metadata": {"marker": "yes" if n == top_k * 3 - 1 else "no"},
            }
            for n in range(top_k * 3)
        ]
        return SearchResults(rows, SearchDiagnostics(coverage={"complete": True}))


def over(where, count, fetch):
    """Core's one fetch-bound WARNING (``visibility_gate.gated_fetch_bound``), the same text at every client site."""
    return (
        GATE_LOG,
        "WARNING",
        f"{where} returned {count} rows for a fetch of {fetch}: the rows past the fetch were never asked for and are "
        "not used",
    )


def cap_drops(ids):
    return [(FORMAT_LOG, "WARNING", f"recall dropped {i}: top_k cap (3)") for i in ids]


def dup_drops(ids):
    """The workspace arm's recency and semantic channels return the same stand-in rows: the second copy is dropped."""
    return [
        (FORMAT_LOG, "WARNING", f"recall dropped {i}: duplicate item_id") for i in ids
    ]


def o(*ns):
    return [f"o{n}" for n in ns]


def _recall_ids(**arm):
    return ids_in(recall(**arm))


def _search_ids(**kw):
    got = storage.search("atlas", **kw)
    return [r["item_id"] for r in got], getattr(got, "partial", ())


# The F2 class (C4 on an over-returning callee): every client fetch consumes at most what it asked for. One row per
# fetch site in the wrapper: recall query / workspace (recency + semantic) / orient, the orient snapshot lookup, the
# unfiltered search and the filtered widening. (case, callee, call, answer, the COMPLETE WARNING list). ON: rows
# past the fetch are never used and core's one WARNING names the site.
OVER_ROWS = [
    (
        "recall query on",
        OverReturning,
        lambda: _recall_ids(query=Q),
        o(0, 1, 2),
        [over("recall search", 18, 6), *cap_drops(o(3, 4, 5))],
    ),
    (
        "recall workspace on",
        OverReturning,
        lambda: _recall_ids(cwd="/project"),
        o(0, 1, 2),
        [
            over("recall recency search", 9, 3),
            over("recall workspace search", 9, 3),
            *dup_drops(o(0, 1, 2)),
        ],
    ),
    (
        "orient on",
        OverReturning,
        lambda: _recall_ids(),
        o(0, 1, 2),
        [over("orient recency search", 9, 3)],
    ),
    (
        "orient snapshot lookup on",
        OverReturning,
        lambda: ids_in(
            storage.recall(
                top_k=3, include_snapshot=True, workspace_id="test", strict=False
            )
        ),
        o(0, 1, 2),
        [over("orient snapshot lookup", 3, 1), over("orient recency search", 9, 3)],
    ),
    (
        "unfiltered search on",
        OverDicts,
        lambda: _search_ids(top_k=2),
        (["test_over_0", "test_over_1"], ()),
        [over("local search", 6, 2)],
    ),
    # review r2 F2: a fetch of 5 used the 15th row. ON: 5/20/50 are each cut, nothing past a fetch matches, the stop at
    # the loop's widest fetch is said and carried.
    (
        "filtered search on",
        OverDicts,
        lambda: _search_ids(top_k=1, filters={"marker": "yes"}),
        ([], (FETCH_BOUND,)),
        [
            over("local filtered search", 15, 5),
            over("local filtered search", 60, 20),
            over("local filtered search", 150, 50),
            (STORAGE_LOG, "WARNING", FILTER_WARNING % (50, 0, 1)),
        ],
    ),
]


@pytest.mark.parametrize(
    "case,callee,call,answer,warnings", OVER_ROWS, ids=[r[0] for r in OVER_ROWS]
)
def test_no_client_fetch_uses_a_row_past_what_it_asked_for(
    monkeypatch, caplog, case, callee, call, answer, warnings
):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    stand_in = callee()
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: stand_in)
    caplog.set_level(logging.WARNING)
    got = call()
    assert got == answer
    assert full_log(caplog) == warnings


# -- search: filtered widening ---------------------------------------------------------------------------------------

# (case, scan budget, top_k, the fetches core was asked, ids returned, carrier names, WARNINGs). Five of the
# six matching rows are retired. A partial core is widened 10/40/100 (50x top_k is the loop's own ceiling) and the
# stop is said. top_k=30 shows the bound: 150/600/1500 becomes 150/200, never a fetch past what core can examine.
FILTER_ROWS = [
    (
        "on partial widens then says so",
        1,
        2,
        [10, 40, 100],
        [],
        (*PARTIAL, "fetch_bound"),
        [gate_warning("local filtered search")] * 3
        + [(STORAGE_LOG, "WARNING", FILTER_WARNING % (100, 0, 2))],
    ),
    (
        "on partial is clamped to the bound",
        1,
        30,
        [150, 200],
        [],
        (*PARTIAL, "fetch_bound"),
        [gate_warning("local filtered search")] * 2
        + [(STORAGE_LOG, "WARNING", FILTER_WARNING % (200, 0, 30))],
    ),
    ("on complete stops after one fetch", None, 2, [10], ["test_u521_5"], (), []),
]


@pytest.mark.parametrize(
    "case,budget,top_k,fetches,ids,names,warnings",
    FILTER_ROWS,
    ids=[r[0] for r in FILTER_ROWS],
)
def test_filtered_search_widens_to_the_bound_and_carries_the_partial_answer(
    world, caplog, case, budget, top_k, fetches, ids, names, warnings
):
    world.budget["max_scan"] = budget
    got = storage.search(Q, top_k=top_k, filters={"kind": "x"})
    assert [k for _, k in world.calls] == fetches
    # ids compared sorted: the order of the rows is the reranker's, which this unit does not touch
    assert sorted(r["item_id"] for r in got) == sorted(ids)
    assert getattr(got, "partial", ()) == names
    assert type(got) is (PartialRows if names else list)
    assert answer_log(caplog) == warnings


UNFILTERED_ROWS = [
    ("on partial", 1, [], PARTIAL, [gate_warning("local search")]),
    ("on complete", None, ["test_u521_5"], (), []),
]


@pytest.mark.parametrize(
    "case,budget,ids,names,warnings",
    UNFILTERED_ROWS,
    ids=[r[0] for r in UNFILTERED_ROWS],
)
def test_unfiltered_search_carries_the_partial_answer_on_the_plain_list(
    world, caplog, case, budget, ids, names, warnings
):
    world.budget["max_scan"] = budget
    got = storage.search(Q, top_k=10)
    assert [k for _, k in world.calls] == [10]
    assert sorted(r["item_id"] for r in got) == sorted(ids)
    assert getattr(got, "partial", ()) == names and type(got) is (
        PartialRows if names else list
    )
    assert answer_log(caplog) == warnings


def test_wildcard_complete_page_is_exhaustion_and_a_partial_one_is_refused_at_the_cap(
    world, caplog, monkeypatch
):
    """The wildcard resolves core's partial answer itself (widen, else a typed refusal), so its result needs no carrier."""
    got = storage.search("*")
    assert [r["item_id"] for r in got] == ["test_u521_5"] and type(got) is list
    assert answer_log(caplog) == []
    caplog.clear()
    callee = PartialCallee()
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: callee)
    monkeypatch.setattr(storage, "_WILDCARD_CORE_CAP", 400)
    with pytest.raises(storage.WildcardEnumerationOverflowError):
        storage.search("*")
    assert [k for _, k in callee.calls] == [200, 400]
    # core's coverage WARNING (visibility_gate) once per widened window (200, 400), then the wrapper's refusal
    assert full_log(caplog) == [
        (
            GATE_LOG,
            "WARNING",
            "wildcard search returned a partial answer under the read gate: core reported complete, so visible "
            "matches past what it examined are not shown",
        )
    ] * 2 + [
        (
            STORAGE_LOG,
            "WARNING",
            "wildcard search refused: core reported a partial answer at the cap of 400 hits, so the listing would be "
            "incomplete",
        )
    ]


def test_the_wildcard_refuses_a_callee_that_over_returns(monkeypatch, caplog):
    callee = OverReturning()
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: callee)
    caplog.set_level(logging.WARNING)
    with pytest.raises(storage.WildcardEnumerationOverflowError):
        storage.search("*")
    assert full_log(caplog) == [
        (
            STORAGE_LOG,
            "WARNING",
            "wildcard search refused: core returned 600 hits for a window of 200, so the listing bound is broken",
        )
    ]


def test_the_wrapper_drops_read_gate_and_context_kwargs(world):
    """G149: the context graph is not a client surface. A caller cannot pass it, so core never sees it."""
    seen = {}
    real = world.mem.search

    def spy(text, **kwargs):
        seen.update(kwargs)
        return real(text, **kwargs)

    world.mem.search = spy
    storage.search(Q, top_k=2, read_gate=object(), include_context=True, history=True)
    assert seen and not ({"read_gate", "include_context", "history"} & set(seen))


# -- the two optional-context degrade sites: a refusal is logged once, by its origin -------------------------------


def _marked():
    return mark_refusal_logged(PendingFacadeUnavailable("no intake port"))


def _unmarked():
    return PendingFacadeUnavailable("no intake port")


ORIGIN = (GATE_LOG, "WARNING", "origin: intake port unavailable")
SNAPSHOT_LOST = (FORMAT_LOG, "WARNING", "Orient lost snapshot context: no intake port")
DECISION_LOST = (FORMAT_LOG, "WARNING", "Recall lost decision lookup: no intake port")

SHORT_1 = (STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)

# The C1 and C8 classes on the two optional-context degrade arms (hooks must degrade, so they are carried, not raised).
# (case, site, exception, whether the origin logged it, names the answer carries, the WARNING list minus NOISE).
# ON: a marked refusal adds nothing to what its origin wrote, an unmarked failure is logged here, and the answer CARRIES
# what was lost (fix round 2, decision 2): a lost snapshot frame or lost decisions are never read as "none".
DEGRADE_ROWS = [
    (
        "snapshot on marked",
        "snapshot",
        _marked,
        True,
        ("snapshot_lookup_failed",),
        [ORIGIN, SHORT_1],
    ),
    (
        "snapshot on unmarked",
        "snapshot",
        _unmarked,
        False,
        ("snapshot_lookup_failed",),
        [SNAPSHOT_LOST, SHORT_1],
    ),
    (
        "decision on marked",
        "decision",
        _marked,
        True,
        ("decision_lookup_failed",),
        [ORIGIN, SHORT_1],
    ),
    (
        "decision on unmarked",
        "decision",
        _unmarked,
        False,
        ("decision_lookup_failed",),
        [DECISION_LOST, SHORT_1],
    ),
]


@pytest.mark.parametrize(
    "case,site,exc,origin,names,warnings",
    DEGRADE_ROWS,
    ids=[r[0] for r in DEGRADE_ROWS],
)
def test_a_degrade_arm_logs_a_refusal_once_and_the_answer_says_what_was_lost(
    world, caplog, case, site, exc, origin, names, warnings
):
    def raise_it():
        error = exc()
        if origin:
            logging.getLogger(GATE_LOG).warning("origin: intake port unavailable")
        raise error

    real = world.mem.search
    if site == "snapshot":

        def search(text, **kw):
            if kw.get("memory_type") == "snapshot":
                raise_it()
            return real(text, **kw)

        world.mem.search = search
    else:
        world.mem._graph.search_nodes = lambda query: raise_it()
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    caplog.clear()
    try:
        out = storage.recall(
            cwd="/project",
            top_k=3,
            workspace_id="test",
            strict=False,
            include_snapshot=site == "snapshot",
        )
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    assert getattr(out, "partial", ()) == names
    assert type(out) is (PartialText if names else str)
    assert answer_log(caplog) == warnings
    lost = (
        "Orient lost snapshot context"
        if site == "snapshot"
        else "Recall lost decision lookup"
    )
    assert trace["degradations"] == [f"{lost}: no intake port"]


# (case, error, WARNING list). The log is skipped for exactly one input: a marked refusal.
HOOK_ROWS = [
    (
        "on unmarked",
        lambda: RuntimeError("boom"),
        [(FORMAT_LOG, "WARNING", "Lost thing: boom")],
    ),
    ("on marked", lambda: mark_refusal_logged(RuntimeError("boom")), []),
]


@pytest.mark.parametrize("case,make,warnings", HOOK_ROWS, ids=[r[0] for r in HOOK_ROWS])
def test_record_hook_degradation_reads_the_mark_from_the_exception_only(
    caplog, case, make, warnings
):
    caplog.set_level(logging.WARNING)
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    try:
        recall_format.record_hook_degradation("Lost thing", make())
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    assert full_log(caplog) == warnings
    assert trace["degradations"] == ["Lost thing: boom"]


def test_record_hook_degradation_has_no_logged_parameter():
    """C1: the logged state travels on the exception, never on a boolean a caller can set."""
    import inspect

    assert list(
        inspect.signature(recall_format.record_hook_degradation).parameters
    ) == ["message", "error"]
    with pytest.raises(TypeError):
        recall_format.record_hook_degradation(
            "Lost thing", RuntimeError("boom"), logged=True
        )


# -- a real memory with no intake port: the typed refusal is not turned into [] ---------------------------------------


@pytest.fixture
def unbound(tmp_path, monkeypatch, caplog):
    mem = create_lite_memory(
        data_dir=str(tmp_path / "test_u521_wrapper"), spawn_worker=False
    )
    try:
        mem._graph.backend.add_node(
            "test_row",
            {
                "content": "atlas budget",
                "origin": "user:test",
                "created_at": "2026-10-11T12:00:00.000000+00:00",
            },
            memory_type="semantic",
        )
        monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
        caplog.set_level(logging.WARNING)
        yield mem
    finally:
        mem.close()


def test_a_gate_refusal_propagates_from_search_and_is_logged_once(unbound, caplog):
    """Gate ON, an admission door open (the Lite intake table exists) and only the empty default port bound
    (U20.BIND1: every host binds that port at construction, so a bare host no longer refuses): core refuses.
    The wrapper must not answer with an empty list."""
    backend = unbound._graph.backend
    with backend._lock:
        backend._conn.execute("CREATE TABLE sm2_intake (id TEXT)")
    with pytest.raises(PendingFacadeUnavailable):
        storage.search("atlas", top_k=3)
    core_logger = "smartmemory.search.pending_facade"
    # a refusal row: the list minus the closed NOISE list (the refusal now lands after the legacy-path switch
    # WARNING). The origin's line once, nothing from the wrapper.
    assert answer_log(caplog) == [
        (
            core_logger,
            "WARNING",
            "Pending facade read refused: the Lite intake table sm2_intake exists, so pending rows can be admitted, "
            "but the bound intake port is the empty construction default; bind the Lite intake store with "
            "bind_pending_reads",
        )
    ]


def test_the_bound_gate_serves_the_row_with_its_status_carrier(unbound):
    unbound.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
    got = storage.search("atlas", top_k=3)
    assert [r["item_id"] for r in got] == ["test_row"]
    assert got[0]["read_status"]["extraction_state"] == "unavailable"
