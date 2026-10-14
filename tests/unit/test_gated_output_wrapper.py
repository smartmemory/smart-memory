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
  ``smartmemory.refusal_logging`` only. With the switch OFF the legacy log is byte-for-byte the base's.

Every case runs the real core: a real lite SQLite store, the real pending read port and the real ``LaneReadGate`` (only
its ``max_scan`` is lowered). The only stand-ins are named where they appear: a recorder around the real ``search``,
a fault injected at the snapshot read, and a callee that over-returns (real core cannot be made to break its own bound).

WARNING lists are asserted EXACTLY as (logger, level, message), for the loggers this unit writes through. The real
facade also logs environment lines (reranker, Redis, SmartGraph fallback) and core's own ``lane_gate`` budget line, whose
exact text core's ``test_search_gate_partial_coverage`` pins, so those loggers are not part of this list.
"""

import logging
import sys
from types import SimpleNamespace

import pytest

from smartmemory.configuration.switch_testing import switch_override
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
from smartmemory.search.visibility_gate import SWITCH
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import recall_format, storage

GATE_LOG = "smartmemory.search.visibility_gate"
STORAGE_LOG = "smartmemory_app.storage"
FORMAT_LOG = "smartmemory_app.recall_format"
UNIT_LOGGERS = {GATE_LOG, STORAGE_LOG, FORMAT_LOG}
OFF_SWITCH_WARNING = (
    GATE_LOG,
    "WARNING",
    "switch sm2_search_visibility_gate OFF: search keeps only the legacy post-retrieval visibility filter; no unified "
    "read gate is bound",
)
PARTIAL = ("complete", "read_gate_scan_budget_spent")
CAP = 200  # min(lane_gate.DEFAULT_MAX_SCAN, read_port.MAX_PAGE_LIMIT)


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
    with switch_override(SWITCH, True):
        mem.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
    caplog.set_level(logging.WARNING)
    try:
        yield w
    finally:
        mem.close()


def unit_log(caplog):
    return [
        (r.name, r.levelname, r.getMessage())
        for r in caplog.records
        if r.name in UNIT_LOGGERS
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

# (case, arm kwargs, gate on, scan budget, the (text, fetch) pairs core was asked, ids emitted, carrier names, WARNINGs)
# The ON/partial rows widen 6/12/24/48/96/192 and stop at 200: never the 384/768/1536 the old fake core allowed. The
# complete rows stop on the first short page (exhaustion proved). The OFF rows are the legacy answer: the retired rows
# are visible, one fetch, no carrier. The orient arm has no lexical lane, so a lane budget cannot make it partial.
Q = "atlas budget"
RECALL_ROWS = [
    (
        "query on partial",
        {"query": Q},
        True,
        1,
        [(Q, k) for k in (6, 12, 24, 48, 96, 192, 200)],
        [],
        PARTIAL,
        [gate_warning("recall search")] * 7
        + [(STORAGE_LOG, "WARNING", SHORTFALL_BOUND % 0)],
    ),
    (
        "query on complete",
        {"query": Q},
        True,
        None,
        [(Q, 6)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
    (
        "query off",
        {"query": Q},
        False,
        1,
        [(Q, 6)],
        ["test_u521_0", "test_u521_1", "test_u521_2"],
        (),
        [OFF_SWITCH_WARNING, *DROPPED],
    ),
    (
        "workspace on partial",
        {"cwd": "/project"},
        True,
        1,
        [(t, k) for k in (3, 6, 12, 24, 48, 96, 192, 200) for t in ("", "/project")],
        ["test_u521_5"],
        ("read_gate_scan_budget_spent",),
        [gate_warning("recall workspace search", "read_gate_scan_budget_spent")] * 8
        + [(STORAGE_LOG, "WARNING", SHORTFALL_BOUND % 1)],
    ),
    (
        "workspace on complete",
        {"cwd": "/project"},
        True,
        None,
        [("", 3), ("/project", 3)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
    (
        "workspace off",
        {"cwd": "/project"},
        False,
        1,
        [("", 3), ("/project", 3)],
        ["test_u521_0", "test_u521_1", "test_u521_2"],
        (),
        [OFF_SWITCH_WARNING],
    ),
    (
        "orient on",
        {},
        True,
        1,
        [("", 3)],
        ["test_u521_5"],
        (),
        [(STORAGE_LOG, "WARNING", SHORTFALL_EXHAUSTED % 1)],
    ),
    (
        "orient off",
        {},
        False,
        1,
        [("", 3)],
        ["test_u521_0", "test_u521_1", "test_u521_2"],
        (),
        [OFF_SWITCH_WARNING],
    ),
]


@pytest.mark.parametrize(
    "case,arm,on,budget,asked,ids,names,warnings",
    RECALL_ROWS,
    ids=[r[0] for r in RECALL_ROWS],
)
def test_recall_widens_to_the_bound_and_says_when_the_answer_is_partial(
    world, caplog, case, arm, on, budget, asked, ids, names, warnings
):
    world.budget["max_scan"] = budget
    with switch_override(SWITCH, on):
        out = recall(**arm)
    assert world.calls == asked
    assert max(k for _, k in world.calls) <= (CAP if on else 10**9)
    assert ids_in(out) == ids
    assert getattr(out, "partial", ()) == names
    assert type(out) is (PartialText if names else str)
    assert unit_log(caplog) == warnings


def test_a_partial_empty_recall_is_not_the_empty_string(world):
    """The F1 class on the text channel: '' to every str reader, but the carrier says core's answer was partial."""
    world.budget["max_scan"] = 1
    with switch_override(SWITCH, True):
        out = recall(query=Q)
    assert out == "" and out.partial == PARTIAL


def test_a_huge_caller_count_is_rendered_bounded_and_the_refusal_still_logs(
    world, caplog
):
    """Caller values are never formatted unbounded: a 5000-digit top_k under the 4300-digit int limit still answers,
    clamps the fetch to the bound, and logs the exact WARNING with a placeholder where the digits would be."""
    world.budget["max_scan"] = 1
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with switch_override(SWITCH, True):
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
    assert out.partial == PARTIAL
    assert unit_log(caplog)[-1] == (
        STORAGE_LOG,
        "WARNING",
        "recall shortfall: requested=<an integer of 16610 bits> emitted=0; stopped widening at a fetch of 200 (core "
        "reported a partial answer), so eligible candidates past what core examined are not shown",
    )


class OverReturning:
    """A callee that ignores the fetch it was given. Real core cannot be made to break its own bound, so this is the one
    stand-in for the over-return rows: it returns three times the rows asked for."""

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


# (case, arm, gate on, ids emitted, WARNINGs). ON: rows past the fetch are not used and one WARNING says so. OFF: the
# legacy answer consumes what the callee returned.
OVER_ROWS = [
    (
        "query on",
        {"query": Q},
        True,
        ["o0", "o1", "o2"],
        [
            (
                STORAGE_LOG,
                "WARNING",
                "recall search returned 18 rows for a fetch of 6: the extra rows are not used, so the fetch bound holds",
            )
        ],
    ),
    (
        "workspace on",
        {"cwd": "/project"},
        True,
        ["o0", "o1", "o2"],
        [
            (
                STORAGE_LOG,
                "WARNING",
                "recall recency search returned 9 rows for a fetch of 3: the extra rows are not used, so the fetch bound holds",
            ),
            (
                STORAGE_LOG,
                "WARNING",
                "recall workspace search returned 9 rows for a fetch of 3: the extra rows are not used, so the fetch bound holds",
            ),
        ],
    ),
    ("query off", {"query": Q}, False, ["o0", "o1", "o2"], []),
]


@pytest.mark.parametrize(
    "case,arm,on,ids,warnings", OVER_ROWS, ids=[r[0] for r in OVER_ROWS]
)
def test_recall_does_not_use_rows_past_the_fetch_bound(
    monkeypatch, caplog, case, arm, on, ids, warnings
):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    callee = OverReturning()
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: callee)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        out = recall(**arm)
    assert ids_in(out) == ids
    assert [t for t in unit_log(caplog) if t[0] == STORAGE_LOG] == warnings


# -- search: filtered widening ---------------------------------------------------------------------------------------

FILTER_WARNING = (
    "local filtered search stopped at a fetch of %d: %d of %d wanted matched and candidates past that window were not "
    "examined"
)
# (case, gate on, scan budget, top_k, the fetches core was asked, ids returned, carrier names, WARNINGs). Five of the
# six matching rows are retired. A partial core is widened 10/40/100 (50x top_k is the loop's own ceiling) and the
# stop is said. top_k=30 shows the bound: 150/600/1500 becomes 150/200, never a fetch past what core can examine.
FILTER_ROWS = [
    (
        "on partial widens then says so",
        True,
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
        True,
        1,
        30,
        [150, 200],
        [],
        (*PARTIAL, "fetch_bound"),
        [gate_warning("local filtered search")] * 2
        + [(STORAGE_LOG, "WARNING", FILTER_WARNING % (200, 0, 30))],
    ),
    ("on complete stops after one fetch", True, None, 2, [10], ["test_u521_5"], (), []),
    (
        "off is the legacy loop and rows",
        False,
        1,
        10,
        [50],
        [f"test_u521_{n}" for n in range(6)],
        (),
        [OFF_SWITCH_WARNING],
    ),
]


@pytest.mark.parametrize(
    "case,on,budget,top_k,fetches,ids,names,warnings",
    FILTER_ROWS,
    ids=[r[0] for r in FILTER_ROWS],
)
def test_filtered_search_widens_to_the_bound_and_carries_the_partial_answer(
    world, caplog, case, on, budget, top_k, fetches, ids, names, warnings
):
    world.budget["max_scan"] = budget
    with switch_override(SWITCH, on):
        got = storage.search(Q, top_k=top_k, filters={"kind": "x"})
    assert [k for _, k in world.calls] == fetches
    # ids compared sorted: the order of the legacy (OFF) rows is the reranker's, which this unit does not touch
    assert sorted(r["item_id"] for r in got) == sorted(ids)
    assert getattr(got, "partial", ()) == names
    assert type(got) is (PartialRows if names else list)
    assert unit_log(caplog) == warnings


UNFILTERED_ROWS = [
    ("on partial", True, 1, [], PARTIAL, [gate_warning("local search")]),
    ("on complete", True, None, ["test_u521_5"], (), []),
    ("off", False, 1, [f"test_u521_{n}" for n in range(6)], (), [OFF_SWITCH_WARNING]),
]


@pytest.mark.parametrize(
    "case,on,budget,ids,names,warnings",
    UNFILTERED_ROWS,
    ids=[r[0] for r in UNFILTERED_ROWS],
)
def test_unfiltered_search_carries_the_partial_answer_on_the_plain_list(
    world, caplog, case, on, budget, ids, names, warnings
):
    world.budget["max_scan"] = budget
    with switch_override(SWITCH, on):
        got = storage.search(Q, top_k=10)
    assert [k for _, k in world.calls] == [10]
    assert sorted(r["item_id"] for r in got) == sorted(ids)
    assert getattr(got, "partial", ()) == names and type(got) is (
        PartialRows if names else list
    )
    assert unit_log(caplog) == warnings


def test_wildcard_complete_page_is_exhaustion_and_a_partial_one_is_refused_at_the_cap(
    world, caplog, monkeypatch
):
    """The wildcard resolves core's partial answer itself (widen, else a typed refusal), so its result needs no carrier."""
    with switch_override(SWITCH, True):
        got = storage.search("*")
    assert [r["item_id"] for r in got] == ["test_u521_5"] and type(got) is list
    assert unit_log(caplog) == []
    caplog.clear()
    callee = PartialCallee()
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: callee)
    monkeypatch.setattr(storage, "_WILDCARD_CORE_CAP", 400)
    with (
        switch_override(SWITCH, True),
        pytest.raises(storage.WildcardEnumerationOverflowError),
    ):
        storage.search("*")
    assert [k for _, k in callee.calls] == [200, 400]
    # core's coverage WARNING (visibility_gate) once per widened window (200, 400), then the wrapper's refusal
    assert unit_log(caplog) == [
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
    with (
        switch_override(SWITCH, True),
        pytest.raises(storage.WildcardEnumerationOverflowError),
    ):
        storage.search("*")
    assert unit_log(caplog) == [
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
    with switch_override(SWITCH, True):
        storage.search(
            Q, top_k=2, read_gate=object(), include_context=True, history=True
        )
    assert seen and not ({"read_gate", "include_context", "history"} & set(seen))


# -- the two optional-context degrade sites: a refusal is logged once, by its origin -------------------------------


def _marked():
    return mark_refusal_logged(PendingFacadeUnavailable("no intake port"))


def _unmarked():
    return PendingFacadeUnavailable("no intake port")


ORIGIN = (GATE_LOG, "WARNING", "origin: intake port unavailable")
SNAPSHOT_LOST = (FORMAT_LOG, "WARNING", "Orient lost snapshot context: no intake port")
DECISION_LOST = (FORMAT_LOG, "WARNING", "Recall lost decision lookup: no intake port")

# (case, exception, gate on, whether the origin logged it, the exact WARNING list). ON: a marked refusal adds nothing to
# what its origin wrote, an unmarked failure is logged here. OFF: the base's behavior whatever the mark: the origin line
# AND the wrapper's own line (BASE OFF = ['origin: intake port unavailable', 'Orient lost snapshot context: no intake
# port'], measured with the base helper by the reviewer and again in the report).
DEGRADE_ROWS = [
    ("on marked", _marked, True, True, [ORIGIN], [ORIGIN]),
    ("on unmarked", _unmarked, True, False, [SNAPSHOT_LOST], [DECISION_LOST]),
    (
        "off marked is the base's two lines",
        _marked,
        False,
        True,
        [ORIGIN, SNAPSHOT_LOST],
        [ORIGIN, DECISION_LOST],
    ),
    ("off unmarked", _unmarked, False, False, [SNAPSHOT_LOST], [DECISION_LOST]),
]


def _run_degrade(world, caplog, exc, on, origin, site):
    def raise_it():
        error = exc()
        if origin:
            logging.getLogger(GATE_LOG).warning("origin: intake port unavailable")
        raise error

    real = world.mem.search
    kwargs = {"include_snapshot": site == "snapshot"}
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
        with switch_override(SWITCH, on):
            storage.recall(
                cwd="/project", top_k=3, workspace_id="test", strict=False, **kwargs
            )
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    # the OFF arm also logs core's OFF switch line and the recall bookkeeping, which are not this refusal's
    return [
        t
        for t in unit_log(caplog)
        if t != OFF_SWITCH_WARNING
        and not t[2].startswith(("recall shortfall", "recall dropped"))
    ], trace


@pytest.mark.parametrize(
    "case,exc,on,origin,snap,_d", DEGRADE_ROWS, ids=[r[0] for r in DEGRADE_ROWS]
)
def test_snapshot_degrade_logs_a_refusal_once(
    world, caplog, case, exc, on, origin, snap, _d
):
    warnings, trace = _run_degrade(world, caplog, exc, on, origin, "snapshot")
    assert warnings == snap
    assert trace["degradations"] == ["Orient lost snapshot context: no intake port"]


@pytest.mark.parametrize(
    "case,exc,on,origin,_s,decision", DEGRADE_ROWS, ids=[r[0] for r in DEGRADE_ROWS]
)
def test_decision_lookup_degrade_logs_a_refusal_once(
    world, caplog, case, exc, on, origin, _s, decision
):
    warnings, trace = _run_degrade(world, caplog, exc, on, origin, "decision")
    assert warnings == decision
    assert trace["degradations"] == ["Recall lost decision lookup: no intake port"]


# (case, error, gate on, WARNING list). The log is skipped for exactly one input: a marked refusal with the gate ON.
HOOK_ROWS = [
    (
        "on unmarked",
        lambda: RuntimeError("boom"),
        True,
        [(FORMAT_LOG, "WARNING", "Lost thing: boom")],
    ),
    ("on marked", lambda: mark_refusal_logged(RuntimeError("boom")), True, []),
    (
        "off unmarked",
        lambda: RuntimeError("boom"),
        False,
        [(FORMAT_LOG, "WARNING", "Lost thing: boom")],
    ),
    (
        "off marked logs as the base did",
        lambda: mark_refusal_logged(RuntimeError("boom")),
        False,
        [(FORMAT_LOG, "WARNING", "Lost thing: boom")],
    ),
]


@pytest.mark.parametrize(
    "case,make,on,warnings", HOOK_ROWS, ids=[r[0] for r in HOOK_ROWS]
)
def test_record_hook_degradation_reads_the_mark_from_the_exception_only(
    caplog, case, make, on, warnings
):
    caplog.set_level(logging.WARNING)
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    try:
        with switch_override(SWITCH, on):
            recall_format.record_hook_degradation("Lost thing", make())
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    assert unit_log(caplog) == warnings
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
    """Gate ON with no intake port bound: core refuses. The wrapper must not answer with an empty list."""
    with switch_override(SWITCH, True), pytest.raises(PendingFacadeUnavailable):
        storage.search("atlas", top_k=3)
    core_logger = "smartmemory.search.pending_facade"
    assert [
        (r.name, r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == core_logger
    ] == [
        (
            core_logger,
            "WARNING",
            "Pending facade read refused: intake port and workspace visibility authority are not bound",
        )
    ]
    assert unit_log(caplog) == []


def test_the_same_search_with_the_gate_off_is_the_legacy_answer(unbound, caplog):
    with switch_override(SWITCH, False):
        got = storage.search("atlas", top_k=3)
    assert [r["item_id"] for r in got] == ["test_row"] and type(got) is list
    assert unit_log(caplog) == [OFF_SWITCH_WARNING]


def test_the_bound_gate_serves_the_row_with_its_status_carrier(unbound):
    with switch_override(SWITCH, True):
        unbound.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
        got = storage.search("atlas", top_k=3)
    assert [r["item_id"] for r in got] == ["test_row"]
    assert got[0]["read_status"]["extraction_state"] == "unavailable"
