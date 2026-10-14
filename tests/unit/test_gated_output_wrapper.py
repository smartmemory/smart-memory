"""U5.21 wrapper search/recall consume core's gated output: a partial answer is not an exhausted one. # trace G:G44,G132

Bug classes:
* C3 a short page is read as "nothing more". Core's scan budget can end a lane early and says so on
  ``SearchResults.diagnostics.coverage``. ``storage.search`` (filtered widening, wildcard) and ``storage.recall``
  (query, workspace and orient arms) must keep widening, or stop at a named cap with one WARNING, never stop silently.
* C1 a refusal logged twice. A core refusal is logged once by the site that raised it, so the two optional-context
  degrade sites (snapshot frame, decision lookup) record the loss in the trace without a second WARNING.
* Both arms of ``sm2_search_visibility_gate`` are paired: OFF output, call count and log are the legacy ones.

The partial-coverage fake returns the real ``SearchResults`` core returns. One real lite memory (SQLite) covers the
typed refusal that must not be swallowed to ``[]``.
"""

import logging
from types import SimpleNamespace

import pytest

from smartmemory.configuration.switch_testing import switch_override
from smartmemory.refusal_logging import mark_refusal_logged
from smartmemory.save.read_port import InMemoryPendingStore
from smartmemory.search.pending_facade import PendingFacadeUnavailable
from smartmemory.search.results import SearchDiagnostics, SearchResults
from smartmemory.search.visibility_gate import SWITCH
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import recall_format, storage

TIME = "2026-10-11T12:00:00.000000+00:00"
PARTIAL = {"read_gate_scan_budget_spent": True}
COMPLETE = {"complete": True}


def _item(name, **meta):
    return SimpleNamespace(
        item_id=name,
        content=name,
        memory_type="semantic",
        metadata=meta,
        confidence=1.0,
        reference=False,
    )


class _Core:
    """A finite core: it examines at most ``budget`` rows per call and says so on the coverage when it stopped early.

    ``full_from``: a fetch of at least this size lets it examine every row (its scan budget finally covers the page).
    """

    def __init__(self, rows, budget, full_from=None):
        self.rows = rows
        self.budget = budget
        self.full_from = full_from
        self.calls = []
        self._graph = SimpleNamespace(search_nodes=lambda query: [])

    def search(self, text, *, top_k, **kwargs):
        self.calls.append((text, top_k))
        if self.full_from is not None and top_k >= self.full_from:
            return SearchResults(
                self.rows[:top_k], SearchDiagnostics(coverage=COMPLETE)
            )
        window = self.rows[: min(top_k, self.budget)]
        stopped = len(self.rows) > len(window) and top_k > self.budget
        return SearchResults(
            window, SearchDiagnostics(coverage=PARTIAL if stopped else COMPLETE)
        )


@pytest.fixture
def core(monkeypatch, caplog):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    monkeypatch.setattr("smartmemory.origin_policy.get_default_tiers", lambda _: {1, 2})
    monkeypatch.setattr(
        "smartmemory.origin_policy.filter_by_tiers",
        lambda rows, tiers: [r for r in rows if getattr(r, "tier", 1) in tiers],
    )
    caplog.set_level(logging.WARNING)

    def install(rows, budget, full_from=None):
        fake = _Core(rows, budget, full_from)
        monkeypatch.setattr(storage, "get_memory", lambda *a, **k: fake)
        return fake

    return install


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records]


# -- recall: exhaustion is decided from core's coverage, on every arm -----------------------------------------------


def _recall(**kw):
    return storage.recall(
        top_k=3, include_snapshot=False, workspace_id="test", strict=False, **kw
    )


ARMS = {
    "query": {"query": "Xavier"},
    "workspace": {"cwd": "/project"},
    "orient": {},
}

# (arm, gate on, fetch sizes each call asked for, memory ids emitted). Four rows exist but core examines one until a
# fetch of 24, so a gate-ON recall widens 6/12/24 (query) or 3/6/12/24 (the two other arms) and reaches all three it
# needs, while the OFF arm reads the one short page as exhausted exactly as before.
RECALL_ROWS = [
    ("query", True, [6, 12, 24], ["r0", "r1", "r2"]),
    ("query", False, [6], ["r0"]),
    ("workspace", True, [3, 6, 12, 24], ["r0", "r1", "r2"]),
    ("workspace", False, [3], ["r0"]),
    ("orient", True, [3, 6, 12, 24], ["r0", "r1", "r2"]),
    ("orient", False, [3], ["r0"]),
]


@pytest.mark.parametrize(
    "arm,on,fetches,ids",
    RECALL_ROWS,
    ids=[f"{r[0]}-{'on' if r[1] else 'off'}" for r in RECALL_ROWS],
)
def test_recall_keeps_widening_while_core_reports_a_partial_answer(
    core, caplog, arm, on, fetches, ids
):
    fake = core([_item(f"r{i}") for i in range(4)], budget=1, full_from=24)
    with switch_override(SWITCH, on):
        out = _recall(**ARMS[arm])
    asked = [
        k for text, k in fake.calls if text == ("" if arm != "query" else "Xavier")
    ]
    assert asked == fetches
    assert [
        line.split("[mem:")[1].split("]")[0] for line in out.splitlines()[1:]
    ] == ids
    # every WARNING is named: ON says core's answer was partial, OFF is the legacy log (the short result only).
    warned = _warnings(caplog)
    assert (
        any("returned a partial answer under the read gate" in m for m in warned) is on
    )
    assert not any("stopped widening" in m for m in warned)


@pytest.mark.parametrize("arm", ["query", "workspace", "orient"])
def test_recall_stops_widening_at_the_cap_with_one_warning(core, caplog, arm):
    """Core keeps saying partial: recall stops once it has asked for the gate's scan budget, and says so once."""
    fake = core([_item("r0")], budget=0)
    with switch_override(SWITCH, True):
        _recall(**ARMS[arm])
    cap_logs = [m for m in _warnings(caplog) if "stopped widening" in m]
    assert cap_logs == [
        "recall stopped widening at a fetch of 1536: core reported a partial answer, so eligible candidates past "
        "what it examined are not shown"
    ]
    assert max(k for _, k in fake.calls) == 1536


# -- search: filtered widening and the wildcard window --------------------------------------------------------------


def _row(n, kind="x"):
    return {
        "item_id": f"r{n}",
        "content": f"r{n}",
        "memory_type": "semantic",
        "metadata": {"kind": kind},
    }


# (case, gate on, core says partial until a fetch of 100, the fetch sizes asked, ids returned, partial WARNINGs).
# Fifteen rows match, top_k=2. A partial core is read widening 10/40/100 until it examines everything. A complete core
# stops after one call in both arms, and the OFF arm stops on the legacy short page.
FILTER_ROWS = [
    (
        "partial gate on widens until core examines all",
        True,
        True,
        [10, 40, 100],
        ["r0", "r1"],
        2,
    ),
    ("partial gate off stops on the short page (legacy)", False, True, [10], ["r0"], 0),
    ("complete gate on stops after one fetch", True, False, [10], ["r0", "r1"], 0),
    ("complete gate off stops after one fetch", False, False, [10], ["r0", "r1"], 0),
]


@pytest.mark.parametrize(
    "case,on,partial_core,fetches,ids,partials",
    FILTER_ROWS,
    ids=[r[0] for r in FILTER_ROWS],
)
def test_filtered_search_widening_follows_core_coverage(
    core, caplog, case, on, partial_core, fetches, ids, partials
):
    fake = (
        core([_row(n) for n in range(15)], budget=1, full_from=100)
        if partial_core
        else core([_row(n) for n in range(15)], budget=100)
    )
    with switch_override(SWITCH, on):
        got = storage.search("atlas", top_k=2, filters={"kind": "x"})
    assert [k for _, k in fake.calls] == fetches
    assert [r["item_id"] for r in got] == ids
    partial_logs = [
        m for m in _warnings(caplog) if "partial answer under the read gate" in m
    ]
    assert len(partial_logs) == partials


def test_unfiltered_search_passes_core_rows_through_with_one_partial_warning(
    core, caplog
):
    fake = core([_row(n) for n in range(4)], budget=1)
    with switch_override(SWITCH, True):
        got = storage.search("atlas", top_k=10)
    assert [k for _, k in fake.calls] == [10]
    assert [r["item_id"] for r in got] == ["r0"]
    assert _warnings(caplog) == [
        "local search returned a partial answer under the read gate: core reported read_gate_scan_budget_spent, "
        "so visible matches past what it examined are not shown"
    ]


def test_wildcard_widens_while_partial_and_stops_at_the_cap(core, caplog, monkeypatch):
    monkeypatch.setattr(storage, "_WILDCARD_CORE_CAP", 800)
    fake = core([_row(n) for n in range(3)], budget=0)
    with switch_override(SWITCH, True), pytest.raises(Exception) as refused:
        storage.search("*")
    assert [k for _, k in fake.calls] == [200, 400, 800]
    cap = [m for m in _warnings(caplog) if m.startswith("wildcard search refused")]
    assert cap == [
        "wildcard search refused: core reported a partial answer at the cap of 800 hits, so the listing would be incomplete"
    ]
    assert "800" in str(refused.value)


def test_wildcard_complete_short_page_is_exhaustion(core, caplog):
    fake = core([_row(n) for n in range(3)], budget=100)
    with switch_override(SWITCH, True):
        got = storage.search("*")
    assert [k for _, k in fake.calls] == [200]
    assert [r["item_id"] for r in got] == ["r0", "r1", "r2"]
    assert _warnings(caplog) == []


def test_the_wrapper_drops_read_gate_and_context_kwargs(core, monkeypatch):
    """G149: the context graph is not a client surface. A caller cannot pass it, so core never sees it."""
    seen = {}

    class Spy(_Core):
        def search(self, text, **kwargs):
            seen.update(kwargs)
            return super().search(text, **kwargs)

    core([], 100)
    spy = Spy([_row(0)], 100)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: spy)
    storage.search(
        "atlas", top_k=2, read_gate=object(), include_context=True, history=True
    )
    assert seen and not ({"read_gate", "include_context", "history"} & set(seen))


# -- the two optional-context degrade sites: a refusal is logged once, by its origin -------------------------------


def _refusing(exc_factory, origin_log):
    def raise_it():
        exc = exc_factory()
        if origin_log:
            logging.getLogger("smartmemory.search.visibility_gate").warning(origin_log)
        raise exc

    return raise_it


def _marked():
    return mark_refusal_logged(PendingFacadeUnavailable("no intake port"))


def _unmarked():
    return PendingFacadeUnavailable("no intake port")


# (case, exception, whether the origin logged it, the WARNING list). A marked refusal adds nothing to what its origin
# wrote. An unmarked failure is logged here, as it always was.
DEGRADE_ROWS = [
    (
        "marked refusal",
        _marked,
        "origin: intake port unavailable",
        ["origin: intake port unavailable"],
    ),
    (
        "unmarked failure",
        _unmarked,
        None,
        ["Orient lost snapshot context: no intake port"],
    ),
]


@pytest.mark.parametrize(
    "case,exc,origin,warnings", DEGRADE_ROWS, ids=[r[0] for r in DEGRADE_ROWS]
)
def test_snapshot_degrade_logs_a_refusal_once(
    core, caplog, case, exc, origin, warnings
):
    fake = core([], budget=100)
    raise_it = _refusing(exc, origin)
    real = fake.search

    def search(text, **kwargs):
        if kwargs.get("memory_type") == "snapshot":
            raise_it()
        return real(text, **kwargs)

    fake.search = search
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    try:
        storage.recall(
            cwd="/project",
            top_k=3,
            include_snapshot=True,
            workspace_id="test",
            strict=False,
        )
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    degraded = [
        m
        for m in _warnings(caplog)
        if not m.startswith("recall shortfall") and "shortfall" not in m
    ]
    assert degraded == warnings
    assert trace["degradations"] == ["Orient lost snapshot context: no intake port"]


@pytest.mark.parametrize(
    "case,exc,origin,warnings", DEGRADE_ROWS, ids=[r[0] for r in DEGRADE_ROWS]
)
def test_decision_lookup_degrade_logs_a_refusal_once(
    core, caplog, case, exc, origin, warnings
):
    fake = core([], budget=100)
    raise_it = _refusing(exc, origin)
    fake._graph = SimpleNamespace(search_nodes=lambda query: raise_it())
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    try:
        storage.recall(
            cwd="/project",
            top_k=3,
            include_snapshot=False,
            workspace_id="test",
            strict=False,
        )
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    expected = [
        w.replace("Orient lost snapshot context", "Recall lost decision lookup")
        for w in warnings
    ]
    assert [m for m in _warnings(caplog) if "shortfall" not in m] == expected
    assert trace["degradations"] == ["Recall lost decision lookup: no intake port"]


# (logged, WARNING list, trace entry): the trace always records the loss, the log is skipped only for a marked one.
HOOK_ROWS = [
    (False, ["Lost thing: boom"]),
    (True, []),
]


@pytest.mark.parametrize(
    "logged,warnings", HOOK_ROWS, ids=["unlogged", "already-logged"]
)
def test_record_hook_degradation_skips_only_the_log(caplog, logged, warnings):
    caplog.set_level(logging.WARNING)
    trace = {}
    token = recall_format._ACTIVE_TRACE.set(trace)
    try:
        recall_format.record_hook_degradation(
            "Lost thing", RuntimeError("boom"), logged=logged
        )
    finally:
        recall_format._ACTIVE_TRACE.reset(token)
    assert _warnings(caplog) == warnings
    assert trace["degradations"] == ["Lost thing: boom"]


# -- a real memory: the typed refusal is not turned into [] -----------------------------------------------------------


@pytest.fixture
def unbound(tmp_path, monkeypatch):
    mem = create_lite_memory(data_dir=str(tmp_path / "test_u521_wrapper"))
    try:
        mem._graph.backend.add_node(
            "test_row",
            {"content": "atlas budget", "origin": "user:test", "created_at": TIME},
            memory_type="semantic",
        )
        monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
        yield mem
    finally:
        mem.close()


def test_a_gate_refusal_propagates_from_search_and_is_logged_once(unbound, caplog):
    """Gate ON with no intake port bound: core refuses. The wrapper must not answer with an empty list."""
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True), pytest.raises(PendingFacadeUnavailable):
        storage.search("atlas", top_k=3)
    assert [m for m in _warnings(caplog) if "refused" in m] == [
        "Pending facade read refused: intake port and workspace visibility authority are not bound"
    ]


def test_the_same_search_with_the_gate_off_is_the_legacy_answer(unbound, caplog):
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, False):
        got = storage.search("atlas", top_k=3)
    assert [r["item_id"] for r in got] == ["test_row"]
    # a real lite memory logs unrelated WARNINGs (bundled snapshot, origin policy): the arm adds none of its own.
    assert [
        m for m in _warnings(caplog) if "partial answer" in m or "refused" in m
    ] == []


def test_the_bound_gate_serves_the_row_with_its_status_carrier(unbound):
    with switch_override(SWITCH, True):
        unbound.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
        got = storage.search("atlas", top_k=3)
    assert [r["item_id"] for r in got] == ["test_row"]
    assert got[0]["read_status"]["extraction_state"] == "unavailable"
