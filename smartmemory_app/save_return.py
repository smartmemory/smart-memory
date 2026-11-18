"""Receipt-or-legacy save return view for local callers (N-02.activate.outer-readers; G52, G98, G137).

Core's save doorways return a bare item id string today. After the producer flip they may return a versioned
save receipt (``smartmemory.save.receipt.Receipt``) or its wire dict. Local callers that read the return as
an id (the MCP tools format ``Item ID: <id>``) must keep working across that flip, so this module gives them
one view of either arm:

- legacy arm: a ``str`` id, or a dict without ``schema_version`` (the queued ``{"item_id", "queued"}`` and the
  consolidation ``{"status": "held", ...}`` shapes), is returned unchanged and nothing is imported;
- receipt arm: an admitted ``Receipt`` (or its wire dict) resolves to its subject id, with the wire receipt
  kept beside it; a receipt carrying ``admission_error`` raises ``SaveRejected`` and a reader refusal or an
  invalid wire dict raises ``SaveReturnRefused``, each with one WARNING. A rejected or refused return is never
  turned into an id, never ``str(receipt)`` (``"<id> (<status>)"`` is not an id).

``is_receipt_arm`` is the one rule that tells the arms apart (U4.42 r2 N1). Receipt identity is decided before the
``str``/``Mapping`` shape (r3 F1), so a receipt that also is a string or a mapping is still a receipt. It is never an
exact class-name equality: any instance of core's ``Receipt``/``ReceiptRefusal`` or of a subclass, any object whose class or a base
is named like one (a second copy of core, a reloaded module), and any mapping with ``schema_version`` is the receipt
arm. A receipt-arm object that is not this core's receipt is refused, never read as legacy. Every receipt is read
through core's own export and codec (``Receipt.to_dict`` then ``decode_receipt``), so a subclass cannot hide its
``admission_error`` by overriding a method or by projecting a mapping view. A failure whose cause already carries
core's logged mark is refused without a second WARNING (r3 F2). ``smartmemory_mcp.backends.local._is_receipt_arm`` and the service's
``cli.ingest_return._is_receipt_arm`` mirror this rule; their tests run the same rows.

No switch: nothing under the legacy save path reaches the receipt arm until core returns a receipt, and
``str`` and legacy-dict returns never leave the first branch of ``resolve_save_return``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

log = logging.getLogger(__name__)

#: Receipt subject kinds a save doorway can return. Occurrence and job subjects name no stored memory.
SAVE_SUBJECT_KINDS = frozenset(
    {"item_save", "document_save", "generation_save", "edit"}
)

#: Core's receipt classes, by name. An object whose class or a base has one of these names is the receipt arm.
RECEIPT_CLASS_NAMES = frozenset({"Receipt", "ReceiptRefusal"})


def named_like_receipt(value: Any) -> bool:
    """Whether the class of ``value`` or one of its bases is named ``Receipt``/``ReceiptRefusal`` (core's or a lookalike)."""
    return any(
        getattr(cls, "__name__", None) in RECEIPT_CLASS_NAMES
        for cls in type(value).__mro__
    )


def is_receipt_arm(value: Any) -> bool:
    """Whether a save return is the receipt arm (a receipt object or its wire mapping) rather than a legacy shape.

    Receipt identity is decided FIRST (U4.42 r3 F1): an object whose class or a base is named ``Receipt``/
    ``ReceiptRefusal``, or that is an instance of the classes of an already-imported ``smartmemory.save.receipt``
    (read from ``sys.modules``, so the legacy arm never imports core), is a receipt even when it also is a ``str`` or a
    ``Mapping``. Only then: ``str`` and a mapping without ``schema_version`` are legacy, a mapping with it is a wire
    receipt. Everything else is legacy.
    """
    if named_like_receipt(value):
        return True
    try:  # a constant-key read: the U4.32 import census admits only that form of sys.modules use
        module = sys.modules["smartmemory.save.receipt"]
    except KeyError:
        module = None
    classes = tuple(
        cls
        for cls in (
            getattr(module, "Receipt", None),
            getattr(module, "ReceiptRefusal", None),
        )
        if isinstance(cls, type)
    )
    if classes and isinstance(value, classes):
        return True
    if isinstance(value, str):
        return False
    if isinstance(value, Mapping):
        return "schema_version" in value
    return False


class SaveReturnError(RuntimeError):
    """Base of the typed refusals. ``code`` is the stable machine name."""

    code = "save_return_error"


class SaveRejected(SaveReturnError):
    """The save was refused at admission (revision conflict, idempotency conflict, retry key expired)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SaveReturnRefused(SaveReturnError):
    """A save return that cannot be read as an id or a legacy shape. Nothing was inferred."""

    code = "save_return_refused"


@dataclass(frozen=True)
class SaveReturn:
    """``value`` is what the legacy callers read (an id string, or the legacy dict). ``receipt`` is the wire
    receipt on the receipt arm and None on the legacy arm. ``summary`` is one human line, receipt arm only."""

    value: Any
    receipt: dict[str, Any] | None = None
    summary: str | None = None


def _logged(exc: SaveReturnError) -> SaveReturnError:
    """Mark ``exc`` logged with core's ``mark_refusal_logged``, so a boundary above that reads the mark does not log it again.

    A core without ``smartmemory.refusal_logging`` has no such boundary: ``exc`` is raised unmarked, still after its one WARNING.
    """
    try:
        from smartmemory.refusal_logging import mark_refusal_logged
    except ImportError:
        return exc
    return mark_refusal_logged(exc)


def _cause_logged(cause: BaseException | None) -> bool:
    """Whether ``cause`` already carries core's logged mark (``refusal_logged``): its writer logged the one WARNING."""
    if cause is None:
        return False
    try:
        from smartmemory.refusal_logging import refusal_logged
    except ImportError:  # a core without the mark has no marked cause
        return False
    return refusal_logged(cause)


def _refuse(op: str, reason: str, cause: BaseException | None = None) -> NoReturn:
    """Raise the typed, marked refusal. It logs its one WARNING unless ``cause`` was already logged and marked (U4.42 r3 F2)."""
    if not _cause_logged(cause):
        log.warning("Save return %s refused: %s; no id returned", op, reason)
    raise _logged(SaveReturnRefused(reason)) from cause


def refuse_uninspectable(op: str, cause: BaseException) -> NoReturn:
    """The refusal for a return whose arm could not be told apart. Every reader ends an inspection failure here, once."""
    _refuse(op, "save return cannot be inspected", cause)


def _summary(receipt: Any, wire: dict[str, Any], subject_id: str, op: str) -> str:
    """G98: a save that skipped extraction reads "Saved, extraction skipped" through core's own badge."""
    subject = wire.get("subject") or {}
    if (
        wire.get("terminal") is True
        and wire.get("outcome") == "skipped"
        and wire.get("processing_stage") == "stored"
        and wire.get("execution_state") == "completed"
    ):
        try:
            from smartmemory.save.status import (
                ExecutionState,
                ProcessingOutcome,
                ProcessingStage,
                ProcessingStatus,
                status_badge,
            )
        except ImportError as exc:  # a receipt exists, so core has receipts: a missing badge reader is a refusal
            _refuse(op, "the installed core cannot read save status badges", exc)

        badge = status_badge(
            ProcessingStatus(
                subject_kind=subject.get("kind"),
                processing_stage=ProcessingStage.STORED,
                execution_state=ExecutionState.COMPLETED,
                outcome=ProcessingOutcome.SKIPPED,
                extraction_skipped=True,
            )
        )
        return f"{subject_id} ({badge})"
    return str(receipt)


def resolve_save_return(value: Any, *, op: str) -> SaveReturn:
    """The receipt-or-legacy view of one save return. ``op`` names the doorway in refusal logs."""
    try:
        receipt_arm = is_receipt_arm(value)
    except Exception as exc:  # a return that cannot even be told apart is never an id or a legacy value
        refuse_uninspectable(op, exc)
    if not receipt_arm:
        return SaveReturn(value)
    try:
        from smartmemory.save.receipt import (
            Receipt,
            ReceiptRefusal,
            ReceiptValidationError,
            decode_receipt,
        )
    except ImportError as exc:  # a receipt exists, so a core that cannot read one is a refusal, never legacy
        _refuse(op, "the installed core cannot read save receipts", exc)
    if isinstance(value, ReceiptRefusal):
        status, cause = _refusal_status(value)
        _refuse(op, f"receipt refused by the reader ({status})", cause)
    if isinstance(value, Receipt):
        try:  # core's own export (class lookup, never the instance), so no override or Mapping view can drop admission_error
            source = Receipt.to_dict(value)
        except Exception as exc:
            _refuse(op, "save return is not a readable receipt", exc)
    elif isinstance(value, Mapping) and not named_like_receipt(value):
        source = value
    else:  # named like a receipt but not this core's (a lookalike, a second copy of core): never legacy success
        _refuse(
            op, "save return is named like a receipt but is not this core's receipt"
        )
    try:
        decoded = decode_receipt(source)
    except ReceiptValidationError as exc:
        _refuse(op, "save return is not a valid receipt", exc)
    except Exception as exc:  # any other codec failure is uncertainty, never an id
        _refuse(op, "save return is not a readable receipt", exc)
    if isinstance(decoded, ReceiptRefusal):
        status, cause = _refusal_status(decoded)
        _refuse(op, f"receipt refused by the reader ({status})", cause)
    try:
        wire = decoded.to_dict()
    except (
        Exception
    ) as exc:  # a decoded receipt that cannot export is uncertainty, never an id
        _refuse(op, "save return is not a readable receipt", exc)
    error = wire.get("admission_error")
    if error is not None:
        log.warning(
            "Save return %s rejected at admission (%s); nothing was stored under a new id",
            op,
            error["code"],
        )
        raise _logged(SaveRejected(error["code"], error["message"]))
    kind = (wire.get("subject") or {}).get("kind")
    if kind is not None and kind not in SAVE_SUBJECT_KINDS:
        _refuse(op, f"receipt subject {kind} is not a save")
    subject_id = decoded.item_id or decoded.document_id or decoded.generation_id
    if not subject_id:
        _refuse(op, "receipt names no stored subject id")
    try:
        summary = _summary(decoded, wire, subject_id, op)
    except SaveReturnError:  # _summary's own typed refusal, already logged and marked
        raise
    except (
        Exception
    ) as exc:  # a summary that cannot be rendered is uncertainty, never an id
        _refuse(op, "save return summary is not readable", exc)
    return SaveReturn(subject_id, wire, summary)


def _refusal_status(value: Any) -> tuple[str, BaseException | None]:
    """The refusal code of a ``ReceiptRefusal`` (or subclass) read through core's own property, and why it could not be.

    ``("unreadable", cause)`` when the property raises or answers a non-string: a refusal whose code cannot be read is
    still a refusal. ``cause`` is the property's exception, so a marked one is not logged again.
    """
    from smartmemory.save.receipt import ReceiptRefusal

    try:
        status = ReceiptRefusal.status.fget(value)
    except Exception as exc:
        return "unreadable", exc
    return (status, None) if type(status) is str else ("unreadable", None)


def save_value(value: Any, *, op: str) -> Any:
    """The legacy view only: an id string for a receipt, every legacy shape unchanged."""
    return resolve_save_return(value, op=op).value


def ingest_dict(value: Any, *, op: str) -> Any:
    """The legacy ``ingest(sync=False)`` dict view: ``{"item_id", "queued", ...}`` for a receipt, else unchanged.

    ``receipt`` and ``run_id`` are additive keys, so a reader that only knows ``item_id`` and ``queued`` keeps
    working. A terminal receipt is not queued.
    """
    resolved = resolve_save_return(value, op=op)
    if resolved.receipt is None:
        return value
    wire = resolved.receipt
    return {
        "item_id": resolved.value,
        "queued": wire.get("terminal") is not True,
        "run_id": wire.get("run_id"),
        "receipt": wire,
    }
