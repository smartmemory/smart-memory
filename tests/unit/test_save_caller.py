"""U4.42 (C-01.activate.save.local-adapters): the wrapper's caller seam for the receipt arm of a local save.

Real lite SQLite memory (``create_lite_memory`` under ``tmp_path``, closed by the fixture), core's real request
normalizer and receipt codec. The one stand-in is the save manager, which core only installs at the facade flip
(U20.S2): ``RecordingManager`` is a real receiver that records what it is handed and answers with a real receipt.

Classes pinned, one explicit row table each (no stacked parametrize):

* retry identity: a supplied key is carried verbatim (``""`` included, core refuses it), only ``None`` mints, a minted
  key is inside core's key alphabet, and re-submitting one frozen ``SaveCall`` sends the identical key and request;
* explicit waits: ``stored``, ``extracted`` and omitted reach core unchanged, and a wait the save can never reach
  (``extracted`` with extraction off) is core's typed ``IncompatibleWait`` with its one WARNING (G98);
* no fallback: the facade off is core's ``SaveNotEnabled`` with one WARNING and no ``add``/``ingest`` call, a memory
  with no ``save`` is one typed refusal, hosted mode is refused, a legacy return from ``save`` is refused;
* receipts: a rejected admission is ``SaveRejected``, a timed-out wait stays a receipt (G173), a non-save subject is
  refused, and the producer namespace is the caller's own (G158);
* dormancy: nothing on the legacy save path imports ``save_caller``.
"""

import ast
import copy
import enum
import logging
import marshal
import operator
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from smartmemory.pipeline import profile_outcomes as outcomes
from smartmemory.save.facade import SaveNotEnabled, install_save_manager
from smartmemory.save.receipt import ReceiptSubject, build_local_receipt, decode_receipt
from smartmemory.save.request import (
    IncompatibleWait,
    SaveRequestError,
    bind_save_receipt,
)
from smartmemory.save.status import ProcessingStage
from smartmemory.tools.factory import create_lite_memory
from smartmemory_app import save_caller, storage
from smartmemory_app.save_caller import (
    INVALID_SEAM,
    MEMORY_UNAVAILABLE,
    NO_RENDERER,
    NOT_A_RECEIPT,
    NOT_A_SAVE_CALL,
    SAVE_UNAVAILABLE,
    SNAPSHOT_UNREADABLE,
    UNCOPYABLE_REQUEST,
    WRITE_LOCK_UNAVAILABLE,
    SaveCall,
    SaveCallRefused,
    build_save_call,
    retry_key,
    submit,
)
from smartmemory_app.save_return import SaveRejected, SaveReturnRefused

CALLER_LOGGER = "smartmemory_app.save_caller"
KEY_ALPHABET = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class RecordingManager:
    """A real receiver: records the hand-off and answers with a real G52 receipt (or whatever ``answer`` builds)."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer

    def receive(self, request, authority):
        self.calls.append((request, authority))
        if self.answer is not None:
            return self.answer(request)
        return bind_save_receipt(
            request,
            item_id="test_item_u442",
            run_id="test_run_u442",
            revision=1,
            arm=outcomes.PERSISTED_EXTRACTION_DEFERRED,
            stage=ProcessingStage.RECEIVED,
        )


@pytest.fixture
def memory(tmp_path):
    mem = create_lite_memory(data_dir=str(tmp_path / "test_u442_save"))
    try:
        yield mem
    finally:
        mem.close()


@pytest.fixture
def manager(memory):
    installed = RecordingManager()
    install_save_manager(memory, installed)
    return installed


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# --- retry identity ---------------------------------------------------------------------------------------------

# (label, supplied key, expected carried value). Only None mints; every other value, including the empty string and
# non-strings, is carried as given so core's normalizer is the only judge of a key.
CARRIED_KEY_ROWS = [
    ("plain", "test-key-1", "test-key-1"),
    ("alphabet-edges", "A.b_c:d-9", "A.b_c:d-9"),
    ("empty-is-not-absence", "", ""),
    ("not-a-string", 7, 7),
    ("too-long-left-for-core", "k" * 129, "k" * 129),
]


@pytest.mark.parametrize(
    "label, supplied, expected", CARRIED_KEY_ROWS, ids=[r[0] for r in CARRIED_KEY_ROWS]
)
def test_a_supplied_key_is_carried_verbatim(label, supplied, expected, caplog):
    with caplog.at_level(logging.WARNING):
        assert retry_key(supplied, seam="mcp") == expected
    assert warnings_of(caplog) == []


def test_only_none_mints_and_a_minted_key_is_in_cores_alphabet_and_unique():
    first = retry_key(None, seam="mcp")
    second = retry_key(None, seam="mcp")
    assert first != second
    assert first.startswith("mcp:") and KEY_ALPHABET.fullmatch(first)


# (label, seam). A seam is the key prefix so it must itself be in the alphabet minus the separator.
BAD_SEAM_ROWS = [
    ("empty", ""),
    ("colon", "a:b"),
    ("space", "a b"),
    ("too-long", "s" * 33),
    ("not-a-string", None),
]


@pytest.mark.parametrize(
    "label, seam", BAD_SEAM_ROWS, ids=[r[0] for r in BAD_SEAM_ROWS]
)
def test_a_bad_seam_is_one_typed_refusal_with_one_warning(label, seam, caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused) as caught:
        retry_key(None, seam=seam)
    assert caught.value.code == INVALID_SEAM
    assert warnings_of(caplog) == [
        f"Save call refused ({INVALID_SEAM}): seam {seam!r} is not 1-32 characters of A-Z a-z 0-9 . _ -; "
        "no retry key minted"
    ]


def test_a_bad_seam_is_refused_even_when_a_key_is_supplied(caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused):
        retry_key("test-key", seam="a:b")
    assert len(warnings_of(caplog)) == 1


def test_a_resubmitted_call_sends_the_same_key_and_request(memory, manager):
    call = build_save_call(seam="mcp", content="test u442 retry")
    submit(memory, call)
    submit(memory, call)
    (first, _), (second, _) = manager.calls
    assert first.idempotency_key == second.idempotency_key == call.idempotency_key
    assert first.content == second.content == "test u442 retry"


def test_two_built_calls_do_not_share_a_minted_key():
    assert (
        build_save_call(seam="mcp", content="x").idempotency_key
        != build_save_call(seam="mcp", content="x").idempotency_key
    )


def test_a_call_is_frozen():
    call = build_save_call(seam="mcp", content="x")
    with pytest.raises(AttributeError):
        call.adapter_namespace = "mcp.hosted"


# --- the request the seam builds ----------------------------------------------------------------------------------

# (label, kwargs, the request keys that must be present, the keys that must be absent). A JSON null is not absence to
# core, so every unset field is left out.
REQUEST_ROWS = [
    (
        "content-only",
        {"content": "c"},
        {"content": "c"},
        {"data", "memory_type", "extract", "wait", "context"},
    ),
    (
        "data-only",
        {"data": {"k": 1}},
        {"data": {"k": 1}},
        {"content", "extract", "wait"},
    ),
    (
        "every-field",
        {
            "content": "c",
            "memory_type": "semantic",
            "extract": False,
            "wait": "stored",
            "requested_links": [{"target_id": "test_t", "link_type": "RELATED"}],
            "context": {"k": "v"},
        },
        {
            "content": "c",
            "memory_type": "semantic",
            "extract": False,
            "wait": "stored",
            "requested_links": [{"target_id": "test_t", "link_type": "RELATED"}],
            "context": {"k": "v"},
        },
        set(),
    ),
    (
        "falsy-values-are-kept",
        {"content": "", "extract": False},
        {"content": "", "extract": False},
        {"wait"},
    ),
]


@pytest.mark.parametrize(
    "label, kwargs, present, absent", REQUEST_ROWS, ids=[r[0] for r in REQUEST_ROWS]
)
def test_the_request_carries_only_what_was_given(label, kwargs, present, absent):
    call = build_save_call(seam="mcp", idempotency_key="test-key", **kwargs)
    assert dict(call.request) == {**present, "idempotency_key": "test-key"}
    assert absent.isdisjoint(call.request)


# --- explicit waits and the real normalizer --------------------------------------------------------------------

# (label, kwargs, check on the SaveRequest core's normalizer built). Every value of the wait axis appears.
WAIT_ROWS = [
    ("wait-omitted", {"content": "c"}, lambda r: r.wait is None),
    ("wait-stored", {"content": "c", "wait": "stored"}, lambda r: r.wait == "stored"),
    (
        "wait-extracted",
        {"content": "c", "wait": "extracted"},
        lambda r: r.wait == "extracted" and r.extract,
    ),
    (
        "wait-stored-extraction-off",
        {"content": "c", "wait": "stored", "extract": False},
        lambda r: r.wait == "stored" and not r.extract,
    ),
]


@pytest.mark.parametrize(
    "label, kwargs, check", WAIT_ROWS, ids=[r[0] for r in WAIT_ROWS]
)
def test_waits_reach_the_manager_through_the_real_normalizer(
    label, kwargs, check, memory, manager, caplog
):
    call = build_save_call(
        seam="mcp",
        idempotency_key="test-key-w",
        adapter_namespace="mcp.local",
        **kwargs,
    )
    with caplog.at_level(logging.WARNING):
        result = submit(memory, call)
    ((normalized, _),) = manager.calls
    assert check(normalized), label
    assert normalized.idempotency_key == "test-key-w"
    assert result.receipt is not None and result.value == "test_item_u442"
    assert warnings_of(caplog) == []


def test_an_unreachable_wait_is_cores_typed_error_before_any_run_exists(
    memory, manager, caplog
):
    call = build_save_call(
        seam="mcp",
        content="c",
        extract=False,
        wait="extracted",
        idempotency_key="test-key-x",
    )
    with caplog.at_level(logging.WARNING), pytest.raises(IncompatibleWait) as caught:
        submit(memory, call)
    assert (
        caught.value.receipt is None
    )  # refused by the normalizer before a run existed, so core attaches none
    assert warnings_of(caplog) == [
        "save refused (IncompatibleWait): Wait target is incompatible with this status; nothing was admitted"
    ]  # core's one WARNING; this seam adds none
    assert manager.calls == []


# (label, key). Core owns the key rule: a bad key reaches it unjudged and is its typed error with one WARNING.
BAD_KEY_ROWS = [
    ("empty", ""),
    ("space", "a b"),
    ("too-long", "k" * 129),
    ("not-a-string", 7),
]


@pytest.mark.parametrize("label, key", BAD_KEY_ROWS, ids=[r[0] for r in BAD_KEY_ROWS])
def test_a_bad_supplied_key_is_cores_refusal_and_never_replaced(
    label, key, memory, manager, caplog
):
    call = build_save_call(seam="mcp", content="c", idempotency_key=key)
    assert call.idempotency_key == key
    with caplog.at_level(logging.WARNING), pytest.raises(SaveRequestError):
        submit(memory, call)
    assert len(warnings_of(caplog)) == 1
    assert manager.calls == []


# --- no fallback --------------------------------------------------------------------------------------------------


def test_the_facade_off_is_cores_refusal_and_never_add_or_ingest(memory, caplog):
    seen = []
    for name in ("add", "ingest", "ingest_structured", "direct_store"):
        original = getattr(memory, name)
        setattr(
            memory,
            name,
            lambda *a, _n=name, _o=original, **k: seen.append(_n) or _o(*a, **k),
        )
    nodes_before = sorted(memory._graph.nodes.nodes())
    with caplog.at_level(logging.WARNING), pytest.raises(SaveNotEnabled):
        submit(memory, build_save_call(seam="mcp", content="test u442 off"))
    assert warnings_of(caplog) == [
        "save refused: no save manager is installed (the common-receipt facade is not enabled); "
        "nothing was admitted and add/ingest were not used"
    ]
    assert seen == []
    assert sorted(memory._graph.nodes.nodes()) == nodes_before


class NoSave:
    """An older core, a hosted client, or a secure wrapper without the delegate: no ``save`` attribute at all."""


class SaveIsNotCallable:
    save = "nope"


class LegacySave:
    def __init__(self, returned):
        self.returned = returned

    def save(self, request, *, adapter_namespace=None):
        return self.returned


# (label, memory object, error code, the ONE exact WARNING)
SEAM_REFUSAL_ROWS = [
    (
        "no-save-attribute",
        NoSave(),
        SAVE_UNAVAILABLE,
        f"Save call refused ({SAVE_UNAVAILABLE}): NoSave has no save(); nothing was saved and add/ingest were not used",
    ),
    (
        "save-not-callable",
        SaveIsNotCallable(),
        SAVE_UNAVAILABLE,
        f"Save call refused ({SAVE_UNAVAILABLE}): SaveIsNotCallable has no save(); "
        "nothing was saved and add/ingest were not used",
    ),
    (
        "legacy-id-returned",
        LegacySave("test_item_legacy"),
        NOT_A_RECEIPT,
        f"Save call refused ({NOT_A_RECEIPT}): LegacySave.save() returned str, not a receipt; "
        "no id was inferred from it",
    ),
    (
        "legacy-dict-returned",
        LegacySave({"item_id": "test_item_legacy"}),
        NOT_A_RECEIPT,
        f"Save call refused ({NOT_A_RECEIPT}): LegacySave.save() returned dict, not a receipt; "
        "no id was inferred from it",
    ),
]


@pytest.mark.parametrize(
    "label, target, code, message",
    SEAM_REFUSAL_ROWS,
    ids=[r[0] for r in SEAM_REFUSAL_ROWS],
)
def test_the_seam_refuses_what_is_not_a_receipt_save(
    label, target, code, message, caplog
):
    with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused) as caught:
        submit(target, build_save_call(seam="mcp", content="c"))
    assert caught.value.code == code
    assert warnings_of(caplog) == [message]


# --- receipts -----------------------------------------------------------------------------------------------------


# (label, admission error code). The reader turns each rejected admission into SaveRejected, never an id.
REJECTION_ROWS = [
    ("idempotency-conflict", "idempotency_conflict"),
    ("retry-key-expired", "retry_key_expired"),
]


@pytest.mark.parametrize(
    "label, code", REJECTION_ROWS, ids=[r[0] for r in REJECTION_ROWS]
)
def test_a_rejected_admission_is_save_rejected_with_one_warning(
    label, code, memory, caplog
):
    wire = {
        "schema_version": "1.1",
        "admission_error": {"code": code, "message": f"test {code}"},
    }
    install_save_manager(
        memory, RecordingManager(answer=lambda request: decode_receipt(wire))
    )
    with caplog.at_level(logging.WARNING), pytest.raises(SaveRejected) as caught:
        submit(memory, build_save_call(seam="mcp", content="c"))
    assert caught.value.code == code
    assert [
        (r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.WARNING
    ] == [
        (
            "smartmemory_app.save_return",
            f"Save return save rejected at admission ({code}); nothing was stored under a new id",
        )
    ]


def test_a_timed_out_wait_stays_a_flagged_receipt_never_failed_or_complete(
    memory, caplog
):
    def timed_out(request):
        return build_local_receipt(
            subject=ReceiptSubject("item_save", item_id="test_item_u442"),
            run_id="test_run_u442",
            processing_stage="stored",
            execution_state="queued",
            outcome=None,
            terminal=False,
            result_availability="pending",
            client_timed_out=True,
            revision=1,
        )

    install_save_manager(memory, RecordingManager(answer=timed_out))
    with caplog.at_level(logging.WARNING):
        result = submit(
            memory, build_save_call(seam="mcp", content="c", wait="extracted")
        )
    assert result.receipt is not None and result.value == "test_item_u442"
    assert result.receipt["client_timed_out"] is True
    assert result.receipt["terminal"] is False
    assert warnings_of(caplog) == []


def test_a_receipt_that_is_not_a_save_is_refused_by_the_reader(memory, caplog):
    def stage_job(request):
        return build_local_receipt(
            subject=ReceiptSubject(
                "stage_job", occurrence_id="test_occ", stage_id="test_stage"
            ),
            run_id="test_run_u442",
            processing_stage=None,
            execution_state="queued",
            outcome=None,
            terminal=False,
            result_availability="pending",
        )

    install_save_manager(memory, RecordingManager(answer=stage_job))
    with caplog.at_level(logging.WARNING), pytest.raises(SaveReturnRefused):
        submit(memory, build_save_call(seam="mcp", content="c"))
    assert warnings_of(caplog) == [
        "Save return save refused: receipt subject stage_job is not a save; no id returned"
    ]


def test_the_producer_namespace_is_the_callers_own_never_the_requests(memory, manager):
    call = build_save_call(
        seam="mcp",
        content="c",
        context={"session_id": "test_s1"},
        adapter_namespace="mcp.local",
    )
    submit(memory, call)
    ((normalized, _),) = manager.calls
    assert normalized.context["producer_namespace"] == "mcp.local"
    assert set(normalized.scoped_identities) == {"session_id"}


def test_no_namespace_keeps_a_context_identity_as_metadata(memory, manager, caplog):
    call = build_save_call(seam="mcp", content="c", context={"session_id": "test_s1"})
    with caplog.at_level(logging.WARNING):
        submit(memory, call)
    ((normalized, _),) = manager.calls
    assert (
        normalized.scoped_identities == {}
        and normalized.context.get("session_id") == "test_s1"
    )
    assert warnings_of(caplog) == [
        "save context identities ['session_id'] have no authenticated producer namespace: kept as caller metadata, "
        "no scoped identity minted"
    ]


# --- storage.save: the local doorway ------------------------------------------------------------------------------


def test_storage_save_runs_under_the_write_lock_on_the_given_memory(
    memory, manager, tmp_path, monkeypatch
):
    locked = []

    class Lock:
        def __enter__(self):
            locked.append("in")

        def __exit__(self, *exc):
            locked.append("out")

    monkeypatch.setattr(storage, "_get_lock_file", lambda path: Lock())
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    result = storage.save(build_save_call(seam="mcp", content="c"), memory=memory)
    assert result.value == "test_item_u442" and locked == ["in", "out"]


def test_storage_save_refuses_hosted_with_one_warning_and_never_ingests(
    caplog, monkeypatch
):
    from smartmemory_app.remote_backend import RemoteMemory

    remote = RemoteMemory.__new__(RemoteMemory)
    seen = []
    monkeypatch.setattr(
        RemoteMemory,
        "ingest",
        lambda self, *a, **k: seen.append("ingest"),
        raising=False,
    )
    with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused) as caught:
        storage.save(build_save_call(seam="mcp", content="c"), memory=remote)
    assert caught.value.code == SAVE_UNAVAILABLE and seen == []
    assert warnings_of(caplog) == [
        f"Save call refused ({SAVE_UNAVAILABLE}): hosted mode has no receipt save from this client; "
        "nothing was saved and ingest was not used"
    ]


def test_every_refusal_of_the_seam_is_marked_logged_for_the_boundary_above(caplog):
    """One row per refusal site: the WARNING is logged here, so the exception carries core's already-logged mark."""
    from smartmemory.refusal_logging import refusal_logged

    class NoSave:
        pass

    class Legacy:
        def save(self, request, *, adapter_namespace=None):
            return "legacy-id"

    rows = [
        (INVALID_SEAM, lambda: build_save_call(seam="bad seam!", content="c")),
        (
            SAVE_UNAVAILABLE,
            lambda: submit(NoSave(), build_save_call(seam="mcp", content="c")),
        ),
        (
            NOT_A_RECEIPT,
            lambda: submit(Legacy(), build_save_call(seam="mcp", content="c")),
        ),
    ]
    for code, act in rows:
        caplog.clear()
        with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused) as caught:
            act()
        assert caught.value.code == code and refusal_logged(caught.value), code
        assert len(warnings_of(caplog)) == 1, code


def test_a_core_without_refusal_logging_still_refuses_once_unmarked(
    caplog, monkeypatch
):
    """The ImportError arm of ``refuse``: no mark module means no boundary that reads it, never a lost refusal."""
    monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    with caplog.at_level(logging.WARNING), pytest.raises(SaveCallRefused) as caught:
        build_save_call(seam="bad seam!", content="c")
    assert caught.value.code == INVALID_SEAM
    assert not any(k.startswith("_") and "logged" in k for k in vars(caught.value))
    assert len(warnings_of(caplog)) == 1


def test_storage_save_with_the_facade_off_is_cores_refusal(
    memory, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    with caplog.at_level(logging.WARNING), pytest.raises(SaveNotEnabled):
        storage.save(build_save_call(seam="mcp", content="c"), memory=memory)
    assert len(warnings_of(caplog)) == 1


# --- dormancy -----------------------------------------------------------------------------------------------------

#: Every module that may import save_caller. storage.save imports it lazily (inside the function), so a legacy module
#: that only reaches it through ``storage.ingest`` cannot.
SAVE_CALLER_IMPORTERS = {"storage.py"}


def test_only_the_save_doorway_imports_the_caller_seam():
    root = Path(save_caller.__file__).parent
    importers = set()
    for path in sorted(root.rglob("*.py")):
        if path.name == "save_caller.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "smartmemory_app.save_caller"
            ):
                importers.add(path.name)
            if isinstance(node, ast.Import) and any(
                a.name == "smartmemory_app.save_caller" for a in node.names
            ):
                importers.add(path.name)
    assert importers == SAVE_CALLER_IMPORTERS


def test_the_legacy_ingest_and_normalizer_do_not_reach_the_caller_seam():
    tree = ast.parse(Path(storage.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "ingest",
            "_normalize_ingest_result",
        }:
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert not ({"save", "submit", "SaveCall"} & (names | attrs)), node.name


def test_savecall_is_a_dataclass_of_request_and_namespace():
    assert [f for f in SaveCall.__dataclass_fields__] == [
        "request",
        "adapter_namespace",
    ]


# --- fix round 1 (F2): a built call is a snapshot --------------------------------------------------------------------
# Bug class: unstable retry identity/payload. The call holds caller-owned mutable values, so a change made after the
# first submit changes what the retry sends under the old key.


@dataclass
class Link:
    target: str


def saved_receipt():
    return build_local_receipt(
        subject=ReceiptSubject("item_save", item_id="test_item_u442"),
        run_id="test_run_u442",
        processing_stage="stored",
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
        revision=1,
    )


class Recorder:
    """A memory whose ``save`` records the exact request object it is handed and answers with a real receipt."""

    def __init__(self):
        self.sent = []

    def save(self, request, *, adapter_namespace=None):
        self.sent.append(request)
        return saved_receipt()


def _link(target):
    return {"target_id": target, "link_type": "RELATED"}


def _after_build(change):
    return lambda kwargs, call, rec: change(kwargs)


def _receiver_mutates(kwargs, call, rec):
    submit(rec, call)
    rec.sent[0]["data"]["k"].append(9)
    rec.sent[0]["idempotency_key"] = "receiver-key"


# (label, build kwargs factory, the change to attempt after build, the error that change must raise or None). Every
# row must leave the request, and so the retry, exactly as it was built.
FROZEN_ROWS = [
    (
        "nested list appended",
        lambda: {"data": {"k": [1]}},
        _after_build(lambda kw: kw["data"]["k"].append(2)),
        None,
    ),
    (
        "nested dict changed",
        lambda: {"data": {"k": {"a": 1}}},
        _after_build(lambda kw: kw["data"]["k"].__setitem__("a", 2)),
        None,
    ),
    (
        "context mapping changed",
        lambda: {"content": "c", "context": {"session_id": ["s1"]}},
        _after_build(lambda kw: kw["context"]["session_id"].append("s2")),
        None,
    ),
    (
        "link mapping changed",
        lambda: {"content": "c", "requested_links": [_link("t1")]},
        _after_build(
            lambda kw: kw["requested_links"][0].__setitem__("target_id", "t2")
        ),
        None,
    ),
    (
        "link list appended",
        lambda: {"content": "c", "requested_links": [_link("t1")]},
        _after_build(lambda kw: kw["requested_links"].append(_link("t2"))),
        None,
    ),
    (
        "nested value changed through a read of the exposed request",
        lambda: {"data": {"k": [1]}},
        lambda kw, call, rec: call.request["data"]["k"].append(2),
        None,
    ),
    (
        "key reassigned on the exposed request",
        lambda: {"content": "c"},
        lambda kw, call, rec: operator.setitem(
            call.request, "idempotency_key", "replacement-key"
        ),
        TypeError,
    ),
    (
        "key deleted from the exposed request",
        lambda: {"content": "c"},
        lambda kw, call, rec: operator.delitem(call.request, "idempotency_key"),
        TypeError,
    ),
    (
        "field added with update",
        lambda: {"content": "c"},
        lambda kw, call, rec: call.request.update({"wait": "stored"}),
        AttributeError,
    ),
    (
        "request attribute rebound",
        lambda: {"content": "c"},
        lambda kw, call, rec: setattr(call, "request", {"content": "other"}),
        AttributeError,
    ),
    (
        "snapshot slot rebound",
        lambda: {"content": "c"},
        lambda kw, call, rec: setattr(call.request, "_wire", b""),
        AttributeError,
    ),
    (
        "receiver mutates what it was handed",
        lambda: {"data": {"k": [1]}},
        _receiver_mutates,
        None,
    ),
]


@pytest.mark.parametrize(
    "label, make, change, raises", FROZEN_ROWS, ids=[r[0] for r in FROZEN_ROWS]
)
def test_a_built_call_is_a_snapshot_the_retry_sends_unchanged(
    label, make, change, raises
):
    kwargs = make()
    call = build_save_call(seam="mcp", **kwargs)
    before = copy.deepcopy(dict(call.request))
    rec = Recorder()
    if raises is None:
        change(kwargs, call, rec)
    else:
        with pytest.raises(raises):
            change(kwargs, call, rec)
    assert dict(call.request) == before, label
    submit(rec, call)
    assert rec.sent[-1] == before, label


# --- fix round 2 (N2): a retry resends the first submission byte for byte -------------------------------------------
# Bug class: unstable retry identity/payload. A copy hook (``__deepcopy__``/``__copy__``/``__reduce_ex__``) is the
# caller's code and may hand back the live object, so the snapshot never calls one: it is rebuilt from exact
# plain-JSON types and held as bytes. One row per way the caller or a receiver can still hold a live reference.


class SharedList(list):
    """The reviewer's N2 repro: a list whose deep copy is itself."""

    def __deepcopy__(self, memo):
        return self


class SharedDict(dict):
    def __deepcopy__(self, memo):
        return self


class AllHooksShared(list):
    """Every copy hook hands back the live object."""

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        return (list, (), None, iter(self))


class CopyOnceList(list):
    """The reviewer's N3 repro: copies once, then fails on every later copy (no later copy is ever taken)."""

    copied = False

    def __deepcopy__(self, memo):
        if self.copied:
            raise RuntimeError("test copy fails on retry export")
        result = CopyOnceList(self)
        result.copied = True
        return result


class LiveMapping(Mapping):
    """A mapping whose items view reads a dict the caller keeps."""

    def __init__(self, backing):
        self.backing = backing

    def __iter__(self):
        return iter(self.backing)

    def __getitem__(self, key):
        return self.backing[key]

    def __len__(self):
        return len(self.backing)

    def items(self):
        return self.backing.items()


class SuppliedKey(str):
    def __str__(self):
        return "not-the-key"


class KindEnum(enum.StrEnum):
    SEMANTIC = "semantic"


class SeenRecorder:
    """Records the object ``save`` was handed and, separately, what it held at that moment."""

    def __init__(self):
        self.sent, self.seen = [], []

    def save(self, request, *, adapter_namespace=None):
        self.sent.append(request)
        self.seen.append(copy.deepcopy(request))
        return saved_receipt()


def _mutate_sent(kw, call, rec):
    rec.sent[0]["data"]["k"].append(9)
    rec.sent[0]["idempotency_key"] = "receiver-key"


# (label, build kwargs factory, change made after the FIRST submission). Every row must resend the first request.
RETRY_ROWS = [
    (
        "deep copy returns the shared list",
        lambda: {"data": {"k": SharedList([1])}},
        lambda kw, c, r: kw["data"]["k"].append(2),
    ),
    (
        "deep copy returns the shared dict",
        lambda: {"data": {"k": SharedDict(a=1)}},
        lambda kw, c, r: kw["data"]["k"].__setitem__("a", 2),
    ),
    (
        "every copy hook returns the shared list",
        lambda: {"data": {"k": AllHooksShared([1])}},
        lambda kw, c, r: kw["data"]["k"].append(2),
    ),
    (
        "copy hook fails after the first copy",
        lambda: {"data": {"k": CopyOnceList([1])}},
        lambda kw, c, r: kw["data"]["k"].append(2),
    ),
    (
        "mapping with a live items view",
        lambda: {"data": LiveMapping({"a": 1})},
        lambda kw, c, r: kw["data"].backing.__setitem__("a", 2),
    ),
    (
        "plain nested list",
        lambda: {"data": {"k": [1]}},
        lambda kw, c, r: kw["data"]["k"].append(2),
    ),
    (
        "context list",
        lambda: {"content": "c", "context": {"session_id": ["s1"]}},
        lambda kw, c, r: kw["context"]["session_id"].append("s2"),
    ),
    (
        "link mapping",
        lambda: {"content": "c", "requested_links": [_link("t1")]},
        lambda kw, c, r: kw["requested_links"][0].__setitem__("target_id", "t2"),
    ),
    (
        "str-subclass key and StrEnum memory type",
        lambda: {
            "content": "c",
            "idempotency_key": SuppliedKey("test-key"),
            "memory_type": KindEnum.SEMANTIC,
        },
        lambda kw, c, r: None,
    ),
    ("receiver mutates what it was handed", lambda: {"data": {"k": [1]}}, _mutate_sent),
]


@pytest.mark.parametrize(
    "label, make, change", RETRY_ROWS, ids=[r[0] for r in RETRY_ROWS]
)
def test_a_retry_resends_the_first_submission_byte_for_byte(
    label, make, change, caplog
):
    kwargs = make()
    call = build_save_call(seam="mcp", **kwargs)
    wire, key = call.request.wire, call.idempotency_key
    rec = SeenRecorder()
    submit(rec, call)
    change(kwargs, call, rec)
    submit(rec, call)
    assert rec.seen[0] == rec.seen[1] == marshal.loads(wire), label
    assert rec.sent[0] is not rec.sent[1], (
        label
    )  # every submission gets its own decoded copy
    assert call.request.wire == wire and call.idempotency_key == key, label
    assert type(rec.seen[1]["idempotency_key"]) is str, label
    assert all(
        type(rec.seen[1].get(name, "")) is str for name in ("memory_type", "content")
    ), label
    assert warnings_of(caplog) == [], label


def test_a_directly_built_savecall_snapshots_its_request_too():
    request = {"content": "c", "idempotency_key": "test-key", "data": {"k": [1]}}
    call = SaveCall(request, "mcp.local")
    request["data"]["k"].append(2)
    assert call.request["data"] == {"k": [1]} and call.idempotency_key == "test-key"
    assert (
        SaveCall(call.request, "mcp.local").request is call.request
    )  # an already-frozen request is not re-copied


# --- fix round 1 (F3): every new failure boundary is a typed, marked, single-WARNING refusal -------------------------
# Bug class: C1 failure path without a typed/logged/marked refusal; C8 raw failure escape. One row per site of the
# census (u-U4.42/census.py): each asserts the exact full WARNING list (logger, level, message), the typed error and
# the propagated core mark (``refusal_logged``).

READER_LOGGER = "smartmemory_app.save_return"


def _warning_records(caplog):
    return [
        (r.name, r.levelno, r.getMessage())
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ]


def _wire(**over):
    return {**saved_receipt().to_dict(), **over}


class Returns:
    def __init__(self, returned):
        self.returned = returned

    def save(self, request, *, adapter_namespace=None):
        return self.returned


def _lock_dir_is_a_file(ctx):
    blocked = ctx.tmp_path / "blocked"
    blocked.write_text("a file, not a data dir")
    ctx.monkeypatch.setattr(storage, "_data_path", blocked)
    cause = f"FileExistsError: [Errno 17] File exists: {str(blocked)!r}"
    return (
        lambda: storage.save(
            build_save_call(seam="mcp", content="c"), memory=Recorder()
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): the local write lock could not be taken ({cause}); "
                "nothing was saved and ingest was not used",
            )
        ],
        True,
    )


def _lock_is_held(ctx):
    import filelock

    ctx.monkeypatch.setattr(storage, "_data_path", ctx.tmp_path)
    ctx.monkeypatch.setattr(storage, "WRITE_LOCK_TIMEOUT", 0.05)
    lock_path = str(ctx.tmp_path / ".write.lock")
    held = filelock.FileLock(lock_path)
    held.acquire()
    ctx.addfinalizer(held.release)
    cause = f"Timeout: The file lock {lock_path!r} could not be acquired."
    return (
        lambda: storage.save(
            build_save_call(seam="mcp", content="c"), memory=Recorder()
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): the local write lock could not be taken ({cause}); "
                "nothing was saved and ingest was not used",
            )
        ],
        True,
    )


def _data_dir_resolution_fails(ctx):
    def boom(explicit=None):
        raise RuntimeError("test data dir failure")

    ctx.monkeypatch.setattr(storage, "_data_path", None)
    ctx.monkeypatch.setattr(storage, "_resolve_data_dir", boom)
    return (
        lambda: storage.save(
            build_save_call(seam="mcp", content="c"), memory=Recorder()
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): the local write lock could not be taken "
                "(RuntimeError: test data dir failure); nothing was saved and ingest was not used",
            )
        ],
        True,
    )


def _default_memory_cannot_be_opened(ctx):
    def boom():
        raise RuntimeError("test memory failure")

    ctx.monkeypatch.setattr(storage, "get_memory", boom)
    return (
        lambda: storage.save(build_save_call(seam="mcp", content="c")),
        SaveCallRefused,
        MEMORY_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({MEMORY_UNAVAILABLE}): the local memory could not be opened "
                "(RuntimeError: test memory failure); nothing was saved and ingest was not used",
            )
        ],
        True,
    )


def _lock_failure_already_logged_is_not_logged_again(ctx):
    from smartmemory.refusal_logging import mark_refusal_logged

    def already_logged(path):
        raise mark_refusal_logged(RuntimeError("test lock refusal"))

    ctx.monkeypatch.setattr(storage, "_data_path", ctx.tmp_path)
    ctx.monkeypatch.setattr(storage, "_get_lock_file", already_logged)
    return (
        lambda: storage.save(
            build_save_call(seam="mcp", content="c"), memory=Recorder()
        ),
        RuntimeError,
        None,
        [],
        True,
    )


def _lock_failure_in_a_core_without_the_mark(ctx):
    ctx.monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    ctx.monkeypatch.setattr(storage, "_data_path", ctx.tmp_path)

    def boom(path):
        raise OSError("test lock failure")

    ctx.monkeypatch.setattr(storage, "_get_lock_file", boom)
    return (
        lambda: storage.save(
            build_save_call(seam="mcp", content="c"), memory=Recorder()
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): the local write lock could not be taken "
                f"({NO_RENDERER}); nothing was saved and ingest was not used",
            )
        ],
        False,
    )


def _uncopyable_request(ctx):
    return (
        lambda: build_save_call(
            seam="mcp", content="c", context={"lock": threading.Lock()}
        ),
        SaveCallRefused,
        UNCOPYABLE_REQUEST,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({UNCOPYABLE_REQUEST}): the request holds a value that is not plain JSON, so it "
                "cannot be frozen for a retry (NotPlainJson: request['context']['lock'] is a lock, not plain JSON); "
                "nothing was saved and no retry key was bound to it",
            )
        ],
        True,
    )


def _reader_refusal(
    returned,
    reason,
    error=SaveReturnRefused,
    code=None,
    marked=True,
    missing_module=None,
):
    def row(ctx):
        if missing_module:
            ctx.monkeypatch.setitem(sys.modules, missing_module, None)
        return (
            lambda: submit(
                Returns(returned()), build_save_call(seam="mcp", content="c")
            ),
            error,
            code,
            [(READER_LOGGER, logging.WARNING, reason)],
            marked,
        )

    return row


def _stage_job_receipt():
    return build_local_receipt(
        subject=ReceiptSubject(
            "stage_job", occurrence_id="test_occ", stage_id="test_stage"
        ),
        run_id="test_run_u442",
        processing_stage=None,
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
    )


def _rejected():
    return decode_receipt(
        {
            "schema_version": "1.1",
            "admission_error": {
                "code": "idempotency_conflict",
                "message": "test conflict",
            },
        }
    )


# --- fix round 2 (N3): every snapshot boundary, and a cause that cannot be rendered -----------------------------------
# Bug class: C1 failure path without a typed, logged, marked refusal (C8 raw escape). The snapshot is taken once and
# read back from bytes, so its boundaries are: the build walk (each way a value is not plain JSON), the read-back of
# the bytes (a field read, ``thaw``, ``submit``), ``submit`` of an object that is not a ``SaveCall``, and the cause
# text itself (an old core has no renderer that cannot raise).


def _self_list():
    looped = []
    looped.append(looped)
    return looped


class TwiceKeyed(Mapping):
    """A mapping whose iteration yields the key ``a`` twice."""

    def __iter__(self):
        return iter(["a", "a"])

    def __getitem__(self, key):
        return 1

    def __len__(self):
        return 2


class BrokenItems(Mapping):
    def __iter__(self):
        raise RuntimeError("test items failure")

    def __getitem__(self, key):
        raise KeyError(key)

    def __len__(self):
        return 1


class MustNotSave:
    def save(self, request, *, adapter_namespace=None):
        raise AssertionError("save must not be called")


def _uncopyable(cause):
    return [
        (
            CALLER_LOGGER,
            logging.WARNING,
            f"Save call refused ({UNCOPYABLE_REQUEST}): the request holds a value that is not plain JSON, so it "
            f"cannot be frozen for a retry ({cause}); nothing was saved and no retry key was bound to it",
        )
    ]


def _not_plain(kwargs, problem, cause=None):
    def row(ctx):
        return (
            lambda: build_save_call(seam="mcp", **kwargs()),
            SaveCallRefused,
            UNCOPYABLE_REQUEST,
            _uncopyable(cause or f"NotPlainJson: {problem}"),
            True,
        )

    return row


def _savecall_not_a_mapping(ctx):
    return (
        lambda: SaveCall("x", "mcp.local"),
        SaveCallRefused,
        UNCOPYABLE_REQUEST,
        _uncopyable("NotPlainJson: the request is a str, not a mapping"),
        True,
    )


def _snapshot_unreadable(read):
    def row(ctx):
        call = build_save_call(seam="mcp", data={"k": [1]})
        corrupt = b"\x00test-corrupt"
        object.__setattr__(call.request, "_wire", corrupt)
        try:
            marshal.loads(corrupt)
        except (
            Exception
        ) as exc:  # the same bytes, read the same way: the cause the refusal must name
            cause = f"{type(exc).__name__}: {exc}"
        return (
            lambda: read(call),
            SaveCallRefused,
            SNAPSHOT_UNREADABLE,
            [
                (
                    CALLER_LOGGER,
                    logging.WARNING,
                    f"Save call refused ({SNAPSHOT_UNREADABLE}): the frozen request could not be read back ({cause}); "
                    "nothing was sent and the retry key was not used",
                )
            ],
            True,
        )

    return row


def _not_a_save_call(ctx):
    return (
        lambda: submit(MustNotSave(), {"content": "c", "idempotency_key": "test-key"}),
        SaveCallRefused,
        NOT_A_SAVE_CALL,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({NOT_A_SAVE_CALL}): the call was not built by build_save_call, so it has no "
                "frozen request; nothing was saved and add/ingest were not used",
            )
        ],
        True,
    )


class UnrenderableOSError(OSError):
    def __str__(self):
        raise ValueError("test render failure")


def _unrenderable_cause_old_core(ctx):
    """The reviewer's N3 repro: no ``smartmemory.refusal_logging``, and a cause whose ``__str__`` raises."""
    ctx.monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    return (
        lambda: save_caller.refuse_from(
            UnrenderableOSError(),
            WRITE_LOCK_UNAVAILABLE,
            "lock failed",
            "nothing saved",
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): lock failed ({NO_RENDERER}); nothing saved",
            )
        ],
        False,
    )


def _unrenderable_cause(ctx):
    return (
        lambda: save_caller.refuse_from(
            UnrenderableOSError(),
            WRITE_LOCK_UNAVAILABLE,
            "lock failed",
            "nothing saved",
        ),
        SaveCallRefused,
        WRITE_LOCK_UNAVAILABLE,
        [
            (
                CALLER_LOGGER,
                logging.WARNING,
                f"Save call refused ({WRITE_LOCK_UNAVAILABLE}): lock failed "
                "(UnrenderableOSError: <UnrenderableOSError could not be rendered>); nothing saved",
            )
        ],
        True,
    )


REJECTED_WARNING = "Save return save rejected at admission (idempotency_conflict); nothing was stored under a new id"

# (label, row builder). A row builder returns (act, error type, code or None, the exact WARNING list, marked).
BOUNDARY_ROWS = [
    ("write-lock data dir is a file", _lock_dir_is_a_file),
    ("write lock held by another process", _lock_is_held),
    ("data dir resolution fails", _data_dir_resolution_fails),
    ("default memory cannot be opened", _default_memory_cannot_be_opened),
    (
        "lock failure already logged and marked",
        _lock_failure_already_logged_is_not_logged_again,
    ),
    (
        "lock failure in a core without the mark",
        _lock_failure_in_a_core_without_the_mark,
    ),
    ("request value cannot be copied", _uncopyable_request),
    (
        "request list contains itself",
        _not_plain(
            lambda: {"data": {"k": _self_list()}},
            "request['data']['k'][0] contains itself",
        ),
    ),
    (
        "link object, not a plain mapping",
        _not_plain(
            lambda: {"content": "c", "requested_links": [Link("t1")]},
            "request['requested_links'][0] is a Link, not plain JSON",
        ),
    ),
    (
        "tuple key",
        _not_plain(
            lambda: {"data": {("a", "b"): 1}}, "request['data'] has a tuple key"
        ),
    ),
    (
        "mapping yields a key twice",
        _not_plain(
            lambda: {"data": TwiceKeyed()}, "request['data'] repeats the key 'a'"
        ),
    ),
    (
        "mapping whose items cannot be read",
        _not_plain(
            lambda: {"data": BrokenItems()}, None, "RuntimeError: test items failure"
        ),
    ),
    ("directly built call whose request is not a mapping", _savecall_not_a_mapping),
    (
        "snapshot unreadable on a read of a field",
        _snapshot_unreadable(lambda call: call.request["data"]),
    ),
    (
        "snapshot unreadable on thaw",
        _snapshot_unreadable(lambda call: call.request.thaw()),
    ),
    (
        "snapshot unreadable on submit, save never called",
        _snapshot_unreadable(lambda call: submit(MustNotSave(), call)),
    ),
    ("submit given what build_save_call did not build", _not_a_save_call),
    (
        "unrenderable cause in a core without refusal_logging",
        _unrenderable_cause_old_core,
    ),
    ("unrenderable cause in a core with refusal_logging", _unrenderable_cause),
    (
        "returned rejected admission",
        _reader_refusal(
            _rejected, REJECTED_WARNING, SaveRejected, "idempotency_conflict"
        ),
    ),
    (
        "returned rejected admission, core without the mark",
        _reader_refusal(
            _rejected,
            REJECTED_WARNING,
            SaveRejected,
            "idempotency_conflict",
            False,
            "smartmemory.refusal_logging",
        ),
    ),
    (
        "returned receipt that is not a save",
        _reader_refusal(
            _stage_job_receipt,
            "Save return save refused: receipt subject stage_job is not a save; no id returned",
        ),
    ),
    (
        "returned receipt wire that fails validation",
        _reader_refusal(
            lambda: _wire(subject={"item_id": "test_item_u442"}),
            "Save return save refused: save return is not a valid receipt; no id returned",
        ),
    ),
    (
        "returned receipt of an unsupported major version",
        _reader_refusal(
            lambda: _wire(schema_version="2.0"),
            "Save return save refused: receipt refused by the reader (unsupported_version); no id returned",
        ),
    ),
    (
        "returned receipt the installed core cannot read",
        _reader_refusal(
            lambda: _wire(),
            "Save return save refused: the installed core cannot read save receipts; no id returned",
            missing_module="smartmemory.save.receipt",
        ),
    ),
]


class Ctx:
    def __init__(self, monkeypatch, tmp_path, addfinalizer):
        self.monkeypatch, self.tmp_path, self.addfinalizer = (
            monkeypatch,
            tmp_path,
            addfinalizer,
        )


@pytest.mark.parametrize(
    "label, build", BOUNDARY_ROWS, ids=[r[0] for r in BOUNDARY_ROWS]
)
def test_every_new_failure_boundary_is_one_typed_marked_refusal(
    label, build, monkeypatch, tmp_path, caplog, request
):
    from smartmemory.refusal_logging import refusal_logged

    act, error, code, warnings, marked = build(
        Ctx(monkeypatch, tmp_path, request.addfinalizer)
    )
    with caplog.at_level(logging.WARNING), pytest.raises(error) as caught:
        act()
    if code is not None:
        assert caught.value.code == code, label
    assert _warning_records(caplog) == warnings, label
    assert refusal_logged(caught.value) is marked, label
