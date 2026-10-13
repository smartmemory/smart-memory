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

No switch: nothing under the legacy save path reaches the receipt arm until core returns a receipt, and
``str`` and legacy-dict returns never leave the first branch of ``resolve_save_return``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

log = logging.getLogger(__name__)

#: Receipt subject kinds a save doorway can return. Occurrence and job subjects name no stored memory.
SAVE_SUBJECT_KINDS = frozenset(
    {"item_save", "document_save", "generation_save", "edit"}
)


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


def _refuse(op: str, reason: str, cause: BaseException | None = None) -> NoReturn:
    log.warning("Save return %s refused: %s; no id returned", op, reason)
    raise SaveReturnRefused(reason) from cause


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
    if isinstance(value, str) or (
        isinstance(value, Mapping) and "schema_version" not in value
    ):
        return SaveReturn(value)
    try:
        from smartmemory.save.receipt import (
            Receipt,
            ReceiptRefusal,
            ReceiptValidationError,
            decode_receipt,
        )
    except ImportError as exc:
        if isinstance(
            value, Mapping
        ):  # a receipt wire dict that this core cannot read is never an id or a legacy dict
            _refuse(op, "the installed core cannot read save receipts", exc)
        return SaveReturn(
            value
        )  # a core without receipts has no receipt object, so any other value is legacy
    if isinstance(value, Mapping):
        try:
            value = decode_receipt(value)
        except ReceiptValidationError as exc:
            _refuse(op, "save return is not a valid receipt", exc)
    if isinstance(value, ReceiptRefusal):
        _refuse(op, f"receipt refused by the reader ({value.status})")
    if not isinstance(value, Receipt):
        return SaveReturn(value)
    wire = value.to_dict()
    error = wire.get("admission_error")
    if error is not None:
        log.warning(
            "Save return %s rejected at admission (%s); nothing was stored under a new id",
            op,
            error["code"],
        )
        raise SaveRejected(error["code"], error["message"])
    kind = (wire.get("subject") or {}).get("kind")
    if kind is not None and kind not in SAVE_SUBJECT_KINDS:
        _refuse(op, f"receipt subject {kind} is not a save")
    subject_id = value.item_id or value.document_id or value.generation_id
    if not subject_id:
        _refuse(op, "receipt names no stored subject id")
    return SaveReturn(subject_id, wire, _summary(value, wire, subject_id, op))


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
