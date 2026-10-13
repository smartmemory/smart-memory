"""U4.32 (N-02.activate.outer-readers): wrapper projection of Lite save status, live and retained.

Real SQLite ``memory.db`` under a tmp data dir and core's real lifecycle, gate and adapter. Pins G53/G137/G148
(live, then retained, then expired receipt through the wrapper), G44 (an actor or workspace that may not see a
run reads the same refusal as an absent id, on every source), the explicit capability and availability answer,
and one typed refusal with one WARNING per failure arm. One explicit row table per class.
"""

import json
import logging
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict

import pytest

from smartmemory.pipeline.work_graph.lifecycle import SQLiteWorkGraphV2Lifecycle
from smartmemory.pipeline.work_graph.port import ExecutionEpoch, ItemSaveSubject, Scope
from smartmemory.save import local_status
from smartmemory.save.local_status import LocalStatusRefused, RunUnknown
from smartmemory.save.read_port import InMemoryPendingStore
from smartmemory.save.run_fate import ExtractionPolicy, RunFate, RunIdentity
from smartmemory.save.status import (
    ExecutionState,
    ProcessingOutcome,
    ProcessingStage,
    ProcessingStatus,
)
from smartmemory_app import work_graph

RUN = "test_ls_run"
SCOPE = Scope("t1", "ws1")
UNKNOWN_LOG = "Local status get_receipt refused: run unavailable in caller scope; nothing returned"
ACTOR = {"tenant_id": "t1", "workspace_id": "ws1", "user_id": "u1"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Items:
    """The authoritative subject-row read the gate hydrates through (the wrapper takes it as an argument)."""

    def __init__(self):
        self.rows = {}

    def visible(self, item_id, **over):
        self.rows[item_id] = {
            "item_id": item_id,
            "workspace_id": "ws1",
            "tenant_id": "t1",
            "tombstoned": False,
            **over,
        }

    def __call__(self, kind, subject_id):
        return self.rows.get(subject_id)


@pytest.fixture
def lite(tmp_path, monkeypatch):
    """A real Lite memory.db at the wrapper's own resolved path, plus a lifecycle for seeding and cleanup."""
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    store = work_graph.get_work_graph()
    path = str(tmp_path / "memory.db")
    clock = Clock()
    lifecycle = SQLiteWorkGraphV2Lifecycle(
        store, scope=SCOPE, fence=lambda request: _allowed(), clock=clock
    )
    items = Items()
    items.visible("test_item")
    return path, store, lifecycle, clock, items


def _allowed():
    from smartmemory.authority import AuthorityVerdict

    return AuthorityVerdict.allowed


def status(**actor):
    return {**ACTOR, **actor}


def live_run(
    path, run_id=RUN, item="test_item", ws="ws1", tenant="t1", state="running"
):
    authority = {
        "kind": "workspace",
        "scope": asdict(Scope(tenant, ws)),
        "actor_id": "actor",
        "payer_authorization_epoch": 0,
    }
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO wg_run(run_id,workspace_id,subject_kind,subject_id,subject,authority,execution_epoch,origin,"
            "store_generation,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,1)",
            (
                run_id,
                ws,
                "item_save",
                item,
                json.dumps({"item_id": item}),
                json.dumps(authority),
                1,
                "save",
                "",
                state,
            ),
        )
        conn.execute(
            "INSERT INTO wg_box(run_id,box_id,kind,lane,updated_at,status) VALUES(?,?,?,?,1,?)",
            (run_id, "b1", "extract", "fast", "done"),
        )
        conn.execute(
            "INSERT INTO wg_event(event_id,run_id,workspace_id,kind,payload,created_at) VALUES(?,?,?,?,?,1)",
            (
                f"adm_{run_id}",
                run_id,
                ws,
                "run.admitted",
                json.dumps(
                    {
                        "admitted_revision": 7,
                        "effect_targets": [],
                        "scheduled_interval_start": None,
                        "created_at": "2026-10-11T00:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()


def settle(lifecycle, run_id=RUN):
    fate_status = ProcessingStatus(
        "item_save",
        ProcessingStage.EXTRACTED,
        ExecutionState.COMPLETED,
        ProcessingOutcome.COMPLETED,
    )
    lifecycle.commit_terminal(
        RunFate(
            RunIdentity(
                SCOPE, ItemSaveSubject("test_item"), run_id, 7, ExecutionEpoch(1)
            ),
            (),
            fate_status,
            ExtractionPolicy.ENABLED,
            ProcessingOutcome.COMPLETED,
            (),
            None,
            (),
        )
    )


def clean_up(lifecycle, clock):
    for intent in lifecycle.pending_outbox(scope=lifecycle.scope):
        assert lifecycle.mark_delivered(intent.intent_id, scope=lifecycle.scope)
    clock.now += 100000
    assert lifecycle.cleanup_v2(1) == 1


def read(items, run_id=RUN, **over):
    return work_graph.get_local_status(
        run_id,
        **status(
            **{
                "workspace_hidden": False,
                "pending": InMemoryPendingStore(),
                "fetch_subject": items,
                **over,
            }
        ),
    )


def one_warning(caplog, expected):
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, expected)
    ]


# -- G53/G137/G148: live, retained, expired through the wrapper -------------------------------------------
def test_live_then_retained_then_expired_projection(lite, caplog):
    path, _, lifecycle, clock, items = lite
    live_run(path)
    live = read(items)
    assert live["arm"] == "receipt" and live["status_text"] == "received"
    assert live["receipt"]["run_id"] == RUN and live["receipt"]["terminal"] is False
    settle(lifecycle)
    retained = read(items)
    assert (
        retained["receipt"]["terminal"] is True
        and retained["receipt"]["outcome"] == "completed"
    )
    clean_up(lifecycle, clock)
    with closing(sqlite3.connect(path)) as conn:  # run detail is really gone
        assert (
            conn.execute("SELECT 1 FROM wg_run WHERE run_id=?", (RUN,)).fetchall() == []
        )
    expired = read(items)
    assert expired["status_text"] == "completed; detailed result expired"
    assert (
        expired["receipt"]["result_availability"] == "expired"
        and expired["receipt"]["placement"] == "lite"
    )
    assert {
        k: v for k, v in expired["receipt"].items() if k != "result_availability"
    } == {k: v for k, v in retained["receipt"].items() if k != "result_availability"}
    assert caplog.records == []


def test_legacy_queue_run_projects_as_display_only(lite, caplog):
    path, _, _, _, items = lite
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO work_runs VALUES (?,?,?,?,?,1)",
            ("test_ls_legacy", "ws1", "test_item", "", "save"),
        )
        conn.execute(
            "INSERT INTO work_nodes(run_id,node_id,kind,input,status,attempts,updated_at) VALUES(?,?,?,?,?,?,1)",
            ("test_ls_legacy", "n0", "stage:extract", "{}", "pending", 0),
        )
        conn.commit()
    out = read(items, "test_ls_legacy")
    assert out["arm"] == "legacy" and out["legacy"]["status"] == "pending"
    assert out["legacy"]["provenance"] == "legacy_queue" and "receipt" not in out
    assert out["legacy"]["status_url"] == "local-status:test_ls_legacy"
    assert [r.levelno for r in caplog.records] == [
        logging.WARNING
    ]  # core's one downgrade warning


# -- G44: actor / workspace gating, every refusal the same as an absent id ----------------------------------
GATE_ROWS = [
    # (name, seed kwargs, read overrides)
    ("absent id", {"run_id": "test_ls_other"}, {"run_id": "test_ls_absent"}),
    ("other workspace run", {"ws": "ws2"}, {}),
    ("other tenant run", {"tenant": "evil"}, {}),
    ("revoked workspace", {}, {"workspace_hidden": True}),
    ("other tenant actor", {}, {"tenant_id": "evil"}),
]


@pytest.mark.parametrize(
    "seed_kw, over", [row[1:] for row in GATE_ROWS], ids=[row[0] for row in GATE_ROWS]
)
def test_unseen_run_reads_as_one_uniform_unknown(lite, caplog, seed_kw, over):
    path, _, _, _, items = lite
    live_run(path, **seed_kw)
    run_id = over.pop("run_id", seed_kw.get("run_id", RUN))
    actor = {
        k: v for k, v in over.items() if k in ("tenant_id", "workspace_id", "user_id")
    }
    rest = {k: v for k, v in over.items() if k not in actor}
    with pytest.raises((RunUnknown, LocalStatusRefused)) as caught:
        work_graph.get_local_status(
            run_id,
            **status(**actor),
            **{
                "workspace_hidden": False,
                "pending": InMemoryPendingStore(),
                "fetch_subject": items,
                **rest,
            },
        )
    if not isinstance(caught.value, RunUnknown):
        pytest.fail(
            f"{type(caught.value).__name__} is distinguishable from an absent id"
        )
    one_warning(caplog, UNKNOWN_LOG)


def test_hidden_subject_reads_like_absent(lite, caplog):
    path, _, _, _, items = lite
    live_run(path, item="test_hidden")
    items.visible("test_hidden", tombstoned=True)
    with pytest.raises(RunUnknown):
        read(items)
    one_warning(caplog, UNKNOWN_LOG)


def test_reads_write_nothing_to_existing_rows(lite):
    path, _, lifecycle, clock, items = lite
    live_run(path)
    settle(lifecycle)

    def dump():
        with closing(sqlite3.connect(path)) as conn:
            return list(conn.iterdump())

    before = dump()
    read(items)
    assert dump() == before


# -- capability and availability are explicit --------------------------------------------------------------
def test_capability_is_explicit_and_declares_hosted_only_unavailable():
    capability = work_graph.local_status_capability()
    assert capability["available"] is True and capability["placement"] == "lite"
    assert capability["capabilities"] == {
        "team_allowances": False,
        "subscription_delivery": False,
        "notifications": False,
    }
    assert capability["unavailable"] == [
        "notifications",
        "subscription_delivery",
        "team_allowances",
    ]
    assert capability["status_url_template"] == "local-status:{run_id}"


def test_core_without_the_adapter_is_unavailable_not_empty(lite, monkeypatch, caplog):
    _, _, _, _, items = lite
    import smartmemory.save

    monkeypatch.delattr(
        smartmemory.save, "local_status", raising=False
    )  # the package attribute would win
    monkeypatch.setitem(
        sys.modules, "smartmemory.save.local_status", None
    )  # ImportError on import
    expected = (
        f"{work_graph.STATUS_UNAVAILABLE}; status replay refused, no status returned"
    )
    assert work_graph.local_status_capability() == {
        "available": False,
        "reason": work_graph.STATUS_UNAVAILABLE,
    }
    one_warning(caplog, expected)
    caplog.clear()
    with pytest.raises(work_graph.LocalStatusUnavailable):
        read(items)
    one_warning(caplog, expected)


# -- wiring faults are typed refusals, never a grant --------------------------------------------------------
def test_status_open_has_no_default_grant(lite):
    _, _, _, _, items = lite
    for missing in (
        "workspace_hidden",
        "pending",
        "fetch_subject",
        "user_id",
        "tenant_id",
        "workspace_id",
    ):
        kwargs = {
            **ACTOR,
            "workspace_hidden": False,
            "pending": InMemoryPendingStore(),
            "fetch_subject": items,
        }
        del kwargs[missing]
        with pytest.raises(TypeError):
            work_graph.open_local_status(**kwargs)


def test_non_boolean_workspace_authority_is_refused_before_any_read(lite, caplog):
    _, _, _, _, items = lite
    from smartmemory.visibility import TYPED_REFUSALS

    with pytest.raises(TYPED_REFUSALS):
        read(items, workspace_hidden=None)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert caplog.records[0].getMessage().startswith("Read visibility unavailable")


def test_read_only_fence_denies_and_logs(caplog):
    from smartmemory.authority import AuthorityVerdict

    assert work_graph._read_only_fence(object()) is AuthorityVerdict.unavailable
    one_warning(
        caplog, "Local status fence called on a read-only status adapter; denied"
    )


def test_unreadable_run_id_is_typed(lite, caplog):
    _, _, _, _, items = lite
    from smartmemory.save.local_status import InvalidRunId

    with pytest.raises(InvalidRunId):
        read(items, "")
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# -- C1/C8: setup I/O and identity faults are typed refusals with one WARNING, never a raw driver error --------
SETUP_ROWS = [
    # (name, fault installed, expected exception, expected log)
    (
        "sqlite open error",
        lambda mp: mp.setattr(
            work_graph, "get_work_graph", _raise(sqlite3.OperationalError("locked"))
        ),
        "LocalStatusRefused",
        "Local status refused: the work graph could not be opened (OperationalError); nothing returned",
    ),
    (
        "data dir unwritable",
        lambda mp: mp.setattr(
            work_graph, "get_work_graph", _raise(PermissionError("ro"))
        ),
        "LocalStatusRefused",
        "Local status refused: the work graph could not be opened (PermissionError); nothing returned",
    ),
    (
        "store of the wrong kind",
        lambda mp: mp.setattr(work_graph, "get_work_graph", lambda: object()),
        "LocalStatusRefused",
        "bind_sqlite_v2 refused: store is object, not a SQLiteWorkGraph; v2 adapter not bound",  # core's one line
    ),
]


def _raise(exc):
    def fail():
        raise exc

    return fail


@pytest.mark.parametrize(
    "install, expected, message",
    [row[1:] for row in SETUP_ROWS],
    ids=[row[0] for row in SETUP_ROWS],
)
def test_setup_failure_is_typed_and_logged(
    lite, monkeypatch, caplog, install, expected, message
):
    _, _, _, _, items = lite
    install(monkeypatch)
    with pytest.raises(getattr(local_status, expected)):
        read(items)
    one_warning(caplog, message)


IDENTITY_ROWS = [
    ("empty tenant", {"tenant_id": ""}, "WorkGraphContractError"),
    ("missing workspace", {"workspace_id": None}, "WorkGraphContractError"),
    ("empty user", {"user_id": ""}, "InvalidRow"),
]


@pytest.mark.parametrize(
    "over, cause",
    [row[1:] for row in IDENTITY_ROWS],
    ids=[row[0] for row in IDENTITY_ROWS],
)
def test_unreadable_identity_is_invalid_argument_with_one_warning(
    lite, caplog, over, cause
):
    _, _, _, _, items = lite
    with pytest.raises(local_status.InvalidArgument):
        read(items, **over)
    one_warning(
        caplog,
        f"Local status refused: caller identity is not readable ({cause}); nothing returned",
    )
