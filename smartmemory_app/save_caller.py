"""Caller seam for the receipt arm of a local save (C-01.activate.save.local-adapters; G52, G55, G56, G98, G158).

``save_return`` reads what core's legacy doorways (``add``/``ingest``/``ingest_structured``) hand back. This module is
the other half: the one place a first-party local caller builds a ``SmartMemory.save`` request, with an explicit
``wait`` and a stable retry identity, and reads the receipt that comes back.

* **Retry identity (G55/G158).** ``retry_key`` carries a caller-supplied key exactly as given, or mints one
  (``<seam>:<uuid4 hex>``). A ``SaveCall`` holds the key, so submitting the same call again sends the same key and
  the same request: core's retry binding then replays the original receipt instead of storing twice. A supplied key
  is never judged here (``""`` is not absence and does not mint): core's normalizer owns the key rule, so a bad
  key is core's typed ``SaveRequestError`` with its one WARNING.
* **Explicit waits (G56/G98).** ``wait`` is ``"stored"``, ``"extracted"`` or omitted. It is passed through as given.
  An omitted wait is omitted from the request (a JSON null is not absence to core). A wait the save can never reach
  (``extracted`` with extraction off) is core's typed ``InvalidWait``, carrying the receipt, and leaves unchanged.
* **No fallback (G50).** The seam only calls ``SmartMemory.save``. Core refuses it with ``SaveNotEnabled`` until the
  Lite common-receipt facade is enabled, and that refusal leaves unchanged. A memory with no ``save`` at all (an older
  core, the hosted ``RemoteMemory``, a ``SecureSmartMemory`` without the delegate) is one typed ``SaveCallRefused``
  with one WARNING. Neither ever becomes ``add`` or ``ingest``.
* **A stable retry (G55/G158, U4.42 r2 N2).** The request is rebuilt once, at build time, from exact plain-JSON types
  (``dict``, ``list``, ``tuple``, ``str``, ``int``, ``float``, ``bool``, ``None``) by an explicit walk that never calls a
  copy hook, and held as ``marshal`` bytes. Every read and every submission decodes those bytes afresh, so nothing the
  caller or a receiver holds can reach the retry. A value that is not plain JSON (core refuses it anyway) is one typed
  ``SaveCallRefused`` (``uncopyable_request``) at build time.
* **One reader.** The receipt core returns is read by ``save_return.resolve_save_return``, so a rejected admission
  (``idempotency_conflict``, ``revision_conflict``, ``retry_key_expired``) is ``SaveRejected`` and a receipt that is
  not a save is ``SaveReturnRefused``. A timed-out wait (G173) comes back as a normal receipt whose wire carries
  ``client_timed_out: true``: it is kept in ``SaveReturn.receipt``, never read as failed or as complete.

No switch: nothing on the legacy save path imports this module (``tests/unit/test_save_caller.py`` scans for that),
and the receipt only exists once core's facade is enabled.
"""

from __future__ import annotations

import logging
import marshal
import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from smartmemory_app.save_return import SaveReturn, resolve_save_return

log = logging.getLogger(__name__)

#: A seam name is the key prefix, so it must itself be inside core's key alphabet (no ``:``, which separates it).
_SEAM = re.compile(r"[A-Za-z0-9._-]{1,32}")

SAVE_UNAVAILABLE = "save_unavailable"
INVALID_SEAM = "invalid_seam"
NOT_A_RECEIPT = "not_a_receipt"
UNCOPYABLE_REQUEST = "uncopyable_request"
SNAPSHOT_UNREADABLE = "snapshot_unreadable"
NOT_A_SAVE_CALL = "not_a_save_call"
WRITE_LOCK_UNAVAILABLE = "write_lock_unavailable"
MEMORY_UNAVAILABLE = "memory_unavailable"


class SaveCallRefused(RuntimeError):
    """This caller cannot make a save call. ``code`` is the stable machine name. Nothing was saved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def refuse(code: str, reason: str, lost: str) -> NoReturn:
    """Log one WARNING, mark the exception logged (``smartmemory.refusal_logging``), then raise ``SaveCallRefused``.

    Every refusal of this seam goes through here, so a boundary above that reads the mark never logs it twice. A core
    without that module has no such boundary, and the exception is raised unmarked.
    """
    log.warning("Save call refused (%s): %s; %s", code, reason, lost)
    raise mark_logged(SaveCallRefused(code, reason)) from None


def mark_logged(exc: BaseException) -> BaseException:
    """Mark ``exc`` as already logged at WARNING (core's ``mark_refusal_logged``) so a boundary above does not log it again.

    A core without ``smartmemory.refusal_logging`` has no such boundary: ``exc`` is returned unmarked and the refusal
    is still raised with the one WARNING its raiser logged.
    """
    try:
        from smartmemory.refusal_logging import mark_refusal_logged
    except ImportError:
        return exc
    return mark_refusal_logged(exc)


def refuse_from(exc: Exception, code: str, reason: str, lost: str) -> NoReturn:
    """A failure of this seam's own I/O (a data-dir or write-lock step) becomes one typed, marked ``SaveCallRefused``.

    An ``exc`` that is already a logged refusal (core's mark) is raised as it is, so it is never logged twice.
    """
    try:
        from smartmemory.refusal_logging import describe_error, refusal_logged
    except ImportError:  # an older core: no mark to read, and no renderer that cannot raise, so nothing is rendered
        cause = NO_RENDERER
    else:
        if refusal_logged(exc):
            raise exc
        cause = describe_error(exc)  # core's renderer never raises
    log.warning("Save call refused (%s): %s (%s); %s", code, reason, cause, lost)
    raise mark_logged(SaveCallRefused(code, reason)) from exc


#: The cause text of a refusal in a core without ``smartmemory.refusal_logging``: the cause is chained, not rendered.
NO_RENDERER = (
    "cause chained, not rendered: this core has no smartmemory.refusal_logging"
)


def retry_key(supplied: Any = None, *, seam: str) -> Any:
    """The retry identity of one save call: the caller's key as it is, or a freshly minted one.

    Only ``None`` mints. Anything else supplied, including ``""``, is carried unchanged for core to accept or refuse.
    """
    if not isinstance(seam, str) or _SEAM.fullmatch(seam) is None:
        refuse(
            INVALID_SEAM,
            f"seam {seam!r} is not 1-32 characters of A-Z a-z 0-9 . _ -",
            "no retry key minted",
        )
    if supplied is not None:
        return supplied
    return f"{seam}:{uuid.uuid4().hex}"


class NotPlainJson(ValueError):
    """A request value the retry snapshot cannot hold: not plain JSON, self-containing, or a repeated key."""


def _plain(value: Any, path: str, active: frozenset[int]) -> Any:
    """``value`` rebuilt from exact plain-JSON types by an explicit walk: no copy hook, no ``__reduce__``, no pickle.

    ``str``/``int``/``float`` subclasses (an ``Enum`` member, say) become the base value core's JSON encoder would
    write. Mapping keys keep their type, so a key core refuses (a non-string) still reaches core and is refused there.
    """
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__index__(value)
    if isinstance(value, float):
        return float.__float__(value)
    if not isinstance(value, (Mapping, list, tuple)):
        raise NotPlainJson(f"{path} is a {type(value).__name__}, not plain JSON")
    if id(value) in active:
        raise NotPlainJson(f"{path} contains itself")
    active = active | {id(value)}
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            plain_key = _plain(key, f"{path} key", active)
            if isinstance(plain_key, (dict, list, tuple)):
                raise NotPlainJson(f"{path} has a {type(key).__name__} key")
            if plain_key in result:
                raise NotPlainJson(f"{path} repeats the key {plain_key!r}")
            result[plain_key] = _plain(item, f"{path}[{plain_key!r}]", active)
        return result
    items = [
        _plain(item, f"{path}[{index}]", active) for index, item in enumerate(value)
    ]
    return items if isinstance(value, list) else tuple(items)


class FrozenRequest(Mapping):
    """An immutable serialisation of one save request: its payload and its retry key, as ``marshal`` bytes.

    Built once from the caller's values by ``_plain`` (exact plain-JSON types, no copy hook), so no later change to
    the caller's own dict, list, context or link mappings, and no copy hook of theirs, can reach the retry. It has no
    item assignment, and every read decodes the bytes afresh, so changing what a read returned changes nothing here.
    ``thaw`` is the plain-dict copy ``SmartMemory.save`` receives. ``wire`` is the bytes themselves.
    """

    __slots__ = ("_wire", "_keys")

    def __init__(self, request: Mapping[str, Any]) -> None:
        try:
            if not isinstance(request, Mapping):
                raise NotPlainJson(
                    f"the request is a {type(request).__name__}, not a mapping"
                )
            snapshot = _plain(request, "request", frozenset())
            wire = marshal.dumps(snapshot)
        except Exception as exc:  # a value that cannot be held as plain JSON cannot be held stable for a retry
            refuse_from(
                exc,
                UNCOPYABLE_REQUEST,
                "the request holds a value that is not plain JSON, so it cannot be frozen for a retry",
                "nothing was saved and no retry key was bound to it",
            )
        object.__setattr__(self, "_wire", wire)
        object.__setattr__(self, "_keys", tuple(snapshot))

    @property
    def wire(self) -> bytes:
        """The serialised request (payload and ``idempotency_key``): what every retry decodes, byte for byte."""
        return self._wire

    def _load(self) -> dict[str, Any]:
        try:
            return marshal.loads(self._wire)
        except Exception as exc:  # the snapshot cannot be read back: nothing is sent, never a partial request
            refuse_from(
                exc,
                SNAPSHOT_UNREADABLE,
                "the frozen request could not be read back",
                "nothing was sent and the retry key was not used",
            )

    def __getitem__(self, name: str) -> Any:
        return self._load()[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, name: object) -> bool:
        return name in self._keys

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("a frozen save request cannot be changed")

    def __repr__(self) -> str:
        return f"FrozenRequest(<{len(self._wire)} bytes, keys {list(self._keys)!r}>)"

    def thaw(self) -> dict[str, Any]:
        """A fresh plain dict decoded from ``wire``: what one submission hands to ``SmartMemory.save``."""
        return self._load()


@dataclass(frozen=True)
class SaveCall:
    """One save call, frozen so a re-submission sends the identical request and retry key.

    ``request`` is a ``FrozenRequest``: a plain-JSON, read-only serialisation taken when the call is built (or constructed),
    so neither the caller's later changes to the values it passed in nor an assignment to the exposed request changes
    what a retry sends. Its ``thaw()`` is exactly what ``SmartMemory.save`` receives (it carries ``idempotency_key``).
    ``adapter_namespace`` is this caller's own registered producer namespace (G158), never read from the request.
    """

    request: Mapping[str, Any]
    adapter_namespace: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, FrozenRequest):
            object.__setattr__(self, "request", FrozenRequest(self.request))

    @property
    def idempotency_key(self) -> Any:
        return self.request["idempotency_key"]


def build_save_call(
    *,
    seam: str,
    content: Any = None,
    data: Any = None,
    memory_type: Any = None,
    extract: Any = None,
    wait: Any = None,
    requested_links: Any = None,
    context: Any = None,
    idempotency_key: Any = None,
    adapter_namespace: str | None = None,
) -> SaveCall:
    """Build the one save request from a caller's arguments. Only ``None`` means omitted.

    Content XOR data, the type, extract default, wait and link rules are core's: the request goes to
    ``SmartMemory.save`` unjudged, so core's normalizer is the only place they are decided.
    """
    fields = {
        "content": content,
        "data": data,
        "memory_type": memory_type,
        "extract": extract,
        "wait": wait,
        "requested_links": requested_links,
        "context": context,
    }
    request = {name: value for name, value in fields.items() if value is not None}
    request["idempotency_key"] = retry_key(idempotency_key, seam=seam)
    return SaveCall(request, adapter_namespace)


def submit(memory: Any, call: SaveCall, *, op: str = "save") -> SaveReturn:
    """Hand ``call`` to ``memory.save`` and read the receipt. Core's typed refusals pass through unchanged."""
    if not isinstance(call, SaveCall):
        refuse(
            NOT_A_SAVE_CALL,
            "the call was not built by build_save_call, so it has no frozen request",
            "nothing was saved and add/ingest were not used",
        )
    save = getattr(memory, "save", None)
    if not callable(save):
        refuse(
            SAVE_UNAVAILABLE,
            f"{type(memory).__name__} has no save()",
            "nothing was saved and add/ingest were not used",
        )
    resolved = resolve_save_return(
        save(call.request.thaw(), adapter_namespace=call.adapter_namespace), op=op
    )
    if (
        resolved.receipt is None
    ):  # save() promises a receipt: a legacy id or dict is not a saved result
        refuse(
            NOT_A_RECEIPT,
            f"{type(memory).__name__}.save() returned {type(resolved.value).__name__}, not a receipt",
            "no id was inferred from it",
        )
    return resolved
