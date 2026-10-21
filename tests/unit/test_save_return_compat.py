"""U4.32 (N-02.activate.outer-readers): receipt-or-legacy save return view for local callers.

Real core ``Receipt`` objects and wire dicts stand in for the producer's return (no producer returns one yet).
Pins: every legacy shape unchanged, a receipt read as its subject id (never ``str(receipt)``), G98 skipped
extraction, admission rejections and reader refusals as typed errors with exactly one WARNING, the wrapper's
``ingest`` seam for both arms, and reachability (the receipt arm is unreachable from a legacy return).
One explicit row table per class, no stacked parametrize.
"""

import ast
import importlib.util
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import pytest
from smartmemory.save import receipt as core_receipt
from smartmemory.save.receipt import ReceiptSubject, build_local_receipt, decode_receipt

from smartmemory_app import save_return, storage
from smartmemory_app.save_caller import (
    NOT_A_RECEIPT,
    SaveCallRefused,
    build_save_call,
    submit,
)
from smartmemory_app.save_return import (
    SaveRejected,
    SaveReturnRefused,
    ingest_dict,
    is_receipt_arm,
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


# --- fix round 2 (N1): one receipt/legacy discrimination rule, every receipt subtype --------------------------------
# Bug class: legacy id envelope broken at the receipt boundary. An exact class-name or dict-only test sent a subclass of
# core's receipt, a non-dict mapping or a foreign copy of core's receipt to the legacy arm, where a rejected admission
# read as success. One row per member of the class. The same rows run against the wrapper
# (tests/unit/test_save_return_compat.py), smartmemory_mcp (tests/test_local_save_return_compat.py) and the service
# (tests/unit/test_cli_add_receipt.py), each against its own consumers.
N1_REJECTED = {
    "schema_version": "1.1",
    "admission_error": {"code": "idempotency_conflict", "message": "test conflict"},
}
N1_FUTURE = {"schema_version": "2.0", "run_id": "test_run"}


def _n1_wire(kind="item_save"):
    if kind == "item_save":
        subject, fields = (
            core_receipt.ReceiptSubject("item_save", item_id="test_item"),
            {"processing_stage": "stored", "revision": 1},
        )
    else:
        subject, fields = (
            core_receipt.ReceiptSubject(
                "stage_job", occurrence_id="test_occ", stage_id="test_stage"
            ),
            {"processing_stage": None},
        )
    return core_receipt.build_local_receipt(
        subject=subject,
        run_id="test_run",
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
        **fields,
    ).to_dict()


class DerivedReceipt(core_receipt.Receipt):
    """A plain subclass: still core's receipt, validated by core's own constructor (the reviewer's N1 repro)."""


class HidingReceipt(core_receipt.Receipt):
    """Overrides ``to_dict`` to report an admitted save: only core's own export shows the rejection."""

    def to_dict(self):
        return _n1_wire()


class DerivedRefusal(core_receipt.ReceiptRefusal):
    """A subclass of core's reader refusal."""


def _named_receipt():
    class Receipt:  # not core's: a lookalike whose to_dict reads as an admitted save
        def to_dict(self):
            return _n1_wire()

    return Receipt()


def _refusal_named_base():
    class ReceiptRefusal:
        pass

    class Wrapped(ReceiptRefusal):
        def to_dict(self):
            return _n1_wire()

    return Wrapped()


def _second_core_copy_rejected():
    """A rejected receipt built by a second, separately loaded copy of core's receipt module (its classes differ)."""
    spec = importlib.util.spec_from_file_location(
        "test_u442_second_core_receipt", core_receipt.__file__
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses reads the module while the class body runs; it is removed again below
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module.decode_receipt(N1_REJECTED)


def _refusal_status_unreadable():
    """A reader-refusal subclass whose ``status`` (core reads ``error.code``) raises."""
    refusal = DerivedRefusal(
        core_receipt.ReceiptError("unsupported_version", "test"), dict(N1_FUTURE)
    )
    object.__setattr__(refusal, "error", None)
    return refusal


class _NoDeepcopy:
    def __deepcopy__(self, memo):
        raise RuntimeError("test: the receipt's data cannot be exported")


def _unexportable_receipt():
    """A receipt subclass whose core export (``Receipt.to_dict``, a deepcopy of its data) raises."""
    receipt = DerivedReceipt(_n1_wire())
    object.__setattr__(receipt, "_data", _NoDeepcopy())
    return receipt


class UninspectableMapping(Mapping):
    """A mapping whose membership test raises: the arm rule itself cannot tell it apart."""

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __contains__(self, key):
        raise RuntimeError("test: membership cannot be read")


class ProjectedReceipt(core_receipt.Receipt, Mapping):
    """A receipt whose Mapping view projects an admitted wire; core's own export shows its rejection (r3 F1)."""

    def __iter__(self):
        return iter(_n1_wire())

    def __len__(self):
        return len(_n1_wire())

    def __getitem__(self, key):
        return _n1_wire()[key]


class MappingReceipt(core_receipt.Receipt, Mapping):
    """A receipt whose Mapping view is a plain legacy id shape with no schema_version (r3 F1)."""

    def __iter__(self):
        return iter(("item_id", "queued"))

    def __len__(self):
        return 2

    def __getitem__(self, key):
        return {"item_id": "test-legacy-id", "queued": True}[key]


class StringReceipt(core_receipt.Receipt, str):
    """A receipt that also is a (here empty, so falsy) string (r3 F1)."""


def _named_mapping_receipt():
    class Receipt(
        Mapping
    ):  # not core's: a mapping lookalike projecting an admitted wire
        def __iter__(self):
            return iter(_n1_wire())

        def __len__(self):
            return len(_n1_wire())

        def __getitem__(self, key):
            return _n1_wire()[key]

    return Receipt()


# (label, make, kind). kind is admitted | rejected | refusal | not_save | foreign | uninspectable | refusal_unreadable | unexportable | legacy.
N1_ROWS = [
    (
        "core receipt admitted",
        lambda: core_receipt.decode_receipt(_n1_wire()),
        "admitted",
    ),
    (
        "core receipt rejected",
        lambda: core_receipt.decode_receipt(N1_REJECTED),
        "rejected",
    ),
    ("core reader refusal", lambda: core_receipt.decode_receipt(N1_FUTURE), "refusal"),
    ("subclass admitted", lambda: DerivedReceipt(_n1_wire()), "admitted"),
    (
        "subclass rejected, the reviewer's N1 repro",
        lambda: DerivedReceipt(N1_REJECTED),
        "rejected",
    ),
    (
        "subclass hiding admission_error in to_dict",
        lambda: HidingReceipt(N1_REJECTED),
        "rejected",
    ),
    (
        "subclass with a job subject",
        lambda: DerivedReceipt(_n1_wire("stage_job")),
        "not_save",
    ),
    (
        "subclass of the reader refusal",
        lambda: DerivedRefusal(
            core_receipt.ReceiptError("unsupported_version", "test"), dict(N1_FUTURE)
        ),
        "refusal",
    ),
    ("lookalike class named Receipt", _named_receipt, "foreign"),
    ("lookalike with a base named ReceiptRefusal", _refusal_named_base, "foreign"),
    (
        "rejected receipt from a second copy of core's receipt module",
        _second_core_copy_rejected,
        "foreign",
    ),
    ("mapping proxy wire admitted", lambda: MappingProxyType(_n1_wire()), "admitted"),
    (
        "mapping proxy wire rejected",
        lambda: MappingProxyType(dict(N1_REJECTED)),
        "rejected",
    ),
    ("mapping whose membership test raises", UninspectableMapping, "uninspectable"),
    (
        "reader refusal whose status cannot be read",
        _refusal_status_unreadable,
        "refusal_unreadable",
    ),
    ("receipt whose core export raises", _unexportable_receipt, "unexportable"),
    (
        "receipt that also is a Mapping projecting an admitted wire, r3 F1",
        lambda: ProjectedReceipt(N1_REJECTED),
        "rejected",
    ),
    (
        "receipt that also is a Mapping with no schema_version, r3 F1",
        lambda: MappingReceipt(N1_REJECTED),
        "rejected",
    ),
    (
        "receipt that also is a str, r3 F1",
        lambda: StringReceipt(N1_REJECTED),
        "rejected",
    ),
    (
        "admitted receipt that also is a str, r3 F1",
        lambda: StringReceipt(_n1_wire()),
        "admitted",
    ),
    (
        "lookalike Mapping named Receipt projecting an admitted wire, r3 F1",
        _named_mapping_receipt,
        "foreign",
    ),
    ("legacy id string", lambda: "test-id", "legacy"),
    ("legacy dict", lambda: {"item_id": "test-id", "queued": True}, "legacy"),
    ("legacy int", lambda: 7, "legacy"),
    ("legacy None", lambda: None, "legacy"),
]


def _n1_refused(kind, op):
    """The typed error and the exact WARNING message for a receipt-arm kind that never resolves to an id."""
    return {
        "rejected": (
            SaveRejected,
            f"Save return {op} rejected at admission (idempotency_conflict); nothing was stored under a new id",
        ),
        "refusal": (
            SaveReturnRefused,
            f"Save return {op} refused: receipt refused by the reader (unsupported_version); no id returned",
        ),
        "not_save": (
            SaveReturnRefused,
            f"Save return {op} refused: receipt subject stage_job is not a save; no id returned",
        ),
        "refusal_unreadable": (
            SaveReturnRefused,
            f"Save return {op} refused: receipt refused by the reader (unreadable); no id returned",
        ),
        "unexportable": (
            SaveReturnRefused,
            f"Save return {op} refused: save return is not a readable receipt; no id returned",
        ),
        "uninspectable": (
            SaveReturnRefused,
            f"Save return {op} refused: save return cannot be inspected; no id returned",
        ),
        "foreign": (
            SaveReturnRefused,
            f"Save return {op} refused: save return is named like a receipt but is not this core's receipt; "
            "no id returned",
        ),
    }[kind]


class _Returns:
    """A memory whose ``save`` answers with the row's value."""

    def __init__(self, returned):
        self.returned = returned

    def save(self, request, *, adapter_namespace=None):
        return self.returned


# (consumer label, call). Every consumer of the wrapper's reader, each fed the same row.
N1_CONSUMERS = [
    ("resolve_save_return", lambda value: resolve_save_return(value, op="add").value),
    ("save_value", lambda value: save_value(value, op="add")),
    ("ingest_dict", lambda value: ingest_dict(value, op="add")),
    (
        "submit",
        lambda value: submit(
            _Returns(value), build_save_call(seam="mcp", content="c"), op="add"
        ).value,
    ),
]


@pytest.mark.parametrize("label, make, kind", N1_ROWS, ids=[r[0] for r in N1_ROWS])
def test_every_receipt_subtype_reaches_the_reader_on_every_consumer(
    label, make, kind, caplog
):
    from smartmemory.refusal_logging import refusal_logged

    if kind == "uninspectable":
        with pytest.raises(RuntimeError):
            is_receipt_arm(make())
    else:
        assert is_receipt_arm(make()) is (kind != "legacy"), label
    for consumer, call in N1_CONSUMERS:
        caplog.clear()
        value = make()
        if kind == "admitted":
            got = call(value)
            if consumer == "ingest_dict":
                assert (got["item_id"], got["queued"], got["run_id"]) == (
                    "test_item",
                    True,
                    "test_run",
                )
            else:
                assert got == "test_item", (label, consumer)
            assert _warning_records(caplog) == [], (label, consumer)
        elif (
            kind == "legacy" and consumer == "submit"
        ):  # save() promises a receipt: a legacy return is refused
            with pytest.raises(SaveCallRefused) as caught:
                call(value)
            assert caught.value.code == NOT_A_RECEIPT
            assert _warning_records(caplog) == [
                (
                    "smartmemory_app.save_caller",
                    logging.WARNING,
                    f"Save call refused (not_a_receipt): _Returns.save() returned {type(value).__name__}, not a "
                    "receipt; no id was inferred from it",
                )
            ], label
            assert refusal_logged(caught.value) is True
        elif kind == "legacy":
            assert call(value) == value, (label, consumer)
            assert _warning_records(caplog) == [], (label, consumer)
        else:
            error, message = _n1_refused(kind, "add")
            with pytest.raises(error) as caught:
                call(value)
            assert type(caught.value) is error, (label, consumer)
            assert _warning_records(caplog) == [(LOGGER, logging.WARNING, message)], (
                label,
                consumer,
            )
            assert refusal_logged(caught.value) is True, (label, consumer)


# --- fix round 3 (F2, F3) ---------------------------------------------------------------------------------------
# F2, bug class "re-log of an already-marked failure": a reader catch whose cause already carries core's logged mark
# raises the typed refusal chained from it with no WARNING of its own, so the whole failure leaves ONE WARNING (the
# dependency's). One row per catch site of resolve_save_return. F3, bug class "uncertainty read as legacy success": an
# inspection failure ends in the typed, once-logged refusal on the FIRST failure and is never inspected again.
DEPENDENCY_WARNING = "test dependency refused; receipt inspection unavailable"
DEPENDENCY_RECORD = ("test_dependency", logging.WARNING, DEPENDENCY_WARNING)


def _dependency_fails(*_args, **_kwargs):
    """A dependency that logs its one WARNING, marks the refusal it raises as logged, and raises."""
    from smartmemory.refusal_logging import mark_refusal_logged

    logging.getLogger("test_dependency").warning(DEPENDENCY_WARNING)
    raise mark_refusal_logged(RuntimeError("test dependency unavailable"))


class MarkedMapping(Mapping):
    """A mapping whose membership test (the arm rule) fails with a marked refusal."""

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 1

    def __getitem__(self, key):
        _dependency_fails()


def _patch_core_export(mp):
    mp.setattr(core_receipt.Receipt, "to_dict", _dependency_fails)


def _patch_codec(mp):
    mp.setattr(core_receipt, "decode_receipt", _dependency_fails)


def _patch_codec_validation(mp):
    from smartmemory.refusal_logging import mark_refusal_logged

    def fails(wire):
        logging.getLogger("test_dependency").warning(DEPENDENCY_WARNING)
        raise mark_refusal_logged(core_receipt.ReceiptValidationError([]))

    mp.setattr(core_receipt, "decode_receipt", fails)


def _patch_refusal_status(mp):
    mp.setattr(core_receipt.ReceiptRefusal, "status", property(_dependency_fails))


def _patch_receipt_text(mp):
    mp.setattr(core_receipt.Receipt, "__str__", _dependency_fails)


def _no_patch(mp):
    return None


# (site, make, patch, the reader line that catches). One row per except of resolve_save_return that has a cause.
MARKED_ROWS = [
    ("arm check (is_receipt_arm)", MarkedMapping, _no_patch),
    (
        "core export of a receipt object",
        lambda: core_receipt.Receipt(N1_REJECTED),
        _patch_core_export,
    ),
    ("codec raises (generic catch)", lambda: dict(N1_REJECTED), _patch_codec),
    (
        "codec raises a validation error",
        lambda: dict(N1_REJECTED),
        _patch_codec_validation,
    ),
    (
        "status of a reader refusal object",
        lambda: core_receipt.decode_receipt(N1_FUTURE),
        _patch_refusal_status,
    ),
    (
        "status of a decoded reader refusal",
        lambda: dict(N1_FUTURE),
        _patch_refusal_status,
    ),
    ("export of the decoded receipt", _n1_wire, _patch_core_export),
    ("summary of an admitted receipt", _n1_wire, _patch_receipt_text),
]


@pytest.mark.parametrize(
    "label, make, patch", MARKED_ROWS, ids=[r[0] for r in MARKED_ROWS]
)
def test_a_marked_cause_is_refused_with_no_second_warning_on_every_consumer(
    label, make, patch, monkeypatch, caplog
):
    from smartmemory.refusal_logging import refusal_logged

    value = make()
    patch(monkeypatch)
    for consumer, call in N1_CONSUMERS:
        caplog.clear()
        with pytest.raises(SaveReturnRefused) as caught:
            call(value)
        assert type(caught.value) is SaveReturnRefused, (label, consumer)
        assert _warning_records(caplog) == [DEPENDENCY_RECORD], (label, consumer)
        assert refusal_logged(caught.value) is True, (label, consumer)
        assert refusal_logged(caught.value.__cause__) is True, (label, consumer)


class FlakyMapping(Mapping):
    """The first membership test fails, every later one succeeds without schema_version (the r3 F3 repro)."""

    def __init__(self):
        self.calls = 0

    def __iter__(self):
        return iter(("item_id",))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        if key == "item_id":
            return "test-legacy-id"
        raise KeyError(key)

    def __contains__(self, key):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("test inspection unavailable")
        return False


def test_a_failed_inspection_is_one_typed_refusal_and_is_never_inspected_again(caplog):
    from smartmemory.refusal_logging import refusal_logged

    for consumer, call in N1_CONSUMERS:
        caplog.clear()
        value = FlakyMapping()
        with pytest.raises(SaveReturnRefused) as caught:
            call(value)
        assert value.calls == 1, consumer
        assert _warning_records(caplog) == [
            (
                LOGGER,
                logging.WARNING,
                "Save return add refused: save return cannot be inspected; no id returned",
            )
        ], consumer
        assert refusal_logged(caught.value) is True, consumer


def test_a_core_without_refusal_logging_still_refuses_once_unmarked(
    monkeypatch, caplog
):
    """The ImportError arms of the mark (``_logged``, ``_cause_logged``): no mark module means no boundary that reads one, never a lost refusal."""
    monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    with caplog.at_level(logging.WARNING), pytest.raises(SaveReturnRefused) as caught:
        resolve_save_return(FlakyMapping(), op="add")
    assert not any("logged" in key for key in vars(caught.value))
    assert _warning_records(caplog) == [
        (
            LOGGER,
            logging.WARNING,
            "Save return add refused: save return cannot be inspected; no id returned",
        )
    ]


def _warning_records(caplog):
    return [
        (r.name, r.levelno, r.getMessage())
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ]


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
    # save_caller.py (U4.42) reads the receipt core's save() returns through the same resolver
    assert importers == {"storage.py", "save_caller.py"}


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
def ingest_callers(root, allowed_dynamic=()):
    """Every reference to ``smartmemory_app.storage.ingest`` under ``root``, resolved through the file's imports:
    the bare imported name, an ``as`` alias, ``import M``/``import M as x``, the package attribute chain, a
    relative import, a dynamic import with a constant module name (``importlib.import_module``/``__import__``
    under any alias, as a call receiver), then any dotted chain ending in ``.ingest`` whose root resolves to the
    module.

    Returns ``{(file relative to root, enclosing function, how the return value is used)}``. The scan fails closed:
    a reference it cannot follow (``getattr``, the module or ``ingest`` itself escaping as a value, a star import,
    a string naming the module, a dynamic import whose name is not a constant, the dynamic-import functions or
    ``importlib``/``sys``/``builtins`` escaping as values, ``sys.modules`` used other than by a constant key)
    raises AssertionError naming file:line. ``allowed_dynamic`` is a set of ``(file, function)`` pairs (``<module>``
    for module level) whose module-name strings and non-constant dynamic imports are known not to load the wrapper.
    """
    import ast
    from pathlib import Path

    module, target = "smartmemory_app.storage", "smartmemory_app.storage.ingest"
    tracked = {"smartmemory_app", module, target}
    loaders = {
        "importlib.import_module",
        "importlib.__import__",
        "builtins.__import__",
        "__import__",
    }
    surface = {"importlib", "sys", "builtins"}
    root = Path(root)
    allowed = set(allowed_dynamic)
    found, unresolved = set(), []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        tree = ast.parse(path.read_text())
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        package = [root.name, *rel.parent.parts]
        aliases = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    bound = a.asname or a.name.split(".")[0]
                    aliases.setdefault(bound, set()).add(a.asname and a.name or bound)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    base = ".".join(
                        [
                            *package[: len(package) - n.level + 1],
                            *filter(None, [n.module]),
                        ]
                    )
                for a in n.names:
                    if a.name == "*":
                        if base in tracked or base in surface:
                            unresolved.append(
                                f"{rel}:{n.lineno}: star import from {base}"
                            )
                        continue
                    aliases.setdefault(a.asname or a.name, set()).add(
                        f"{base}.{a.name}"
                    )

        def loaded(call):
            """What a dynamic import with a constant, absolute module name returns (empty when unreadable)."""
            arg = call.args[0] if call.args else None
            arg = arg or next((k.value for k in call.keywords if k.arg == "name"), None)
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                return set()
            if not arg.value or arg.value.startswith("."):
                return set()
            if resolve(call.func) & {"importlib.import_module"}:
                return {arg.value}
            return {
                arg.value,
                arg.value.split(".")[0],
            }  # __import__ without fromlist returns the top package

        def resolve(node):
            if isinstance(node, ast.Name):
                if node.id == "__import__" and node.id not in aliases:
                    return {"__import__"}
                return aliases.get(node.id, set())
            if isinstance(node, ast.Attribute):
                return {f"{d}.{node.attr}" for d in resolve(node.value)}
            if isinstance(node, ast.Call) and resolve(node.func) & loaders:
                return loaded(node)
            return set()

        def enclosing(node):
            while node in parents and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                node = parents[node]
            return node

        for n in ast.walk(tree):
            p = parents.get(n)
            if (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and (
                    n.value == module
                    or n.value.startswith((module + ".", module + ":"))
                )
                and (str(rel), getattr(enclosing(n), "name", "<module>")) not in allowed
                and not (  # the name of a dynamic import that resolves is enumerated, not a blind reference
                    isinstance(p, ast.Call)
                    and resolve(p.func) & loaders
                    and p.args[:1] == [n]
                )
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: string {n.value!r} (dynamic lookup)"
                )
            elif (
                isinstance(n, ast.Subscript)
                and ast.unparse(n.value).endswith("modules")
                and isinstance(n.slice, ast.Constant)
                and n.slice.value in tracked
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic lookup)"
                )
            elif (
                isinstance(n, ast.Call)
                and ast.unparse(n.func) in ("getattr", "builtins.getattr")
                and len(n.args) > 1
                and isinstance(n.args[1], ast.Constant)
                and n.args[1].value == "ingest"
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: getattr(..., 'ingest') on an unresolved receiver"
                )
            elif isinstance(n, ast.Call) and (
                resolve(n.func) & loaders
                or ast.unparse(n.func).split(".")[-1] in ("import_module", "__import__")
            ):
                got = loaded(n)
                first = n.args[0] if n.args else None
                if not resolve(n.func) & loaders:
                    if (
                        isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and first.value in tracked
                    ):
                        unresolved.append(
                            f"{rel}:{n.lineno}: dynamic import of {first.value} on an unresolved receiver"
                        )
                elif not got:
                    if (
                        str(rel),
                        getattr(enclosing(n), "name", "<module>"),
                    ) not in allowed:
                        unresolved.append(
                            f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import of a name the scan cannot read)"
                        )
                elif got & tracked and not (
                    isinstance(p, ast.Attribute) and p.value is n
                ):
                    unresolved.append(
                        f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import of {module} or its package used as a value)"
                    )
            if not isinstance(n, (ast.Name, ast.Attribute)) or not isinstance(
                n.ctx, ast.Load
            ):
                continue
            refs = resolve(n)
            if "sys.modules" in refs:
                key = p.slice if isinstance(p, ast.Subscript) and p.value is n else None
                if not (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.split(".")[0] != "smartmemory_app"
                ):
                    unresolved.append(
                        f"{rel}:{n.lineno}: {ast.unparse(n)} (sys.modules used other than by a constant key outside the package)"
                    )
            if refs & surface and not (isinstance(p, ast.Attribute) and p.value is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (import machinery escaping as a value)"
                )
            if refs & loaders and not (isinstance(p, ast.Call) and p.func is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import function referenced as a value, not called)"
                )
            if isinstance(p, ast.Attribute) and p.value is n:
                continue  # the chain continues; judged at its end
            hits = refs & tracked
            if not hits:
                continue
            if target not in hits:
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} ({module} or its package) used as a value"
                )
                continue
            if not (isinstance(p, ast.Call) and p.func is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} referenced as a value, not called"
                )
                continue
            fn = enclosing(p)
            use_of = parents.get(p)
            use = (
                "discarded"
                if isinstance(use_of, ast.Expr)
                else "returned"
                if isinstance(use_of, ast.Return)
                else "assigned to " + ast.unparse(use_of.targets[0])
                if isinstance(use_of, ast.Assign)
                else "argument of " + ast.unparse(use_of.func)
                if isinstance(use_of, ast.Call)
                else "used in " + type(use_of).__name__
            )
            found.add((str(rel), getattr(fn, "name", "<module>"), use))
    assert not unresolved, (
        "storage.ingest references the census cannot resolve: " + "; ".join(unresolved)
    )
    return found


#: One row per consumer. A new caller of ``storage.ingest`` fails this table until its reading of the return is
#: stated: it either discards the value, or reads it as an id string, or reads ``["item_id"]`` of the dict.
INGEST_CONSUMER_ROWS = [
    # CORE-HOLD-RECEIPT-1 (forward-merged from main): every site assigns the return to ``receipt`` and checks for a
    # held dict (``status == "held"``) before using it, so a terminal hold is never read as an id.
    (
        "cli.py",
        "add_cmd",
        "assigned to receipt",
        "reads a held dict's status first, else an id string (sync)",
    ),
    (
        "lifecycle.py",
        "observe",
        "assigned to receipt",
        "reads a held dict's status first, else discards the value",
    ),
    (
        "lifecycle.py",
        "distill",
        "assigned to receipt",
        "reads a held dict's status first, else discards the value",
    ),
    (
        "lifecycle.py",
        "learn",
        "assigned to receipt",
        "reads a held dict's status first, else discards the value",
    ),
    (
        "local_api.py",
        "ingest_endpoint",
        "assigned to result",
        "reads item_id of the dict, or an id string (async)",
    ),
]


#: The only dynamic loading in the wrapper that is not a ``storage.ingest`` consumer: ``work_graph.py`` names core
#: modules (and one wrapper helper, ``_resolve_data_dir``) in the ``STATUS_IMPORTS`` table and imports them by name.
#: The test below proves the table never names ``ingest``, which is what makes the allowance sound.
DYNAMIC_IMPORT_ALLOWED = {
    ("work_graph.py", "<module>"),
    ("work_graph.py", "_core_names"),
    # Forward-merged from main (FORWARD-MERGE-1): imports the fixed native libraries ``usearch.index`` and ``numpy`` by
    # loop variable, never the wrapper.
    ("install_check.py", "native_library_checks"),
}


def test_every_wrapper_ingest_consumer_is_assessed():
    from smartmemory_app.work_graph import STATUS_IMPORTS

    assert not [row for row in STATUS_IMPORTS if row[1] == "ingest"]
    root = Path(save_return.__file__).parent
    assert ingest_callers(root, DYNAMIC_IMPORT_ALLOWED) == {
        row[:3] for row in INGEST_CONSUMER_ROWS
    }


# -- the census helper itself, over every import form (review r2 F3): in-memory source, no repository file written ----
CALLER = {("test_scan.py", "caller", "returned")}
CENSUS_ROWS = [
    # (name, source of one file, package name of the scan root, consumers found or the file:line it must fail on)
    (
        "bare imported name",
        "from smartmemory_app.storage import ingest\n\ndef caller():\n    return ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "imported name alias",
        "from smartmemory_app.storage import ingest as save\n\ndef caller():\n    return save('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import module, fully dotted call",
        "import smartmemory_app.storage\n\ndef caller():\n    return smartmemory_app.storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import module as alias",
        "import smartmemory_app.storage as st\n\ndef caller():\n    return st.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import package, attribute chain",
        "import smartmemory_app\n\ndef caller():\n    return smartmemory_app.storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "from package import module",
        "from smartmemory_app import storage\n\ndef caller():\n    return storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "from package import module as alias",
        "from smartmemory_app import storage as s\n\ndef caller():\n    return s.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "relative import of the name",
        "from .storage import ingest\n\ndef caller():\n    return ingest('test')\n",
        "smartmemory_app",
        CALLER,
    ),
    (
        "relative import of the module",
        "from . import storage\n\ndef caller():\n    x = storage.ingest('test')\n",
        "smartmemory_app",
        {("test_scan.py", "caller", "assigned to x")},
    ),
    (
        "getattr with a string on the module",
        "from smartmemory_app import storage\n\ndef caller():\n    return getattr(storage, 'ingest')('test')\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "getattr with a string on an unknown receiver",
        "def caller(mod):\n    return getattr(mod, 'ingest')('test')\n",
        "test_scan_pkg",
        "test_scan.py:2: ",
    ),
    (
        "module alias made by assignment",
        "from smartmemory_app import storage\n\ndef caller():\n    st = storage\n    return st.ingest('test')\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import of the module, constant name (resolved since review r3 F3)",
        "import importlib\n\ndef caller():\n    return importlib.import_module('smartmemory_app.storage').ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "ingest passed on as a value",
        "from smartmemory_app import storage\n\ndef caller(run):\n    return run(storage.ingest)\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "star import of the module",
        "from smartmemory_app.storage import *\n\ndef caller():\n    return ingest('test')\n",
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "aliased import_module, call receiver (review r3 F3)",
        'from importlib import import_module as load\n\ndef caller():\n    return load("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "aliased __import__ through builtins, fromlist",
        'from builtins import __import__ as imp\n\ndef caller():\n    return imp("smartmemory_app.storage", fromlist=["ingest"]).ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import importlib as il, il.import_module",
        'import importlib as il\n\ndef caller():\n    return il.import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "call receiver chain through the package",
        'import importlib\n\ndef caller():\n    return importlib.import_module("smartmemory_app").storage.ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "sys.modules aliased by from-import",
        'from sys import modules as registry\n\ndef caller():\n    return registry["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "sys.modules held in a variable through import sys as s",
        'import sys as s\n\ndef caller():\n    table = s.modules\n    return table["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "sys.modules read without a constant key",
        'import sys\n\ndef caller():\n    return sys.modules.get("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "module name as a string only",
        'NAME = "smartmemory_app.storage"\n',
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "dynamic import with a name read at run time",
        'import importlib\n\ndef caller(name):\n    return importlib.import_module(name).ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import with a relative name",
        'import importlib\n\ndef caller():\n    return importlib.import_module(".storage", "smartmemory_app").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "aliased dynamic import result held in a variable",
        'from importlib import import_module as load\n\ndef caller():\n    m = load("smartmemory_app.storage")\n    return m.ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "importlib itself held in a variable",
        'import importlib\n\ndef caller():\n    loader = importlib\n    return loader.import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import function held as a value",
        'from importlib import import_module\n\ndef caller():\n    load = import_module\n    return load("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "star import of importlib",
        'from importlib import *\n\ndef caller():\n    return import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "getattr on sys passes the registry on",
        'import sys\n\ndef caller():\n    return getattr(sys, "modules")["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
]


@pytest.mark.parametrize(
    "source, package, expected",
    [row[1:] for row in CENSUS_ROWS],
    ids=[row[0] for row in CENSUS_ROWS],
)
def test_census_resolves_every_import_form_or_fails_naming_the_line(
    monkeypatch, source, package, expected
):
    import re
    from pathlib import Path

    root = Path(package)
    with monkeypatch.context() as mp:
        mp.setattr(Path, "rglob", lambda self, pattern: [root / "test_scan.py"])
        mp.setattr(Path, "read_text", lambda self, *a, **k: source)
        if isinstance(expected, str):
            with pytest.raises(AssertionError, match=re.escape(expected)):
                ingest_callers(root)
            return
        found = ingest_callers(root)
    assert found == expected


def test_census_allows_a_named_dynamic_import_and_no_other(monkeypatch):
    import re
    from pathlib import Path

    source = "import importlib\n\ndef loader(name):\n    return importlib.import_module(name)\n"
    root = Path("test_scan_pkg")
    with monkeypatch.context() as mp:
        mp.setattr(Path, "rglob", lambda self, pattern: [root / "test_scan.py"])
        mp.setattr(Path, "read_text", lambda self, *a, **k: source)
        assert ingest_callers(root, {("test_scan.py", "loader")}) == set()
        for allowed in ((), {("test_scan.py", "other")}, {("other.py", "loader")}):
            with pytest.raises(AssertionError, match=re.escape("test_scan.py:4: ")):
                ingest_callers(root, allowed)


#: sha256 of ``ingest_callers`` source. The wrapper and the MCP package each hold a copy (the repositories cannot
#: import each other), and both tests pin this one hash, so the copies cannot drift apart unnoticed.
INGEST_CALLERS_SHA256 = (
    "5b79b81b0da964c53a498925b7f3ae396d00c8d1477ca687628e1a0d24d8888e"
)


def test_census_helper_copy_matches_its_twin():
    import hashlib
    import inspect

    digest = hashlib.sha256(inspect.getsource(ingest_callers).encode()).hexdigest()
    assert digest == INGEST_CALLERS_SHA256, (
        "ingest_callers changed: update the copy in the other repository and this hash together"
    )


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
def test_cli_add_prints_the_id_on_both_arms(lite_ingest, result, expected, monkeypatch):
    from unittest.mock import patch

    from click.testing import CliRunner

    from smartmemory_app.cli import cli

    lite_ingest(result)
    monkeypatch.setenv("SMARTMEMORY_QUIET", "1")  # CLI-WORKSPACE-UX-1: header off
    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.cli._prepare_direct_access"),
    ):
        single = CliRunner().invoke(cli, ["add", "test content"])
        piped = CliRunner().invoke(cli, ["add", "-"], input="test one\ntest two\n")
    assert single.exit_code == 0 and single.output.strip() == expected
    assert piped.exit_code == 0
    assert piped.output.split() == ["Added", "2", "memories", expected, expected]
