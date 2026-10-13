"""U4.32 (N-02.activate.outer-readers): receipt-or-legacy save return view for local callers.

Real core ``Receipt`` objects and wire dicts stand in for the producer's return (no producer returns one yet).
Pins: every legacy shape unchanged, a receipt read as its subject id (never ``str(receipt)``), G98 skipped
extraction, admission rejections and reader refusals as typed errors with exactly one WARNING, the wrapper's
``ingest`` seam for both arms, and reachability (the receipt arm is unreachable from a legacy return).
One explicit row table per class, no stacked parametrize.
"""

import ast
import logging
from pathlib import Path

import pytest

from smartmemory.save.receipt import ReceiptSubject, build_local_receipt, decode_receipt
from smartmemory_app import save_return, storage
from smartmemory_app.save_return import (
    SaveRejected,
    SaveReturnRefused,
    ingest_dict,
    resolve_save_return,
    save_value,
)

LOGGER = "smartmemory_app.save_return"


def receipt(
    kind="item_save",
    stage="stored",
    state="queued",
    outcome=None,
    terminal=False,
    **over,
):
    subject = {
        "item_save": ReceiptSubject("item_save", item_id="test_item"),
        "document_save": ReceiptSubject("document_save", document_id="test_doc"),
        "edit": ReceiptSubject("edit", item_id="test_item"),
        "stage_job": ReceiptSubject(
            "stage_job", occurrence_id="test_occ", stage_id="test_stage"
        ),
    }[kind]
    fields = {"revision": 1} if kind != "stage_job" else {}
    return build_local_receipt(
        subject=subject,
        run_id="test_run",
        processing_stage=None if kind == "stage_job" else stage,
        execution_state=state,
        outcome=outcome,
        terminal=terminal,
        result_availability="none" if terminal else "pending",
        **fields,
        **over,
    )


def generation_receipt():
    """build_local_receipt does not build generation saves (not a Lite subject): decode the wire a producer sends."""
    wire = {
        **receipt("document_save").to_dict(),
        "subject": {"kind": "generation_save", "generation_id": "test_gen"},
    }
    return decode_receipt(wire)


def rejection(code):
    wire = {
        "schema_version": "1.1",
        "admission_error": {"code": code, "message": f"test {code}"},
    }
    if (
        code == "revision_conflict"
    ):  # an edit conflict names its subject and both revisions
        wire.update(
            subject={"kind": "edit", "item_id": "test_item"},
            expected_revision=1,
            current_revision=2,
        )
    return decode_receipt(wire)


def one_warning(caplog, expected):
    assert [(r.levelno, r.name, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, LOGGER, expected)
    ]


LEGACY_ROWS = [
    ("id string", "test-id-1"),
    ("empty string", ""),
    ("queued dict", {"item_id": "test-id-2", "queued": True, "run_id": "r1"}),
    (
        "held dict",
        {
            "status": "held",
            "item_id": None,
            "queued": False,
            "reason": "low_confidence",
            "reasons": [],
        },
    ),
    ("error dict", {"error": "test failure"}),
    ("none", None),
    ("int", 7),
]


@pytest.mark.parametrize(
    "value", [row[1] for row in LEGACY_ROWS], ids=[row[0] for row in LEGACY_ROWS]
)
def test_legacy_return_passes_through_unchanged(value, caplog):
    caplog.set_level(logging.DEBUG)
    resolved = resolve_save_return(value, op="add")
    assert (
        resolved.value is value
        and resolved.receipt is None
        and resolved.summary is None
    )
    assert save_value(value, op="add") is value
    assert ingest_dict(value, op="ingest") is value
    assert caplog.records == []


def test_str_and_legacy_dict_never_leave_the_first_branch(monkeypatch):
    """Legacy returns import nothing: a core without receipts (or a broken import) cannot change them."""
    import builtins

    real = builtins.__import__

    def refuse_receipt_import(name, *args, **kwargs):
        if name.startswith("smartmemory.save"):
            raise AssertionError(f"legacy return imported {name}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_receipt_import)
    for value in ("test-id", {"item_id": "x", "queued": False}, {"status": "held"}):
        assert resolve_save_return(value, op="add").value is value


RECEIPT_ROWS = [
    # (name, receipt factory, expected id, expected summary)
    (
        "item received",
        lambda: receipt(stage="received"),
        "test_item",
        "test_item (received)",
    ),
    (
        "item stored, processing",
        lambda: receipt(stage="stored"),
        "test_item",
        "test_item (stored)",
    ),
    ("document", lambda: receipt("document_save"), "test_doc", "test_doc (stored)"),
    ("generation", generation_receipt, "test_gen", "test_gen (stored)"),
    ("edit", lambda: receipt("edit"), "test_item", "test_item (stored)"),
    # G98: a save that skips extraction completes at stored with "extraction skipped"
    (
        "G98 extraction skipped",
        lambda: receipt(state="completed", outcome="skipped", terminal=True),
        "test_item",
        "test_item (Saved, extraction skipped)",
    ),
]


@pytest.mark.parametrize(
    "make, expected_id, summary",
    [row[1:] for row in RECEIPT_ROWS],
    ids=[row[0] for row in RECEIPT_ROWS],
)
def test_receipt_reads_as_its_subject_id_on_both_shapes(
    make, expected_id, summary, caplog
):
    """R15: the receipt object and its wire dict give the same id; neither is ``str(receipt)``."""
    obj = make()
    for shape in (obj, obj.to_dict()):
        resolved = resolve_save_return(shape, op="add")
        assert resolved.value == expected_id and isinstance(resolved.value, str)
        assert resolved.receipt == obj.to_dict() and resolved.summary == summary
        assert resolved.value != str(obj)
    assert caplog.records == []


def test_wire_dict_keeps_additive_reader_fields():
    wire = {**receipt().to_dict(), "x_future_field": {"kept": True}}
    resolved = resolve_save_return(wire, op="add")
    assert resolved.value == "test_item" and resolved.receipt["x_future_field"] == {
        "kept": True
    }


REJECTION_ROWS = ["revision_conflict", "idempotency_conflict", "retry_key_expired"]


@pytest.mark.parametrize("code", REJECTION_ROWS)
def test_admission_rejection_is_typed_logged_and_never_an_id(code, caplog):
    for shape in (rejection(code), rejection(code).to_dict()):
        caplog.clear()
        with pytest.raises(SaveRejected) as caught:
            resolve_save_return(shape, op="add")
        assert caught.value.code == code and caught.value.message == f"test {code}"
        one_warning(
            caplog,
            f"Save return add rejected at admission ({code}); nothing was stored under a new id",
        )


REFUSAL_ROWS = [
    (
        "unsupported major version",
        lambda: {**receipt().to_dict(), "schema_version": "2.0"},
        "Save return add refused: receipt refused by the reader (unsupported_version); no id returned",
    ),
    (
        "unsupported subject kind",
        lambda: {
            **receipt().to_dict(),
            "subject": {"kind": "future_kind", "item_id": "x"},
        },
        "Save return add refused: receipt refused by the reader (unsupported_subject); no id returned",
    ),
    (
        "invalid wire",
        lambda: {"schema_version": "1.1", "run_id": 7},
        "Save return add refused: save return is not a valid receipt; no id returned",
    ),
    (
        "malformed version",
        lambda: {"schema_version": "one"},
        "Save return add refused: save return is not a valid receipt; no id returned",
    ),
    (
        "job subject names no stored memory",
        lambda: receipt("stage_job", state="queued"),
        "Save return add refused: receipt subject stage_job is not a save; no id returned",
    ),
]


@pytest.mark.parametrize(
    "make, message",
    [row[1:] for row in REFUSAL_ROWS],
    ids=[row[0] for row in REFUSAL_ROWS],
)
def test_unreadable_receipt_is_a_typed_refusal_with_one_warning(make, message, caplog):
    with pytest.raises(SaveReturnRefused):
        resolve_save_return(make(), op="add")
    one_warning(caplog, message)


def test_receipt_wire_with_a_core_that_cannot_read_receipts_is_refused_not_passed_on(
    monkeypatch, caplog
):
    """A wire dict carrying schema_version is never returned as if it were a legacy dict (it would read as id "unknown")."""
    import sys

    import smartmemory.save

    wire = receipt().to_dict()
    monkeypatch.delattr(smartmemory.save, "receipt", raising=False)
    monkeypatch.setitem(sys.modules, "smartmemory.save.receipt", None)
    with pytest.raises(SaveReturnRefused):
        resolve_save_return(wire, op="add")
    one_warning(
        caplog,
        "Save return add refused: the installed core cannot read save receipts; no id returned",
    )
    caplog.clear()
    assert (
        resolve_save_return(None, op="add").value is None
    )  # any other value is legacy, no warning
    assert caplog.records == []


def test_reader_refusal_object_is_refused_too(caplog):
    refusal = decode_receipt({**receipt().to_dict(), "schema_version": "2.0"})
    with pytest.raises(SaveReturnRefused):
        resolve_save_return(refusal, op="add")
    one_warning(
        caplog,
        "Save return add refused: receipt refused by the reader (unsupported_version); no id returned",
    )


# -- the wrapper's ingest seam, both arms -------------------------------------------------------------
class FakeMemory:
    """The core facade's save return, scripted. No producer returns a receipt yet, so this is the seam input."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def ingest(self, content, context=None, sync=True):
        self.calls.append((content, sync))
        return self.result


@pytest.fixture
def lite_ingest(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_data_path", tmp_path)

    def arm(result):
        mem = FakeMemory(result)
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        monkeypatch.setattr(storage, "llm_extraction_warning", lambda: None)
        return mem

    return arm


INGEST_ROWS = [
    # (name, core return, sync, expected wrapper return)
    ("legacy id, sync", "test-id", True, "test-id"),
    ("legacy dict, sync", {"item_id": "test-id", "queued": False}, True, "test-id"),
    (
        "legacy id, async dict",
        {"item_id": "test-id", "queued": True, "run_id": "r1"},
        False,
        None,
    ),
    ("receipt, sync", receipt(), True, "test_item"),
    ("receipt wire, sync", receipt().to_dict(), True, "test_item"),
    (
        "receipt, async",
        receipt(),
        False,
        {
            "item_id": "test_item",
            "queued": True,
            "run_id": "test_run",
            "receipt": receipt().to_dict(),
        },
    ),
    (
        "terminal receipt, async",
        receipt(state="completed", outcome="skipped", terminal=True),
        False,
        {
            "item_id": "test_item",
            "queued": False,
            "run_id": "test_run",
            "receipt": receipt(
                state="completed", outcome="skipped", terminal=True
            ).to_dict(),
        },
    ),
]


@pytest.mark.parametrize(
    "result, sync, expected",
    [row[1:] for row in INGEST_ROWS],
    ids=[row[0] for row in INGEST_ROWS],
)
def test_wrapper_ingest_reads_both_arms(lite_ingest, result, sync, expected):
    lite_ingest(result)
    out = storage.ingest("test content", sync=sync)
    assert out == (expected if expected is not None else result)


def test_wrapper_ingest_rejected_receipt_raises_and_stores_nothing(lite_ingest, caplog):
    lite_ingest(rejection("revision_conflict"))
    with pytest.raises(SaveRejected):
        storage.ingest("test content", sync=True)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        "Save return ingest rejected at admission (revision_conflict); nothing was stored under a new id"
    ]


def test_normalize_ingest_result_legacy_arms_unchanged():
    for value, expected in (
        ("a", "a"),
        ({"item_id": "b"}, "b"),
        ({"x": 1}, "{'x': 1}"),
        (None, "None"),
        (5, "5"),
    ):
        assert storage._normalize_ingest_result(value) == expected


# -- reachability: the receipt arm is not reachable from a legacy return ---------------------------------
def test_only_the_ingest_seam_imports_the_receipt_arm():
    """AST scan of every importer of smartmemory_app.save_return inside the wrapper package."""
    root = Path(save_return.__file__).parent
    importers = set()
    for path in sorted(root.rglob("*.py")):
        if path.name == "save_return.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "smartmemory_app.save_return"
            ):
                importers.add(path.name)
    assert importers == {"storage.py"}


def test_receipt_without_the_status_badge_reader_is_refused_not_str(
    monkeypatch, caplog
):
    """A receipt exists, so core has receipts: a missing badge reader refuses (typed, one WARNING), it never raises raw."""
    import sys

    import smartmemory.save

    skipped = receipt(state="completed", outcome="skipped", terminal=True)
    monkeypatch.delattr(smartmemory.save, "status", raising=False)
    monkeypatch.setitem(sys.modules, "smartmemory.save.status", None)
    with pytest.raises(SaveReturnRefused):
        resolve_save_return(skipped, op="add")
    one_warning(
        caplog,
        "Save return add refused: the installed core cannot read save status badges; no id returned",
    )


# -- every consumer of the wrapper's ingest, enumerated by AST (review r1 F3) -------------------------------
def ingest_callers(root):
    """Every call of ``smartmemory_app.storage.ingest`` under ``root``: bare imported name, module alias or dotted.

    Returns ``(file relative to root, enclosing function, how the return value is used)``.
    """
    found = set()
    for path in sorted(Path(root).rglob("*.py")):
        if path.name == "storage.py":
            continue
        tree = ast.parse(path.read_text())
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        names, mods = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "smartmemory_app.storage":
                names |= {a.asname or a.name for a in n.names if a.name == "ingest"}
            if isinstance(n, ast.ImportFrom) and n.module == "smartmemory_app":
                mods |= {a.asname or a.name for a in n.names if a.name == "storage"}
            if isinstance(n, ast.Import):
                mods |= {
                    a.asname
                    for a in n.names
                    if a.name == "smartmemory_app.storage" and a.asname
                }
        for n in ast.walk(tree):
            f = n.func if isinstance(n, ast.Call) else None
            if not (
                (isinstance(f, ast.Name) and f.id in names)
                or (
                    isinstance(f, ast.Attribute)
                    and f.attr == "ingest"
                    and (
                        (isinstance(f.value, ast.Name) and f.value.id in mods)
                        or ast.unparse(f.value) == "smartmemory_app.storage"
                    )
                )
            ):
                continue
            fn = n
            while fn in parents and not isinstance(fn, ast.FunctionDef):
                fn = parents[fn]
            p = parents[n]
            use = (
                "discarded"
                if isinstance(p, ast.Expr)
                else "returned"
                if isinstance(p, ast.Return)
                else "assigned to " + ast.unparse(p.targets[0])
                if isinstance(p, ast.Assign)
                else "argument of " + ast.unparse(p.func)
                if isinstance(p, ast.Call)
                else "used in " + type(p).__name__
            )
            found.add((str(path.relative_to(root)), fn.name, use))
    return found


#: One row per consumer. A new caller of ``storage.ingest`` fails this table until its reading of the return is
#: stated: it either discards the value, or reads it as an id string, or reads ``["item_id"]`` of the dict.
INGEST_CONSUMER_ROWS = [
    ("cli.py", "add_cmd", "argument of click.echo", "reads an id string (sync)"),
    ("cli.py", "add_cmd", "argument of ids.append", "reads an id string (sync)"),
    ("lifecycle.py", "observe", "discarded", "discards the value"),
    ("lifecycle.py", "distill", "discarded", "discards the value"),
    ("lifecycle.py", "learn", "discarded", "discards the value"),
    (
        "local_api.py",
        "ingest_endpoint",
        "assigned to result",
        "reads item_id of the dict, or an id string (async)",
    ),
]


def test_every_wrapper_ingest_consumer_is_assessed():
    root = Path(save_return.__file__).parent
    assert ingest_callers(root) == {row[:3] for row in INGEST_CONSUMER_ROWS}


# Rows below drive the real consumers that read the return (cli ``add``, the daemon ``/ingest`` route) with the
# scripted core return on both arms. The lifecycle sites discard the value, so the arm cannot change what they read.
CONSUMER_ARMS = [
    # (name, core return, expected id read by the consumer)
    ("legacy id", "test-id", "test-id"),
    ("legacy queued dict", {"item_id": "test-id", "queued": True}, "test-id"),
    ("receipt", receipt(), "test_item"),
    ("receipt wire", receipt().to_dict(), "test_item"),
    (
        "terminal receipt",
        receipt(state="completed", outcome="skipped", terminal=True),
        "test_item",
    ),
]


@pytest.mark.parametrize(
    "result, expected",
    [row[1:] for row in CONSUMER_ARMS],
    ids=[row[0] for row in CONSUMER_ARMS],
)
def test_daemon_ingest_route_reads_the_id_on_both_arms(lite_ingest, result, expected):
    from smartmemory_app.local_api import IngestRequest, ingest_endpoint

    lite_ingest(result)
    assert ingest_endpoint(IngestRequest(content="test content")) == {
        "item_id": expected
    }


@pytest.mark.parametrize(
    "result, expected",
    [row[1:] for row in CONSUMER_ARMS],
    ids=[row[0] for row in CONSUMER_ARMS],
)
def test_cli_add_prints_the_id_on_both_arms(lite_ingest, result, expected):
    from unittest.mock import patch

    from click.testing import CliRunner

    from smartmemory_app.cli import cli

    lite_ingest(result)
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.cli._prepare_direct_access"),
    ):
        single = CliRunner().invoke(cli, ["add", "test content"])
        piped = CliRunner().invoke(cli, ["add", "-"], input="test one\ntest two\n")
    assert single.exit_code == 0 and single.output.strip() == expected
    assert piped.exit_code == 0
    assert piped.output.split() == ["Added", "2", "memories", expected, expected]
