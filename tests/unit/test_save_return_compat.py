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


#: The only dynamic loading in the wrapper that is not a ``storage.ingest`` consumer: ``work_graph.py`` names core
#: modules (and one wrapper helper, ``_resolve_data_dir``) in the ``STATUS_IMPORTS`` table and imports them by name.
#: The test below proves the table never names ``ingest``, which is what makes the allowance sound.
DYNAMIC_IMPORT_ALLOWED = {
    ("work_graph.py", "<module>"),
    ("work_graph.py", "_core_names"),
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
