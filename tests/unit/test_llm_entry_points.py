"""TOUR-ASK-502-1 fix round 1: every process entry point that can reach an LLM
applies the configured provider before its first LLM use.

Core's ``get_default_model()`` ranks an importable Claude Agent SDK above every
provider key, so an entry point that never runs ``apply_runtime_config()`` sends
a ``groq`` setup to the ``claude`` CLI. Each row drives one entry point under a
groq config with the SDK forced importable and no ``SMARTMEMORY_LLM_MODEL``, and
asserts the model core would use at that point. Provider keys are neutered; no
network, no daemon and no keychain is reached.
"""

from __future__ import annotations

import ast
import builtins
import json
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

from smartmemory_app.config import LLM_KEY_ENV_VARS, SmartMemoryConfig

APP = Path(__file__).resolve().parents[2] / "smartmemory_app"
GROQ_DEFAULT = "openai/gpt-oss-120b"


class _Seen(list):
    """Models observed at hand-off, plus the two stubs that record them."""

    record = None
    stop = None


class _Stop(Exception):
    """Raised by a stub to end an entry point right after the model is read."""


@pytest.fixture(autouse=True)
def _groq_config_sdk_importable(monkeypatch, tmp_path):
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import config, storage

    for name in (*LLM_KEY_ENV_VARS, "GEMINI_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(name, "")
    # Entry points write os.environ directly: setenv first so teardown restores.
    for name in (
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = SmartMemoryConfig(mode="local", llm_provider="groq")
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(storage, "load_config", lambda: cfg)
    monkeypatch.setattr(core_llm, "_claude_agent_sdk_available", lambda: True)


@pytest.fixture()
def seen(monkeypatch):
    """Record get_default_model() where each entry point first hands off to core."""
    import smartmemory.utils.llm as core_llm

    models = _Seen()

    def record(*args, **kwargs):
        models.append(core_llm.get_default_model())
        return 0

    monkeypatch.setattr("smartmemory.pipeline.work_graph.worker.run_worker", record)

    def stop(*args, **kwargs):
        record()
        raise _Stop

    monkeypatch.setattr("smartmemory.tools.factory.create_lite_memory", stop)
    models.record, models.stop = record, stop
    return models


# ---------------------------------------------------------------------------
# One driver per entry point. Each runs the real entry code up to the hand-off.
# ---------------------------------------------------------------------------


def _sm_worker(*args):
    def drive(seen, monkeypatch):
        from click.testing import CliRunner

        from smartmemory_app.cli import cli

        result = CliRunner().invoke(cli, ["worker", *args])
        assert result.exit_code == 0, result.output

    return drive


def _worker_entry_main(seen, monkeypatch):
    from smartmemory_app import runtime_diagnostics, worker_entry

    monkeypatch.setattr(
        runtime_diagnostics, "install_daemon_diagnostics", lambda *a, **k: None
    )
    monkeypatch.setattr(sys, "argv", ["worker_entry"])
    worker_entry.main()


def _viewer_server_main(seen, monkeypatch):
    import atexit

    from smartmemory_app import (
        crash_reporter,
        events_server,
        hook_failures,
        runtime_diagnostics,
        setup,
        viewer_server,
    )

    monkeypatch.setitem(
        sys.modules, "keyring", types.SimpleNamespace(get_password=lambda *a: None)
    )
    monkeypatch.setattr(setup, "_read_env_from_profile", lambda name: None)
    monkeypatch.setattr(
        runtime_diagnostics, "install_daemon_diagnostics", lambda *a, **k: None
    )
    monkeypatch.setattr(crash_reporter, "flush_in_background", lambda: None)
    monkeypatch.setattr(hook_failures, "consume_in_background", lambda *a: None)
    monkeypatch.setattr(events_server, "start_background", lambda: None)
    monkeypatch.setattr(atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(viewer_server, "_print_enrichment_status", lambda: None)
    monkeypatch.setattr(viewer_server, "_start_background_warmup", lambda: None)
    monkeypatch.setattr(viewer_server.uvicorn, "Server", lambda config: object())
    monkeypatch.setattr(viewer_server, "_run_until_stopped", seen.record)
    viewer_server.main(open_browser=False)


def _library_get_memory(seen, monkeypatch):
    from smartmemory_app import storage

    monkeypatch.setattr(storage, "is_configured", lambda: True)
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    with pytest.raises(_Stop):
        storage.get_memory()


def _capture_worker_memory(seen, monkeypatch):
    from smartmemory_app import capture_worker

    # MEM-SIZE-1: the Groq pin applies only on the per-source AI opt-in; costless capture has no LLM route.
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    monkeypatch.setenv("SMARTMEMORY_INGEST_AI_SUMMARY", "claude_code_transcript")
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    with pytest.raises(_Stop):
        capture_worker._memory()


ENTRY_ROWS = [
    # (id, module under smartmemory_app, driver)
    ("sm worker", "cli", _sm_worker()),
    ("sm worker --loop", "cli", _sm_worker("--loop")),
    ("worker_entry.main", "worker_entry", _worker_entry_main),
    ("viewer_server.main", "viewer_server", _viewer_server_main),
    ("storage.get_memory (add, ingest, hooks, MCP)", "storage", _library_get_memory),
]

# Entry modules that never reach an LLM, each with the reason.
EXEMPT = {
    "__main__": "delegates to cli.main",
    "diagnostic_worker": "offline store diagnostics, no LLM call",
    "enrichment_worker": "retired consumer, only logs a warning",
    "setup": "wizard: no call_llm; its first-run helper applies the config itself",
    # TOUR-ASK-FU-H1: never calls apply_runtime_config() and overwrites
    # SMARTMEMORY_LLM_MODEL (capture_worker.py), on purpose. Product policy since
    # DEMO-CC-UPLIFT-1 6a017f4: production capture is pinned to Groq, never to
    # subscription billing, whatever provider the user configured. MEM-SIZE-1: capture is
    # costless by default (no LLM route); the pin applies only on the AI opt-in.
    # CAPTURE-HOLD-ALL-1 release-held CLI: moves held event files and spawns the
    # capture worker (exempt below); it makes no LLM call itself.
    "capture_queue": "hold release CLI: moves queue files, no LLM call",
    "capture_worker": "deliberate Groq pin only on the AI opt-in, not the configured provider; costless by default (policy)",
}


@pytest.mark.parametrize(
    "driver", [row[2] for row in ENTRY_ROWS], ids=[row[0] for row in ENTRY_ROWS]
)
def test_entry_point_pins_configured_provider(seen, monkeypatch, driver):
    driver(seen, monkeypatch)

    assert seen == [GROQ_DEFAULT]  # never "sonnet", the Agent SDK route


def test_capture_worker_exemption_is_the_groq_pin(seen, monkeypatch):
    """The exemption holds only while the worker really pins Groq itself."""
    assert "Groq pin" in EXEMPT["capture_worker"]
    assert "capture_worker" not in {row[1] for row in ENTRY_ROWS}
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", "user-picked-model")

    _capture_worker_memory(seen, monkeypatch)

    assert seen == [GROQ_DEFAULT]  # overwritten, config never applied


# ---------------------------------------------------------------------------
# Completeness: a new entry point cannot ship without a row or an exemption.
# ---------------------------------------------------------------------------


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(
            isinstance(c, ast.Constant) and c.value == "__main__"
            for c in test.comparators
        )
    )


def _entry_modules() -> set[str]:
    modules = set()
    for path in APP.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.If) and _is_main_guard(n) for n in tree.body):
            modules.add(path.stem)
    pyproject = tomllib.loads(
        (APP.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    for target in pyproject["project"]["scripts"].values():
        modules.add(target.split(":")[0].rsplit(".", 1)[-1])
    return modules


def test_every_entry_module_has_a_row_or_an_exemption():
    covered = {row[1] for row in ENTRY_ROWS}
    assert _entry_modules() <= covered | set(EXEMPT), (
        "entry module without an LLM-pin row or exemption: "
        f"{sorted(_entry_modules() - covered - set(EXEMPT))}"
    )
    assert not covered & set(EXEMPT)


def test_only_worker_entry_starts_core_run_worker():
    starters = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            if "run_worker" in names:
                starters.add(path.stem)
    assert starters == {"worker_entry"}, (
        "start core's worker through worker_entry.run_configured_worker, which "
        f"applies the provider config first: {sorted(starters)}"
    )


# ---------------------------------------------------------------------------
# TOUR-ASK-FU-M1: completeness per Click command, not per module.
# ---------------------------------------------------------------------------

# Names that start core's LLM route (or ask which model it would use).
LLM_SINKS = {
    "call_llm",
    "get_default_model",
    "create_lite_memory",
    "run_worker",
    "llm_route_available",
}
# Wrapper functions, as (module, name), that run apply_runtime_config() before any
# of the sinks above. A call earns applier credit only when the called name is
# bound to one of these, never because of how the call is spelled.
CONFIG_APPLIERS = {
    ("storage", "apply_runtime_config"),
    ("storage", "get_memory"),
    ("worker_entry", "run_configured_worker"),
}

# "module:command" -> reason. A command that reaches a sink without an applier.
COMMAND_EXEMPT: dict[str, str] = {}


def _app_sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in APP.glob("*.py")}


def _dotted(node: ast.AST) -> list[str] | None:
    """``['a', 'b', 'c']`` for a plain ``a.b.c`` Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPES = (*_DEFS, ast.Lambda, ast.ClassDef, *_COMPS)


def _runs_outside(node: ast.AST) -> list[ast.AST]:
    """Parts of a nested scope node that the ENCLOSING scope evaluates: decorators,
    defaults, class bases, a comprehension's first iterable."""
    if isinstance(node, (*_DEFS, ast.Lambda)):
        args = node.args
        out = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
        return out + list(getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    return [node.generators[0].iter]


def _runs_inside(node: ast.AST) -> list[ast.AST]:
    """The code a nested scope node runs in its own scope."""
    if isinstance(node, (*_DEFS, ast.ClassDef)):
        return list(node.body)
    if isinstance(node, ast.Lambda):
        return [node.body]
    elts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    for i, gen in enumerate(node.generators):
        elts += [gen.target, *gen.ifs] + ([gen.iter] if i else [])
    return elts


def _scope_nodes(body: list) -> list[ast.AST]:
    """Nodes of one scope: ``body`` without descending into nested scopes, plus the
    parts of each nested scope that run here (``_runs_outside``)."""
    out: list[ast.AST] = []
    stack = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, _SCOPES):
            stack.extend(_runs_outside(node))
        else:
            stack.extend(ast.iter_child_nodes(node))
    return out


def _arg_names(node: ast.AST) -> list[str]:
    if not isinstance(node, (*_DEFS, ast.Lambda)):
        return []
    return [a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)]


class _Scope:
    """One Python scope and the raw binding specs of every name it binds.

    A spec is ``("def", module, qualname)``, ``("func", module, name)`` (a name
    imported from a wrapper module), ``("pkgattr", name)`` (``from smartmemory_app
    import name``), ``("module", stem)``, ``("package",)``,
    ``("ext", "dotted.path")`` (anything outside ``smartmemory_app``),
    ``("alias", value_node, scope)`` (``x = a.b``), ``("pending", spec, stmt,
    scope)`` (an import, until ``_CallGraph`` settles it to ``spec`` or to
    ``("caught", spec)`` when ``_Swallow`` finds its ImportError may be swallowed) or
    ``("opaque",)`` (an argument, a loop target, any other value).
    """

    def __init__(self, kind, module, qual, parent, node):
        self.kind, self.module, self.qual = kind, module, qual
        self.parent, self.node = parent, node
        self.binds: dict[str, list[tuple]] = {}
        self.declared: dict[str, _Scope] = {}  # global / nonlocal name -> owner
        self.nodes: list[ast.AST] = []
        self.children: list[_Scope] = []
        self.star = False  # ``from m import *`` binds names no one can list

    def bind(self, name: str, spec: tuple) -> None:
        owner = self.declared.get(name, self)
        owner.binds.setdefault(name, []).append(spec)

    def lookup(self, name: str) -> list[tuple] | None:
        """Python's rule: own scope, enclosing functions (never a class body),
        the module. ``global``/``nonlocal`` go straight to their owner."""
        scope = self
        while scope is not None:
            if name in scope.declared:
                return scope.declared[name].binds.get(name)
            if name in scope.binds and (scope is self or scope.kind != "class"):
                return scope.binds[name]
            scope = scope.parent
        return None


def _app_path(node: ast.ImportFrom) -> str | None:
    """The dotted path under ``smartmemory_app`` that a from-import names (``""`` for
    the package itself), or None when it leaves the package. Every scanned module
    sits directly in the package, so ``.x`` is ``smartmemory_app.x``."""
    mod = node.module or ""
    if node.level == 0:
        head, _, rest = mod.partition(".")
        return rest if head == "smartmemory_app" else None
    return mod if node.level == 1 else None


def _import_specs(node: ast.AST, modules: set[str]) -> list[tuple[str, tuple]]:
    """``(bound name, spec)`` for every name an import statement binds."""
    out = []
    if isinstance(node, ast.ImportFrom):
        rel = _app_path(node)
        for alias in node.names:
            if alias.name == "*":
                continue  # no name to bind: ``_Scope.star`` makes the command refuse
            bound = alias.asname or alias.name
            if rel is None:
                dots = "." * node.level
                spec = ("ext", f"{dots}{node.module or ''}.{alias.name}")
            elif rel == "":
                spec = ("pkgattr", alias.name)
            elif "." in rel:
                # A sub-package is never a scanned module: it has no identity and
                # earns no credit, however its last component is spelled.
                spec = ("ext", f"smartmemory_app.{rel}.{alias.name}")
            else:
                spec = ("func", rel, alias.name)
            out.append((bound, spec))
    elif isinstance(node, ast.Import):
        for alias in node.names:
            parts = alias.name.split(".")
            app = parts[0] == "smartmemory_app"
            if not alias.asname:
                out.append((parts[0], ("package",) if app else ("ext", parts[0])))
            elif not app:
                out.append((alias.asname, ("ext", alias.name)))
            elif len(parts) == 1:
                out.append((alias.asname, ("package",)))
            elif len(parts) == 2 and parts[1] in modules:
                out.append((alias.asname, ("module", parts[1])))
            else:
                out.append((alias.asname, ("opaque",)))
    return out


_TRIES = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t)
_TRY_STAR = getattr(ast, "TryStar", ())
# Context managers whose ``__exit__`` never returns true, so they cannot swallow an
# exception raised in their body (each checked in CPython's contextlib source).
# Every other manager, ``contextlib.suppress`` and ``open`` included, may swallow.
NON_SWALLOWING_MANAGERS = {
    "contextlib.redirect_stdout",
    "contextlib.redirect_stderr",
    "contextlib.nullcontext",
}
# Decorators, applied as a call, that return the decorated function itself, so a
# decorated helper still runs its own body and lets its exceptions out (checked in
# CPython's functools source). Every other decorator may swallow: a decorated helper
# earns no applier credit unless each of its decorators is a call bound to one of
# these.
SAFE_DECORATORS = {"functools.wraps"}
# CLI-GUARD-HARDEN-2: the builtin and allow-list proofs read these namespaces. A
# scanned source that writes into them, or into any module namespace dynamically,
# voids those proofs everywhere and flags its own commands (``_namespace_writes``).
_PROOF_ROOTS = {
    "builtins",
    "__builtins__",
    *(p.split(".")[0] for p in NON_SWALLOWING_MANAGERS | SAFE_DECORATORS),
}
# Names and attributes that reach a namespace as a mapping or run code in one.
_DYNAMIC_NAMES = {
    "globals",
    "vars",
    "locals",
    "exec",
    "eval",
    "__import__",
    "builtins",
    "__builtins__",
}
_DYNAMIC_ATTRS = {"__dict__", "__setattr__", "__delattr__"}
# Spellings that fetch a module object at runtime, outside any lexical binding.
_MODULE_FETCHERS = {"modules", "import_module", "__import__"}
# Statements that cannot jump: a handler made of these and then a ``raise`` stops the
# command (if one of them raises, a different exception propagates instead).
_STRAIGHT = (
    ast.Expr,
    ast.Pass,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.Delete,
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.Assert,
)
_JUMPS = (ast.Return, ast.Break, ast.Continue)


def _within(child: ast.AST, block: list) -> bool:
    return any(child is stmt for stmt in block)


class _Swallow:
    """Can an exception raised at ``node`` be swallowed so that the code after it
    runs on? Walks from ``node`` up to its function (a nested def's exceptions leave
    through its callers, which are checked at their call sites). ``kind`` is what is
    propagating: ``"import"`` (an ImportError) or ``"any"`` (an exception the guard
    cannot name, such as one a handler raised instead). Swallowed, fail closed, when
    on the way up:

    - a ``try`` whose body holds it has a handler that may catch ``kind`` and does not
      stop: its top-level statements are not straight-line statements and then a
      ``raise`` (``break``, ``continue``, ``return``, an ``if``, a loop or no
      ``raise`` at all before the end), or the straight-line part may reach a sink or
      a scanned function. A stopping handler that re-raises anything but a bare
      ``raise`` after only ``pass``, or any ``except*`` handler, propagates ``"any"``;
    - a ``finally`` it passes through holds a ``return``, ``break`` or ``continue``
      anywhere in its own scope; any other non-``pass`` ``finally`` makes ``"any"``;
    - a ``with`` whose body holds it has a manager that is not a call bound to
      ``NON_SWALLOWING_MANAGERS`` (``contextlib.suppress`` in any spelling, a
      local ``contextmanager``, ``open``, ``ExitStack``: anything else).

    A handler catches ``kind="import"`` when it is bare, names ImportError, a
    subclass or a base, or names anything not provably the builtin: an attribute, a
    starred or computed member, or a name bound in any enclosing scope (class bodies
    included), under a wildcard import, or while ``proofs_lost``.
    ``kind="any"`` is caught by every handler.

    ``proofs_lost`` (some scanned source writes a namespace dynamically, see
    ``_namespace_writes``) also voids every manager and decorator allow-list proof."""

    def __init__(self, trees, funcs, resolver, proofs_lost):
        self.parents: dict[int, ast.AST] = {}
        for tree in trees:
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    self.parents[id(child)] = node
        self.funcs, self.resolver = funcs, resolver
        self.proofs_lost = proofs_lost

    def may_swallow(self, node: ast.AST, scope: _Scope, kind: str) -> bool:
        child, parent = node, self.parents.get(id(node))
        while parent is not None:
            if isinstance(parent, _DEFS) and _within(child, parent.body):
                return False
            if isinstance(parent, ast.Lambda) and child is parent.body:
                return False
            if isinstance(parent, _TRIES):
                if _within(child, parent.body):
                    for handler in parent.handlers:
                        if not self._catches(handler, kind, scope):
                            continue
                        out = self._stops(handler, scope)
                        if out is None:
                            return True
                        if out == "any" or isinstance(parent, _TRY_STAR):
                            kind = "any"
                if parent.finalbody and not _within(child, parent.finalbody):
                    final = _scope_nodes(parent.finalbody)
                    if any(isinstance(n, _JUMPS) for n in final):
                        return True
                    if not all(isinstance(s, ast.Pass) for s in parent.finalbody):
                        kind = "any"
            elif isinstance(parent, (ast.With, ast.AsyncWith)) and _within(
                child, parent.body
            ):
                if not all(self._safe_manager(i, scope) for i in parent.items):
                    return True
            child, parent = parent, self.parents.get(id(parent))
        return False

    def _builtin(self, name: str, scope: _Scope) -> bool:
        if self.proofs_lost:
            return False
        while scope is not None:
            if name in scope.binds or name in scope.declared or scope.star:
                return False
            scope = scope.parent
        return True

    def _catches(self, handler: ast.ExceptHandler, kind: str, scope: _Scope) -> bool:
        if kind != "import" or handler.type is None:
            return True
        types_ = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        for expr in types_:
            if not isinstance(expr, ast.Name) or not self._builtin(expr.id, scope):
                return True
            cls = getattr(builtins, expr.id, None)
            if not (isinstance(cls, type) and issubclass(cls, BaseException)):
                return True
            if issubclass(cls, ImportError) or issubclass(ImportError, cls):
                return True
        return False

    def _stops(self, handler: ast.ExceptHandler, scope: _Scope) -> str | None:
        """None when the handler may fall through, else the kind it propagates."""
        body = handler.body
        n = 0
        while n < len(body) and isinstance(body[n], _STRAIGHT):
            n += 1
        if n == len(body) or not isinstance(body[n], ast.Raise):
            return None
        # Everything the handler evaluates before it leaves: the straight-line part,
        # the raised exception and its cause, and the lambdas and comprehensions in
        # them (a lambda that is called there runs here; one that is not is refused
        # all the same).
        for sub in (s for stmt in body[: n + 1] for s in ast.walk(stmt)):
            if isinstance(sub, (ast.Yield, ast.YieldFrom)):
                return None
            if isinstance(sub, (ast.Name, ast.Attribute)):
                spelled = sub.id if isinstance(sub, ast.Name) else sub.attr
                idents = self.resolver.expr(sub, scope)
                if spelled in LLM_SINKS or any(
                    _is_sink(i) or (i[0] == "func" and i[1:] in self.funcs)
                    for i in idents
                ):
                    return None  # the handler may reach an LLM before it stops
        bare = body[n].exc is None and all(isinstance(s, ast.Pass) for s in body[:n])
        return "import" if bare else "any"

    def _safe_manager(self, item: ast.withitem, scope: _Scope) -> bool:
        return self._allowed_call(item.context_expr, scope, NON_SWALLOWING_MANAGERS)

    def plain(self, fn: _Scope) -> bool:
        """``fn`` is called as written: each decorator is a call bound to
        ``SAFE_DECORATORS``. Any other decorator (a bare name, an attribute, a
        class, a local ``contextmanager``, a rebound allow-listed name) may wrap the
        body in a catcher, so ``fn`` lends no applier credit to its callers."""
        return all(
            self._allowed_call(dec, fn.parent, SAFE_DECORATORS)
            for dec in getattr(fn.node, "decorator_list", [])
        )

    def _allowed_call(self, call: ast.AST, scope: _Scope, allowed: set) -> bool:
        if self.proofs_lost or not isinstance(call, ast.Call):
            return False
        idents = self.resolver.expr(call.func, scope)
        return bool(idents) and all(i[0] == "ext" and i[1] in allowed for i in idents)


def _namespace_writes(nodes: list, scope: _Scope, resolver: _Resolver) -> list[str]:
    """Where ``nodes``, read in ``scope``, may write a namespace the guard's proofs
    read, as ``"L<line> <code>"``. A flat refusal, not a proof (CLI-GUARD-HARDEN-2):

    - any use of ``globals``, ``vars``, ``locals``, ``exec``, ``eval``,
      ``__import__``, ``builtins`` or ``__builtins__``, or of a ``__dict__``,
      ``__setattr__`` or ``__delattr__`` attribute;
    - an attribute store or delete, or a ``setattr``/``delattr`` call, on a target
      that is not a dotted name, or that resolves to a scanned module, the package,
      or a module under ``_PROOF_ROOTS`` (``contextlib.nullcontext = ...``,
      ``storage.get_memory = ...``), or whose expression fetches a module at
      runtime (``sys.modules[...]``, ``import_module(...)``).

    Stores on other imported modules stay allowed: shipped code patches
    ``smartmemory.utils.llm``, ``socket``, ``sys`` and ``huggingface_hub``, and no
    proof reads those namespaces."""
    out = []

    def risky(target: ast.AST) -> bool:
        if _dotted(target) is None:
            return any(
                (isinstance(n, ast.Name) and n.id in _MODULE_FETCHERS)
                or (isinstance(n, ast.Attribute) and n.attr in _MODULE_FETCHERS)
                or (
                    isinstance(n, (ast.Name, ast.Attribute))
                    and _dotted(n) is not None
                    and risky(n)
                )
                for n in ast.walk(target)
                if n is not target
            )
        for ident in resolver.expr(target, scope):
            while ident[0] == "caught" and len(ident) > 1:
                ident = ident[1]
            if ident[0] in ("module", "package"):
                return True
            if ident[0] == "ext" and ident[1].split(".")[0] in _PROOF_ROOTS:
                return True
        return False

    for node in nodes:
        hit = (
            (isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES)
            or (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_ATTRS)
            or (
                isinstance(node, ast.alias)
                and node.name.split(".")[0] in _DYNAMIC_NAMES
            )
            or (isinstance(node, ast.ImportFrom) and node.module == "builtins")
            or (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and risky(node.value)
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("setattr", "delattr")
                and bool(node.args)
                and risky(node.args[0])
            )
        )
        if hit:
            out.append(f"L{getattr(node, 'lineno', '?')} {ast.unparse(node)[:60]}")
    return out


def _build_scope(kind, module, qual, parent, node, body, modules, funcs):
    scope = _Scope(kind, module, qual, parent, node)
    if parent is not None:
        parent.children.append(scope)
    if kind == "function":
        funcs[(module, qual)] = scope
    scope.nodes = _scope_nodes(body)
    scope.star = any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in scope.nodes
    )
    for stmt in scope.nodes:
        if isinstance(stmt, ast.Global):
            root = scope
            while root.parent is not None:
                root = root.parent
            scope.declared.update(dict.fromkeys(stmt.names, root))
        elif isinstance(stmt, ast.Nonlocal):
            for name in stmt.names:
                owner = parent
                while owner is not None and (
                    owner.kind in ("class", "module") or name not in owner.binds
                ):
                    owner = owner.parent
                scope.declared[name] = owner or parent
    for name in _arg_names(node):
        scope.bind(name, ("opaque",))
    aliases: dict[int, ast.AST] = {}
    walrus: set[int] = set()
    for stmt in scope.nodes:
        if isinstance(stmt, ast.Assign) and _dotted(stmt.value):
            for target in stmt.targets:
                aliases[id(target)] = stmt.value
        elif isinstance(stmt, (ast.AnnAssign, ast.NamedExpr)):
            if stmt.value is not None and _dotted(stmt.value):
                aliases[id(stmt.target)] = stmt.value
            if isinstance(stmt, ast.NamedExpr):
                walrus.add(id(stmt.target))
    prefix = f"{qual}." if qual else ""
    defined: dict[str, int] = {}
    for stmt in scope.nodes:
        if isinstance(stmt, (*_DEFS, ast.ClassDef)):
            defined[stmt.name] = defined.get(stmt.name, 0) + 1

    def own_qual(stmt) -> str:
        """Each definition has its own identity: a name defined more than once in a
        scope is told apart by line, so no body replaces another's."""
        return (
            prefix + stmt.name + (f"@L{stmt.lineno}" if defined[stmt.name] > 1 else "")
        )

    for stmt in scope.nodes:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for bound, spec in _import_specs(stmt, modules):
                # Settled by ``_CallGraph`` once every scope is built: ``("caught",
                # spec)`` when a swallowed ImportError may have left it unbound.
                scope.bind(bound, ("pending", spec, stmt, scope))
        elif isinstance(stmt, _DEFS):
            scope.bind(stmt.name, ("def", module, own_qual(stmt)))
        elif isinstance(stmt, ast.ClassDef):
            scope.bind(stmt.name, ("opaque",))
        elif isinstance(stmt, ast.Name) and isinstance(stmt.ctx, (ast.Store, ast.Del)):
            home = scope
            while id(stmt) in walrus and home.kind == "comp":
                home = home.parent  # a walrus in a comprehension binds outside it
            value = aliases.get(id(stmt))
            home.bind(stmt.id, ("alias", value, scope) if value else ("opaque",))
        elif isinstance(stmt, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            if stmt.name:
                scope.bind(stmt.name, ("opaque",))
        elif isinstance(stmt, ast.MatchMapping) and stmt.rest:
            scope.bind(stmt.rest, ("opaque",))
    for stmt in scope.nodes:
        if isinstance(stmt, _SCOPES):
            child_kind = {
                ast.FunctionDef: "function",
                ast.AsyncFunctionDef: "function",
                ast.Lambda: "lambda",
                ast.ClassDef: "class",
            }.get(type(stmt), "comp")
            child_qual = own_qual(stmt) if child_kind in ("function", "class") else qual
            _build_scope(
                child_kind,
                module,
                child_qual,
                scope,
                stmt,
                _runs_inside(stmt),
                modules,
                funcs,
            )
    return scope


class _Resolver:
    """What a name or dotted chain is bound to, as canonical identities:
    ``("func", module, qualname)``, ``("unresolved", module, name)`` (no such
    module or export in the scanned sources), ``("module", stem)``, ``("package",)``,
    ``("ext", "dotted.path")``, ``("opaque",)`` or ``("unbound", name)``. A name that
    a swallowed ImportError may have left unbound also resolves to ``("caught",)``:
    it still names what it names (a sink stays a sink) and can earn no applier credit.
    A wrapper name imported from a module in ``sources`` is followed to what that
    module binds it to (re-exports and aliases included)."""

    def __init__(self, modules, module_scopes):
        self.modules, self.module_scopes = modules, module_scopes

    def name(self, name, scope, seen=frozenset()):
        specs = scope.lookup(name)
        if specs is None:
            return {("unbound", name)}
        return set().union(*(self.spec(s, seen) for s in specs))

    def spec(self, spec, seen):
        kind = spec[0]
        if kind == "pending":
            # Only while ``_CallGraph`` settles imports (manager and handler names).
            return self.spec(spec[1], seen)
        if kind == "caught":
            return self.spec(spec[1], seen) | {("caught",)}
        if kind == "def":
            return {("func", spec[1], spec[2])}
        if kind == "func":
            return self.export(spec[1], spec[2], seen)
        if kind == "pkgattr":
            return self.package_attr(spec[1], seen)
        if kind == "alias":
            key = ("alias", id(spec[1]))
            if key in seen:
                return {("opaque",)}
            return self.expr(spec[1], spec[2], seen | {key})
        return {spec} if kind in ("ext", "module", "package") else {("opaque",)}

    def export(self, module, name, seen):
        scope = self.module_scopes.get(module)
        key = ("export", module, name)
        if key in seen:
            return {("opaque",)}
        if scope is None or name not in scope.binds:
            # No such module or no such export in the scanned sources: nothing the
            # guard can credit. It still counts as a sink when it is spelled like one.
            return {("unresolved", module, name)}
        return self.name(name, scope, seen | {key})

    def package_attr(self, name, seen):
        """``smartmemory_app.<name>``: what ``__init__`` binds it to, else the
        submodule of that name (Python's order for ``from package import name``)."""
        init = self.module_scopes.get("__init__")
        if init is not None and name in init.binds:
            return self.export("__init__", name, seen)
        return {("module", name)}

    def attr(self, ident, attr, seen):
        if ident[0] == "caught":
            return {ident}
        if ident[0] == "ext":
            return {("ext", f"{ident[1]}.{attr}")}
        if ident[0] == "package":
            return self.package_attr(attr, seen)
        if ident[0] == "module":
            return self.export(ident[1], attr, seen)
        return {("opaque",)}

    def expr(self, node, scope, seen=frozenset()):
        parts = _dotted(node)
        if parts is None:
            return {("opaque",)}
        out = self.name(parts[0], scope, seen)
        for attr in parts[1:]:
            out = set().union(*(self.attr(i, attr, seen) for i in out))
        return out


def _is_sink(ident: tuple) -> bool:
    if ident[0] == "ext":
        return ident[1].rsplit(".", 1)[-1] in LLM_SINKS
    return ident[0] in ("func", "unresolved") and (
        ident[2].rsplit(".", 1)[-1] in LLM_SINKS
    )


def _is_applier(ident: tuple, funcs) -> bool:
    """A trusted applier that exists in the scanned sources under its full path."""
    return ident[0] == "func" and ident[1:] in CONFIG_APPLIERS and ident[1:] in funcs


def _is_click_command(fn: ast.AST, scope: _Scope, resolver: _Resolver) -> bool:
    """``@<group>.command``/``.group`` (any object), or a decorator bound to
    ``click.command``/``click.group`` however it was imported or renamed."""
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in {"command", "group"}:
            return True
        for ident in resolver.expr(target, scope):
            path = ident[1].split(".") if ident[0] == "ext" else []
            if path[:1] == ["click"] and path[-1] in {"command", "group"}:
                return True
    return False


def _all_scopes(scopes):
    for scope in scopes:
        yield scope
        yield from _all_scopes(scope.children)


def _star_in(scope: _Scope) -> bool:
    """A wildcard import in ``scope`` or in any scope below it."""
    return scope.star or any(_star_in(child) for child in scope.children)


class _CallGraph:
    """Per wrapper function: sinks it names, appliers it calls (by resolved
    identity), resolved call targets, its call and reference edges, and whether a
    wildcard import in reach makes its names unresolvable (``wild``)."""

    def __init__(self, sources: dict[str, str]):
        modules = set(sources)
        self.funcs: dict[tuple[str, str], _Scope] = {}
        module_scopes = {}
        trees = []
        for module, source in sources.items():
            tree = ast.parse(source)
            trees.append(tree)
            module_scopes[module] = _build_scope(
                "module", module, "", None, tree, tree.body, modules, self.funcs
            )
        self.resolver = _Resolver(modules, module_scopes)
        self.dynamic: dict[str, list[str]] = {}
        for (module, scope), tree in zip(module_scopes.items(), trees):
            scopes = list(_all_scopes([scope]))
            owned = {id(n) for s in scopes for n in s.nodes}
            # Annotations run at ``def`` time but no scope owns them: read them in
            # the module scope.
            loose = [n for n in ast.walk(tree) if id(n) not in owned]
            writes = [
                w for s in scopes for w in _namespace_writes(s.nodes, s, self.resolver)
            ]
            writes += _namespace_writes(loose, scope, self.resolver)
            if writes:
                self.dynamic[module] = writes
        self.swallow = _Swallow(
            trees, self.funcs, self.resolver, proofs_lost=bool(self.dynamic)
        )
        settled = {}
        for scope in _all_scopes(module_scopes.values()):
            for specs in scope.binds.values():
                for i, spec in enumerate(specs):
                    if spec[0] == "pending":
                        key = id(spec[2])
                        if key not in settled:
                            settled[key] = self.swallow.may_swallow(
                                spec[2], spec[3], "import"
                            )
                        specs[i] = ("caught", spec[1]) if settled[key] else spec[1]
        self.plain = {
            key: self.swallow.plain(scope) for key, scope in self.funcs.items()
        }
        self.commands = sorted(
            key
            for key, scope in self.funcs.items()
            if _is_click_command(scope.node, scope.parent, self.resolver)
        )
        self.sinks = {key: set() for key in self.funcs}
        self.appliers = {key: set() for key in self.funcs}
        self.calls = {key: set() for key in self.funcs}
        self.call_edges = {key: set() for key in self.funcs}
        self.call_groups = {key: [] for key in self.funcs}
        self.ref_edges = {key: set() for key in self.funcs}
        self.wild = {}
        for key, scope in self.funcs.items():
            self._facts(key, scope, runs=True)
            up = scope
            while up is not None and not up.star:
                up = up.parent
            # A wildcard import or a dynamic namespace write in reach: the guard
            # cannot know what any name is bound to.
            self.wild[key] = up is not None or _star_in(scope) or key[0] in self.dynamic

    def _facts(self, key, scope, runs):
        """Facts of ``scope`` charged to function ``key``. ``runs`` is False inside
        a lambda: its body may never run, so it names sinks but credits nothing."""
        resolve = self.resolver.expr
        for node in scope.nodes:
            if isinstance(node, ast.ImportFrom):
                self.sinks[key] |= {a.name for a in node.names} & LLM_SINKS
            elif isinstance(node, ast.Call):
                idents = resolve(node.func, scope)
                hits = {i[1:] for i in idents if i[0] == "func" and i[1:] in self.funcs}
                if runs:
                    self.calls[key] |= idents
                # The callee's own imports may fail: a call whose ImportError (a direct
                # applier) or any exception (a wrapper may have raised another one in
                # its place) can be swallowed earns no credit.
                direct = bool(idents) and all(
                    _is_applier(i, self.funcs) for i in idents
                )
                kind = "import" if direct else "any"
                # A decorated callee lends credit only through ``SAFE_DECORATORS``:
                # any other decorator may catch what the body raises.
                trusted = (
                    runs
                    and all(self.plain[h] for h in hits)
                    and not self.swallow.may_swallow(node, scope, kind)
                )
                if trusted and direct:
                    self.appliers[key] |= {i[1:] for i in idents}
                if trusted and hits and len(hits) == len(idents):
                    # A name bound several ways donates sinks from every binding,
                    # and applier credit only for what every one of them earns.
                    self.call_edges[key] |= hits
                    self.call_groups[key].append(frozenset(hits))
                else:
                    self.ref_edges[key] |= hits
            elif isinstance(node, (ast.Name, ast.Attribute)):
                idents = resolve(node, scope)
                spelled = node.id if isinstance(node, ast.Name) else node.attr
                if spelled in LLM_SINKS:
                    self.sinks[key].add(spelled)
                self.sinks[key] |= {i[-1] for i in idents if _is_sink(i)}
                self.ref_edges[key] |= {
                    i[1:] for i in idents if i[0] == "func" and i[1:] in self.funcs
                }
        for child in scope.children:
            if child.kind == "function":
                # A nested def's sinks count, its appliers only when it is called.
                self.ref_edges[key].add((child.module, child.qual))
            else:
                self._facts(key, child, runs and child.kind != "lambda")

    def reachable(self) -> tuple[dict, dict, dict]:
        """Sinks, appliers and the wildcard flag per function after following the
        edges to a fixed point: sinks and the flag along call and reference edges,
        appliers along calls only (what every binding of the called name earns)."""
        sinks = {k: set(v) for k, v in self.sinks.items()}
        appliers = {k: set(v) for k, v in self.appliers.items()}
        wild = dict(self.wild)
        changed = True
        while changed:
            changed = False
            for key in self.funcs:
                for hit in self.call_edges[key] | self.ref_edges[key]:
                    if not sinks[hit] <= sinks[key]:
                        sinks[key] |= sinks[hit]
                        changed = True
                    if wild[hit] and not wild[key]:
                        wild[key] = changed = True
                for group in self.call_groups[key]:
                    earned = set.intersection(*(appliers[hit] for hit in group))
                    if not earned <= appliers[key]:
                        appliers[key] |= earned
                        changed = True
        return sinks, appliers, wild


def _unconfigured_llm_commands(sources: dict[str, str]) -> list[str]:
    """``module:command`` for every Click command whose reachable code starts an LLM
    sink and never calls a config applier.

    Every name is resolved to what it is bound to, scope by scope, the way Python
    does: the function's own bindings (imports, ``x = a.b`` aliases, arguments,
    loop and ``with`` targets, nested defs) shadow enclosing functions, which
    shadow the module; a class body is never an enclosing scope; ``global`` and
    ``nonlocal`` go to their owner. Imports follow ``import x as y``, ``from m
    import f as g``, ``m.f()`` and an aliased module ``h.f()``, and a name
    imported from a wrapper module follows what that module binds it to.

    A module is its full dotted path: ``smartmemory_app.untrusted.storage`` is not
    ``smartmemory_app.storage``. Every definition is its own identity, so a name
    defined twice in a scope is bound two ways and earns credit only if each
    definition does. A wildcard import in reach (the command's scopes, or a wrapper
    it reaches) makes the command an offender: it can rebind any name.

    A call earns applier credit only when its name resolves to a member of
    ``CONFIG_APPLIERS``, every way it is bound, and that function exists in the
    scanned sources under its full dotted path (a missing module, a missing export
    or an import that fails earns nothing), and the name is not bound by an import
    whose ImportError may be swallowed on any path (``_Swallow``: a catching handler
    that jumps or never raises, a ``finally`` that jumps, a ``with`` that may
    suppress, an enclosing catcher of what a handler raised instead, a handler name
    that may not be the builtin): the import may have failed and the command run on,
    wherever the call sits. The credited call itself must not sit where its own
    failure may be swallowed either (an ImportError for a direct applier, anything
    for a wrapper). A decorated function lends no credit to its callers unless each
    decorator is a call bound to ``SAFE_DECORATORS``. Not modelled: a target module
    whose own top-level import fails (the failure is not in this command's ``try``).

    Scope of the guarantee (CLI-GUARD-HARDEN-2): every proof above is over lexical
    bindings, what the source text binds each name to. Writes that change a binding
    without a lexical store are not modelled; they are refused flat instead
    (``_namespace_writes``): ``globals()``/``vars()``/``locals()``, ``exec``,
    ``builtins``, ``__dict__``, ``setattr``, or an attribute store on a scanned
    module or a module the proofs read (``contextlib``, ``functools``). A source
    that does any of these voids every builtin and allow-list proof in every scanned
    module, and each command in it, or reaching it, is an offender. Still out of
    reach: a module object bound through a plain variable from a runtime fetch
    (``m = importlib.import_module("contextlib")``, then ``m.nullcontext = ...``).
    A call that resolves to a sink is
    a sink call whatever it is spelled, and a name with no binding earns nothing.
    A sink also counts when it is merely named or imported, or when an attribute
    is spelled like one. Reachable code is the command body, comprehensions and
    class bodies in it, the wrapper functions it calls (appliers and sinks) or
    names (sinks only), and nested defs and lambdas (sinks only, unless called).
    Appliers are not expanded (storage.get_memory is a real sink holder, and is
    checked below). A command's decorators are not read.
    Blind spots: method calls on objects, calls through containers, and call order
    inside one command (an applier after the sink still passes).
    """
    graph = _CallGraph(sources)
    sinks, appliers, wild = graph.reachable()
    return [
        f"{module}:{name}"
        for module, name in graph.commands
        if wild[(module, name)]
        or (sinks[(module, name)] and not appliers[(module, name)])
    ]


def test_every_llm_command_applies_config_or_is_exempt():
    offenders = set(_unconfigured_llm_commands(_app_sources()))

    assert offenders <= set(COMMAND_EXEMPT), (
        "Click command reaches core's LLM without apply_runtime_config(): "
        f"{sorted(offenders - set(COMMAND_EXEMPT))}"
    )
    assert set(COMMAND_EXEMPT) <= offenders, "stale command exemption"


def test_config_appliers_really_apply_the_config():
    """Each applier calls the real apply_runtime_config() itself or in a helper one
    hop away, resolved by binding, not by spelling."""
    graph = _CallGraph(_app_sources())
    apply = ("storage", "apply_runtime_config")
    assert apply in graph.funcs
    for key in CONFIG_APPLIERS - {apply}:
        called = graph.calls[key]
        hops = called.union(
            *(
                graph.calls[i[1:]]
                for i in called
                if i[0] == "func" and i[1:] in graph.funcs
            )
        )
        assert ("func", *apply) in hops, key


_NEW_COMMAND = """

@cli.command("review-new-llm")
def review_new_llm():
    {body}
"""

# (id, extra module sources, text appended to cli.py, offending commands expected)
GUARD_ROWS = [
    (
        "review-m1 repro: call_llm straight in a new command",
        {},
        _NEW_COMMAND.format(
            body="from smartmemory.utils.llm import call_llm; call_llm(user_content='p')"
        ),
        ["cli:review_new_llm"],
    ),
    (
        "model lookup through a module attribute",
        {},
        _NEW_COMMAND.format(
            body="import smartmemory.utils.llm as m; m.get_default_model()"
        ),
        ["cli:review_new_llm"],
    ),
    (
        "same-module helper starts the memory factory",
        {},
        "\ndef _helper():\n    from smartmemory.tools.factory import create_lite_memory\n"
        "    return create_lite_memory('x')\n" + _NEW_COMMAND.format(body="_helper()"),
        ["cli:review_new_llm"],
    ),
    (
        "helper imported from a new wrapper module",
        {
            "newmod": "from smartmemory.utils.llm import call_llm\n"
            "def helper():\n    return call_llm(user_content='p')\n"
        },
        _NEW_COMMAND.format(body="from smartmemory_app.newmod import helper; helper()"),
        ["cli:review_new_llm"],
    ),
    (
        "core worker started directly",
        {},
        _NEW_COMMAND.format(
            body="from smartmemory.pipeline.work_graph.worker import run_worker; run_worker('d')"
        ),
        ["cli:review_new_llm"],
    ),
    (
        "control: applies the config first",
        {},
        _NEW_COMMAND.format(
            body="from smartmemory_app.storage import apply_runtime_config\n"
            "    apply_runtime_config()\n"
            "    from smartmemory.utils.llm import call_llm; call_llm(user_content='p')"
        ),
        [],
    ),
    (
        "control: goes through storage.get_memory",
        {},
        _NEW_COMMAND.format(
            body="from smartmemory_app.storage import get_memory; get_memory()"
        ),
        [],
    ),
    (
        "control: never touches an LLM",
        {},
        _NEW_COMMAND.format(body="print('hi')"),
        [],
    ),
]


@pytest.mark.parametrize(
    "extra, appended, expected",
    [row[1:] for row in GUARD_ROWS],
    ids=[row[0] for row in GUARD_ROWS],
)
def test_command_guard_catches_a_new_command_in_an_existing_module(
    extra, appended, expected
):
    sources = _app_sources()
    sources["cli"] += appended
    sources.update(extra)

    found = set(_unconfigured_llm_commands(sources)) - set(COMMAND_EXEMPT)

    assert found == set(expected)


# r1 HIGH-2: imports are bindings, not execution, and module-qualified calls resolve.
_CMD = '@cli.command("review")\ndef review_command():\n    {body}\n'
_SINK_HELPER = (
    "from smartmemory.utils.llm import call_llm\ndef invoke():\n    call_llm()\n"
)
_APPLYING_HELPER = (
    "from smartmemory_app.storage import apply_runtime_config\n"
    "from smartmemory.utils.llm import call_llm\n"
    "def invoke():\n    apply_runtime_config()\n    call_llm()\n"
)
_SINK = "from smartmemory.utils.llm import call_llm\n    call_llm()"
_OFFENDER = ["cli:review_command"]

# (id, command body, {helper module sources}, offending commands expected)
QUALIFIED_ROWS = [
    ("direct-control", _SINK, {}, _OFFENDER),
    (
        "unused-applier-import",  # reviewer's r1 row
        "from smartmemory_app.storage import get_memory\n    " + _SINK,
        {},
        _OFFENDER,
    ),
    (
        "unused-applier-import-qualified",
        "from smartmemory_app import storage\n    " + _SINK,
        {"storage": "def get_memory():\n    pass\n"},
        _OFFENDER,
    ),
    (
        "applier-named-not-called",
        "from smartmemory_app.storage import get_memory\n    get_memory\n    " + _SINK,
        {},
        _OFFENDER,
    ),
    (
        "qualified-wrapper-helper",  # reviewer's r1 row
        "from smartmemory_app import helper\n    helper.invoke()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "aliased-module-call",
        "import smartmemory_app.helper as h\n    h.invoke()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "from-import-aliased-module-call",
        "from smartmemory_app import helper as h\n    h.invoke()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "dotted-package-call",
        "import smartmemory_app.helper\n    smartmemory_app.helper.invoke()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "from-import-call",
        "from smartmemory_app.helper import invoke\n    invoke()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "from-import-renamed-call",
        "from smartmemory_app.helper import invoke as go\n    go()",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "sink-helper-only-named",
        "from smartmemory_app import helper\n    helper.invoke",
        {"helper": _SINK_HELPER},
        _OFFENDER,
    ),
    (
        "control-applier-called",
        "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
        + _SINK,
        {},
        [],
    ),
    (
        "control-qualified-helper-applies-config",
        "from smartmemory_app import helper\n    helper.invoke()",
        {"helper": _APPLYING_HELPER},
        [],
    ),
    (
        "control-aliased-helper-applies-config",
        "import smartmemory_app.helper as h\n    h.invoke()",
        {"helper": _APPLYING_HELPER},
        [],
    ),
    (
        "control-decorator-names-a-sink-group",
        "",
        {
            "cli": "from smartmemory.utils.llm import call_llm\n"
            "def parent():\n    call_llm()\n"
            '@parent.command("review")\ndef review_command():\n    print("hi")\n'
        },
        [],
    ),
    (
        "control-local-name-shadows-an-import",
        "",
        {
            "cli": "from smartmemory_app.helper import invoke\n"
            '@cli.command("review")\ndef review_command():\n'
            "    invoke = 1\n    print(invoke)\n",
            "helper": _SINK_HELPER,
        },
        [],
    ),
    (
        "unshadowed-import-is-called",
        "",
        {
            "cli": "from smartmemory_app.helper import invoke\n"
            '@cli.command("review")\ndef review_command():\n    invoke()\n',
            "helper": _SINK_HELPER,
        },
        _OFFENDER,
    ),
    (
        "applying-helper-only-named",
        "from smartmemory_app import helper\n    helper.invoke\n    " + _SINK,
        {"helper": _APPLYING_HELPER},
        _OFFENDER,
    ),
]


@pytest.mark.parametrize(
    "body, helpers, expected",
    [row[1:] for row in QUALIFIED_ROWS],
    ids=[row[0] for row in QUALIFIED_ROWS],
)
def test_every_unconfigured_click_command_is_detected(body, helpers, expected):
    sources = {**_TRUSTED, "cli": _CMD.format(body=body), **helpers}

    assert _unconfigured_llm_commands(sources) == expected


# r2 HIGH-2: applier credit follows what the called name is bound to, never its
# spelling. The first three rows are the reviewer's r2 repro.
_ALIASED_SINK = "from smartmemory.utils.llm import call_llm as {0}\n    {0}()"
_MODULE_GET_MEMORY = "from smartmemory_app.storage import get_memory\n"


def _cli(body: str, header: str = "") -> dict[str, str]:
    return {"cli": header + _CMD.format(body=body)}


# (id, module sources, offending commands expected)
ALIAS_ROWS = [
    ("get_memory", _cli(_ALIASED_SINK.format("get_memory")), _OFFENDER),
    (
        "apply_runtime_config",
        _cli(_ALIASED_SINK.format("apply_runtime_config")),
        _OFFENDER,
    ),
    (
        "run_configured_worker",
        _cli(_ALIASED_SINK.format("run_configured_worker")),
        _OFFENDER,
    ),
    (
        "control-local-import-get_memory",
        _cli(
            "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
            + _SINK
        ),
        [],
    ),
    (
        "control-local-import-apply_runtime_config",
        _cli(
            "from smartmemory_app.storage import apply_runtime_config\n"
            "    apply_runtime_config()\n    " + _SINK
        ),
        [],
    ),
    (
        "control-local-import-run_configured_worker",
        _cli(
            "from smartmemory_app.worker_entry import run_configured_worker\n"
            "    run_configured_worker()\n    " + _SINK
        ),
        [],
    ),
    (
        "control-module-level-applier-alias",
        _cli(
            "mem()\n    " + _SINK,
            "from smartmemory_app.storage import get_memory as mem\n",
        ),
        [],
    ),
    (
        "local-variable-shadows-applier-with-sink",
        _cli(
            "from smartmemory.utils.llm import call_llm\n"
            "    get_memory = call_llm\n    get_memory()",
            _MODULE_GET_MEMORY,
        ),
        _OFFENDER,
    ),
    (
        "module-level-sink-alias",
        _cli(
            "get_memory()",
            "from smartmemory.utils.llm import call_llm as get_memory\n",
        ),
        _OFFENDER,
    ),
    (
        "wrapper-module-reexports-a-sink",
        {
            **_cli("from smartmemory_app.helper import get_memory\n    get_memory()"),
            "helper": "from smartmemory.utils.llm import call_llm as get_memory\n",
        },
        _OFFENDER,
    ),
    (
        "applier-module-rebinds-the-name-to-a-sink",
        {
            **_cli("from smartmemory_app import storage\n    storage.get_memory()"),
            "storage": "from smartmemory.utils.llm import call_llm as get_memory\n",
        },
        _OFFENDER,
    ),
    (
        "nested-def-local-sink-alias",
        _cli(
            "def inner():\n"
            "        from smartmemory.utils.llm import call_llm as get_memory\n"
            "        get_memory()\n    inner()",
            _MODULE_GET_MEMORY,
        ),
        _OFFENDER,
    ),
    (
        "nested-def-applier-never-called",
        _cli(
            "from smartmemory_app.storage import get_memory\n"
            "    def later():\n        get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "control-nested-def-applier-called",
        _cli(
            "from smartmemory_app.storage import get_memory\n"
            "    def later():\n        get_memory()\n    later()\n    " + _SINK
        ),
        [],
    ),
    (
        "lambda-applier-may-never-run",
        _cli(
            "from smartmemory_app.storage import get_memory\n"
            "    later = lambda: get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    ("unbound-applier-name", _cli("get_memory()\n    " + _SINK), _OFFENDER),
    (
        "applier-spelled-on-an-object",
        _cli("ctx = object()\n    ctx.get_memory()\n    " + _SINK),
        _OFFENDER,
    ),
    (
        "control-aliased-applier-module",
        _cli(
            "from smartmemory_app import storage as st\n    st.get_memory()\n    "
            + _SINK
        ),
        [],
    ),
    (
        "two-bindings-one-a-sink",
        _cli(
            "from smartmemory_app.storage import get_memory\n"
            "    if flag:\n"
            "        from smartmemory.utils.llm import call_llm as get_memory\n"
            "    get_memory()"
        ),
        _OFFENDER,
    ),
    (
        "global-rebinds-applier-to-sink",
        _cli(
            "_swap()\n    get_memory()",
            _MODULE_GET_MEMORY + "from smartmemory.utils.llm import call_llm\n"
            "def _swap():\n    global get_memory\n    get_memory = call_llm\n",
        ),
        _OFFENDER,
    ),
    (
        "control-comprehension-target-does-not-shadow",
        _cli(
            "[get_memory for get_memory in ()]\n    get_memory()\n    " + _SINK,
            _MODULE_GET_MEMORY,
        ),
        [],
    ),
    (
        "click-command-decorator-imported-renamed",
        {
            "cli": "from click import command as cmd\n"
            "from smartmemory.utils.llm import call_llm\n"
            '@cmd("review")\ndef review_command():\n    call_llm()\n'
        },
        _OFFENDER,
    ),
]


@pytest.mark.parametrize(
    "sources, expected",
    [row[1:] for row in ALIAS_ROWS],
    ids=[row[0] for row in ALIAS_ROWS],
)
def test_applier_credit_follows_the_binding_not_the_spelling(sources, expected):
    assert _unconfigured_llm_commands({**_TRUSTED, **sources}) == expected


# r3 HIGH (CLI-GUARD-RESOLVE-1): where a name does not provably resolve to a trusted
# applier, the guard refuses instead of resolving. Rows 1 to 8 are the reviewer's
# r3 repro; the rest pin the same rule for global, nonlocal and nested scopes and
# the controls that must still pass.
_TRUSTED = {
    "storage": (
        "def apply_runtime_config():\n    pass\n"
        "def get_memory():\n    apply_runtime_config()\n"
    ),
    "worker_entry": (
        "from smartmemory_app.storage import apply_runtime_config\n"
        "def run_configured_worker():\n    apply_runtime_config()\n"
    ),
}
_LLM_IMPORT = "from smartmemory.utils.llm import call_llm\n"
_STAR_HELPER = (
    '__all__ = ["get_memory", "apply_runtime_config", "run_configured_worker"]\n'
    "from smartmemory.utils.llm import call_llm as get_memory, "
    "call_llm as apply_runtime_config, call_llm as run_configured_worker\n"
)


def _resolve(body: str, header: str = "", **modules: str) -> dict[str, str]:
    return {**_TRUSTED, **_cli(body, header), **modules}


# (id, module sources, offending commands expected)
RESOLVE_ROWS = [
    (
        "wildcard-shadows-get_memory",
        _resolve(
            "get_memory()\n    " + _SINK,
            _MODULE_GET_MEMORY + "from smartmemory_app.helper import *\n",
            helper=_STAR_HELPER,
        ),
        _OFFENDER,
    ),
    (
        "wildcard-shadows-apply_runtime_config",
        _resolve(
            "apply_runtime_config()\n    " + _SINK,
            "from smartmemory_app.storage import apply_runtime_config\n"
            "from smartmemory_app.helper import *\n",
            helper=_STAR_HELPER,
        ),
        _OFFENDER,
    ),
    (
        "wildcard-shadows-run_configured_worker",
        _resolve(
            "run_configured_worker()\n    " + _SINK,
            "from smartmemory_app.worker_entry import run_configured_worker\n"
            "from smartmemory_app.helper import *\n",
            helper=_STAR_HELPER,
        ),
        _OFFENDER,
    ),
    (
        "other-package-get_memory",
        _resolve(
            "from smartmemory_app.untrusted.storage import get_memory\n"
            "    get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "other-package-apply_runtime_config",
        _resolve(
            "from smartmemory_app.untrusted.storage import apply_runtime_config\n"
            "    apply_runtime_config()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "other-package-run_configured_worker",
        _resolve(
            "from smartmemory_app.untrusted.worker_entry import run_configured_worker\n"
            "    run_configured_worker()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "repeated-module-helper",  # the later def wins at runtime
        _resolve(
            "helper()\n    " + _SINK,
            _MODULE_GET_MEMORY
            + _LLM_IMPORT
            + "def helper():\n    get_memory()\n"
            + "def helper():\n    call_llm()\n",
        ),
        _OFFENDER,
    ),
    (
        "repeated-nested-helper",
        _resolve(
            "def helper():\n        get_memory()\n"
            "    def helper():\n        call_llm()\n"
            "    helper()\n    " + _SINK,
            _MODULE_GET_MEMORY + _LLM_IMPORT,
        ),
        _OFFENDER,
    ),
    (
        "global-def-rebinds-applier",
        _resolve(
            "_swap()\n    get_memory()",
            _MODULE_GET_MEMORY + _LLM_IMPORT + "def _swap():\n    global get_memory\n"
            "    def get_memory():\n        call_llm()\n",
        ),
        _OFFENDER,
    ),
    (
        "nonlocal-import-rebinds-applier",
        _resolve(
            "from smartmemory_app.storage import get_memory\n"
            "    def swap():\n        nonlocal get_memory\n"
            "        from smartmemory.utils.llm import call_llm as get_memory\n"
            "    swap()\n    get_memory()"
        ),
        _OFFENDER,
    ),
    (
        "nested-scope-reaches-repeated-helper",
        _resolve(
            "def helper():\n        get_memory()\n"
            "    def helper():\n        call_llm()\n"
            "    def inner():\n        helper()\n"
            "    inner()\n    " + _SINK,
            _MODULE_GET_MEMORY + _LLM_IMPORT,
        ),
        _OFFENDER,
    ),
    (
        "relative-import-of-another-package",
        _resolve(
            "from .untrusted.storage import get_memory\n    get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "wildcard-in-a-reached-wrapper",
        _resolve(
            "from smartmemory_app.helper import invoke\n    invoke()",
            helper=_MODULE_GET_MEMORY
            + "from smartmemory_app.other import *\n"
            + _LLM_IMPORT
            + "def invoke():\n    get_memory()\n    call_llm()\n",
        ),
        _OFFENDER,
    ),
    (
        "wildcard-in-a-command-module-without-a-sink",
        _resolve("print('hi')", "from smartmemory_app.helper import *\n"),
        _OFFENDER,
    ),
    (
        "control-from-import-of-trusted-applier",  # r3 control
        _resolve(
            "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
            + _SINK
        ),
        [],
    ),
    (
        "control-relative-import-of-trusted-applier",
        _resolve("from .storage import get_memory\n    get_memory()\n    " + _SINK),
        [],
    ),
    (
        "control-every-repeated-def-applies",
        _resolve(
            "helper()\n    " + _SINK,
            _MODULE_GET_MEMORY
            + "def helper():\n    get_memory()\n"
            + "def helper():\n    get_memory()\n",
        ),
        [],
    ),
    (
        "control-wildcard-in-a-module-nothing-reaches",
        _resolve(
            "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
            + _SINK,
            lonely="from smartmemory_app.other import *\n",
        ),
        [],
    ),
]


@pytest.mark.parametrize(
    "sources, expected",
    [row[1:] for row in RESOLVE_ROWS],
    ids=[row[0] for row in RESOLVE_ROWS],
)
def test_guard_refuses_where_resolution_is_ambiguous(sources, expected):
    assert _unconfigured_llm_commands(sources) == expected


# CLI-POSTLAND-FU-1 H2: a trusted applier earns credit only when the called function
# exists in the scanned sources under its full dotted path. Rows 1 to 3 are the
# reviewer's repro (missing export behind a swallowed ImportError).
def _guarded_call(module: str, name: str) -> str:
    return (
        f"try:\n        from smartmemory_app.{module} import {name}\n"
        f"        {name}()\n    except ImportError:\n        pass\n    " + _SINK
    )


_UNRELATED = "present_but_not_the_trusted_export = True\n"

# (id, module sources, offending commands expected)
MISSING_EXPORT_ROWS = [
    (
        "missing-get_memory-behind-importerror",
        {**_cli(_guarded_call("storage", "get_memory")), "storage": _UNRELATED},
        _OFFENDER,
    ),
    (
        "missing-apply_runtime_config-behind-importerror",
        {
            **_cli(_guarded_call("storage", "apply_runtime_config")),
            "storage": _UNRELATED,
        },
        _OFFENDER,
    ),
    (
        "missing-run_configured_worker-behind-importerror",
        {
            **_cli(_guarded_call("worker_entry", "run_configured_worker")),
            "worker_entry": _UNRELATED,
        },
        _OFFENDER,
    ),
    (
        "missing-module-entirely",
        _cli(
            "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
            + _SINK
        ),
        _OFFENDER,
    ),
    (
        "missing-export-no-try",
        {
            **_cli(
                "from smartmemory_app.storage import get_memory\n"
                "    get_memory()\n    " + _SINK
            ),
            "storage": _UNRELATED,
        },
        _OFFENDER,
    ),
    (
        "missing-export-through-module-attribute",
        {
            **_cli(
                "storage.get_memory()\n    " + _SINK,
                "from smartmemory_app import storage\n",
            ),
            "storage": _UNRELATED,
        },
        _OFFENDER,
    ),
    (
        "control-real-get_memory-earns-credit",
        _resolve(
            "from smartmemory_app.storage import get_memory\n    get_memory()\n    "
            + _SINK
        ),
        [],
    ),
    (
        "control-real-run_configured_worker-earns-credit",
        _resolve(
            "from smartmemory_app.worker_entry import run_configured_worker\n"
            "    run_configured_worker()\n    " + _SINK
        ),
        [],
    ),
]


@pytest.mark.parametrize(
    "sources, expected",
    [row[1:] for row in MISSING_EXPORT_ROWS],
    ids=[row[0] for row in MISSING_EXPORT_ROWS],
)
def test_guard_credits_only_appliers_that_exist(sources, expected):
    assert _unconfigured_llm_commands(sources) == expected


# CLI-POSTLAND-FU-1 r2 H2: a name bound inside a ``try:`` whose handlers can catch an
# ImportError earns no applier credit anywhere (the try body, its handlers, ``else``,
# ``finally`` or after the ``try``): Python may have swallowed the failed import and
# run on. The first row is the reviewer's repro.
_R1_CLI = """import click
@click.command()
def review_command():
    try:
        from smartmemory_app.storage import get_memory
        get_memory()
    except ImportError:
        pass
    from smartmemory.utils.llm import call_llm
    call_llm()
"""
_IMPORT_GM = "from smartmemory_app.storage import get_memory"


def _caught(handler: str) -> dict[str, str]:
    """The applier is imported and called in a ``try`` whose handler is ``handler``."""
    return _resolve(
        f"try:\n        {_IMPORT_GM}\n        get_memory()\n    {handler}\n"
        f"        pass\n    " + _SINK
    )


def _try_import(tail: str) -> dict[str, str]:
    """Import in a ``try`` that catches ImportError, then ``tail`` (more statements,
    each indented four spaces after the first) and the sink."""
    return _resolve(
        f"try:\n        {_IMPORT_GM}\n    except ImportError:\n        pass\n"
        f"    {tail}\n    " + _SINK
    )


# (id, module sources, offending commands expected)
CAUGHT_IMPORT_ROWS = [
    (
        "r1-repro-storage-imports-a-missing-module",
        {
            "storage": (
                "import review_backend_that_does_not_exist\ndef get_memory(): pass\n"
            ),
            "cli": _R1_CLI,
        },
        _OFFENDER,
    ),
    (
        "r1-companion-storage-defines-get_memory",
        {"storage": "def get_memory(): pass\n", "cli": _R1_CLI},
        _OFFENDER,
    ),
    ("handler-ImportError", _caught("except ImportError:"), _OFFENDER),
    ("handler-ModuleNotFoundError", _caught("except ModuleNotFoundError:"), _OFFENDER),
    (
        "handler-tuple-with-ImportError",
        _caught("except (ImportError, OSError):"),
        _OFFENDER,
    ),
    ("handler-Exception", _caught("except Exception:"), _OFFENDER),
    ("handler-BaseException", _caught("except BaseException:"), _OFFENDER),
    ("handler-bare-except", _caught("except:"), _OFFENDER),
    (
        "handler-raises-only-when-a-condition-holds",
        _caught("except ImportError:\n        if debug:\n            raise"),
        _OFFENDER,
    ),
    # A name the guard cannot resolve to a builtin might be ImportError: fail closed.
    ("handler-unknown-name", _caught("except SomethingElse:"), _OFFENDER),
    ("handler-tuple-starred", _caught("except (KeyError, *others):"), _OFFENDER),
    ("call-after-the-try", _try_import("get_memory()"), _OFFENDER),
    (
        "call-in-except-handler",
        _resolve(
            f"try:\n        {_IMPORT_GM}\n    except ImportError:\n"
            "        get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "call-in-else",
        _resolve(
            f"try:\n        {_IMPORT_GM}\n    except ImportError:\n        pass\n"
            "    else:\n        get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "call-in-finally",
        _resolve(
            f"try:\n        {_IMPORT_GM}\n    except ImportError:\n        pass\n"
            "    finally:\n        get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    ("alias-of-a-caught-import", _try_import("g = get_memory\n    g()"), _OFFENDER),
    (
        "caught-module-import-then-attribute-call",
        _resolve(
            "try:\n        from smartmemory_app import storage\n"
            "    except ImportError:\n        pass\n    storage.get_memory()\n    "
            + _SINK
        ),
        _OFFENDER,
    ),
    (
        "caught-re-export-through-a-wrapper-module",
        _resolve(
            "from smartmemory_app.helpers import get_memory\n    get_memory()\n    "
            + _SINK,
            helpers=f"try:\n    {_IMPORT_GM}\nexcept ImportError:\n    pass\n",
        ),
        _OFFENDER,
    ),
    (
        "bound-twice-once-uncaught-once-caught",
        _resolve(
            f"{_IMPORT_GM}\n    try:\n        {_IMPORT_GM}\n    except ImportError:\n"
            "        pass\n    get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "inner-try-does-not-hide-the-outer-catch",
        _resolve(
            f"try:\n        try:\n            {_IMPORT_GM}\n        except KeyError:\n"
            "            pass\n    except ImportError:\n        pass\n"
            "    get_memory()\n    " + _SINK
        ),
        _OFFENDER,
    ),
    (
        "control-no-try-earns-credit",
        _resolve(f"{_IMPORT_GM}\n    get_memory()\n    " + _SINK),
        [],
    ),
    ("control-except-KeyError-earns-credit", _caught("except KeyError:"), []),
    (
        # cli_code.code_index_cmd: the handler raises ClickException, the command stops.
        "control-handler-always-raises-earns-credit",
        _caught("except Exception as exc:\n        raise RuntimeError(exc)"),
        [],
    ),
    (
        "control-except-tuple-without-ImportError-earns-credit",
        _caught("except (KeyError, OSError):"),
        [],
    ),
    (
        "control-try-finally-has-no-handler-earns-credit",
        _resolve(
            f"try:\n        {_IMPORT_GM}\n    finally:\n        pass\n"
            "    get_memory()\n    " + _SINK
        ),
        [],
    ),
    (
        "control-import-in-the-handler-is-not-protected-by-it",
        _resolve(
            "try:\n        pass\n    except ImportError:\n"
            f"        {_IMPORT_GM}\n    get_memory()\n    " + _SINK
        ),
        [],
    ),
]


@pytest.mark.parametrize(
    "sources, expected",
    [row[1:] for row in CAUGHT_IMPORT_ROWS],
    ids=[row[0] for row in CAUGHT_IMPORT_ROWS],
)
def test_caught_import_bindings_earn_no_applier_credit(sources, expected):
    assert _unconfigured_llm_commands(sources) == expected


# CLI-GUARD-HARDEN-1: credit only an applier whose import (or call) cannot fail
# silently on any path: a handler that jumps or never raises, ``finally`` that jumps,
# a ``with`` that may suppress, an enclosing catcher of what a handler raised instead,
# a handler name that may not be the builtin, a caller that swallows a helper's
# failure. Rows 1 to 15 are review r2's probes (``units/CLI-POSTLAND-FU-1/review-r2.md``),
# built the same way. Each row is also run: storage's dependency is missing, so
# importing storage inside the command fails, and a storage imported at module load
# fails when ``get_memory()`` runs. ``reaches_sink`` is what really happens; a row
# that reaches the sink must be an offender.
_SW_SINK = "from smartmemory.utils.llm import call_llm\ncall_llm()"
_SW_IMPORT = "from smartmemory_app.storage import get_memory\nget_memory()"


def _ind(text: str, n: int = 4) -> str:
    return "\n".join(" " * n + line for line in text.splitlines())


def _sw(body: str, header: str = "") -> dict[str, str]:
    return {
        "storage": (
            "import review_backend_that_does_not_exist\ndef get_memory(): pass\n"
        ),
        "cli": "import click\nimport contextlib\n"
        + header
        + "\n@click.command()\ndef review_command():\n"
        + _ind(body)
        + "\n",
    }


def _sw_catch(handler: str, exc: str = "ImportError") -> str:
    return f"try:\n{_ind(_SW_IMPORT)}\nexcept {exc}:\n{_ind(handler)}"


def _sw_helper(body: str) -> str:
    return f"def helper():\n{_ind(body)}\nhelper()\n{_SW_SINK}"


_SW_HELPER_SETUP = "def setup():\n" + _ind(_SW_IMPORT) + "\n"
_SW_MODULE_GM = "from smartmemory_app.storage import get_memory\n"

# (id, module sources, offending commands expected, command really reaches the sink)
SWALLOW_ROWS = [
    (
        "if-only-raise",
        _sw(_sw_catch("if False:\n    raise\npass") + "\n" + _SW_SINK),
        _OFFENDER,
        True,
    ),
    (
        "return-before-raise-helper",
        _sw(_sw_helper(_sw_catch("return\nraise"))),
        _OFFENDER,
        True,
    ),
    (
        "conditional-return-before-raise-helper",
        _sw(_sw_helper(_sw_catch("if True:\n    return\nraise"))),
        _OFFENDER,
        True,
    ),
    (
        "break-before-raise",
        _sw("for _ in (0,):\n" + _ind(_sw_catch("break\nraise")) + "\n" + _SW_SINK),
        _OFFENDER,
        True,
    ),
    (
        "continue-before-raise",
        _sw("for _ in (0,):\n" + _ind(_sw_catch("continue\nraise")) + "\n" + _SW_SINK),
        _OFFENDER,
        True,
    ),
    (
        "control-pass-then-raise",
        _sw(_sw_catch("pass\nraise") + "\n" + _SW_SINK),
        [],
        False,
    ),
    (
        "control-log-then-raise",
        _sw(_sw_catch("log()\nraise") + "\n" + _SW_SINK),
        [],
        False,
    ),
    (
        "suppress-ImportError",
        _sw(
            "with contextlib.suppress(ImportError):\n"
            + _ind(_SW_IMPORT)
            + "\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "nested-reraise-same",
        _sw(
            "try:\n"
            + _ind(_sw_catch("raise"))
            + "\nexcept ImportError:\n    pass\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "nested-reraise-different",
        _sw(
            "try:\n"
            + _ind(_sw_catch("raise RuntimeError('changed')"))
            + "\nexcept RuntimeError:\n    pass\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "control-raise-different-stops",
        _sw(_sw_catch("raise RuntimeError('changed')") + "\n" + _SW_SINK),
        [],
        False,
    ),
    (
        "TryStar-swallow",
        _sw(
            "try:\n"
            + _ind(_SW_IMPORT)
            + "\nexcept* ImportError:\n    pass\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "control-TryStar-raise-stops",
        _sw(
            "try:\n"
            + _ind(_SW_IMPORT)
            + "\nexcept* ImportError:\n    raise\n"
            + _SW_SINK
        ),
        [],
        False,
    ),
    (
        "finally-return-overrides-raise",
        _sw(_sw_helper(_sw_catch("raise") + "\nfinally:\n    return")),
        _OFFENDER,
        True,
    ),
    (
        "shadowed-builtin-handler",
        _sw(
            _sw_catch("pass", "ValueError") + "\n" + _SW_SINK,
            "ValueError = ImportError",
        ),
        _OFFENDER,
        True,
    ),
    # The author's three required probes.
    (
        "suppress-imported-under-an-alias",
        _sw(
            "with s(ImportError):\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK,
            "from contextlib import suppress as s",
        ),
        _OFFENDER,
        True,
    ),
    (
        # Conservative: the local ImportError is KeyError, so nothing is swallowed,
        # but a rebound name is never trusted to be the builtin.
        "ImportError-rebound-in-the-function",
        _sw("ImportError = KeyError\n" + _sw_catch("pass") + "\n" + _SW_SINK),
        _OFFENDER,
        False,
    ),
    (
        "unknown-manager-open",  # conservative: open() does not swallow, unproven
        _sw(
            "with open(os.devnull):\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK, "import os"
        ),
        _OFFENDER,
        False,
    ),
    # The rest of the class.
    (
        "KeyError-bound-to-ImportError-in-the-function",
        _sw(
            "KeyError = ImportError\n" + _sw_catch("pass", "KeyError") + "\n" + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "handler-name-is-an-attribute",
        _sw(
            _sw_catch("pass", "errors.KeyError") + "\n" + _SW_SINK,
            "class errors:\n    KeyError = ImportError",
        ),
        _OFFENDER,
        True,
    ),
    (
        "builtins-namespace-rebound",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            '__builtins__["KeyError"] = ImportError',
        ),
        _OFFENDER,
        True,
    ),
    (
        "local-contextmanager-swallows",
        _sw(
            "with quiet():\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK,
            "@contextlib.contextmanager\ndef quiet():\n"
            "    try:\n        yield\n    except ImportError:\n        pass",
        ),
        _OFFENDER,
        True,
    ),
    (
        "ExitStack-is-not-allow-listed",  # conservative: an empty stack does not swallow
        _sw("with contextlib.ExitStack():\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK),
        _OFFENDER,
        False,
    ),
    (
        "control-redirect_stdout-is-allow-listed",
        _sw(
            "with contextlib.redirect_stdout(sys.stderr):\n"
            + _ind(_SW_IMPORT)
            + "\n"
            + _SW_SINK,
            "import sys",
        ),
        [],
        False,
    ),
    (
        "finally-break-in-a-loop",
        _sw(
            "for _ in (0,):\n    try:\n"
            + _ind(_SW_IMPORT, 8)
            + "\n    finally:\n        break\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "TryStar-reraise-caught-as-a-group",
        _sw(
            "try:\n    try:\n"
            + _ind(_SW_IMPORT, 8)
            + "\n    except* ImportError:\n        raise\nexcept ExceptionGroup:\n    pass\n"
            + _SW_SINK
        ),
        _OFFENDER,
        True,
    ),
    (
        "handler-reaches-a-sink-before-it-raises",
        _sw(
            _sw_catch("call_llm()\nraise"), "from smartmemory.utils.llm import call_llm"
        ),
        _OFFENDER,
        True,
    ),
    (
        "caller-swallows-a-helpers-failed-import",
        _sw(
            "try:\n    setup()\nexcept ImportError:\n    pass\n" + _SW_SINK,
            _SW_HELPER_SETUP,
        ),
        _OFFENDER,
        True,
    ),
    (
        "caller-catches-what-a-helper-raised-instead",
        _sw(
            "try:\n    setup()\nexcept RuntimeError:\n    pass\n" + _SW_SINK,
            "def setup():\n" + _ind(_sw_catch("raise RuntimeError('changed')")) + "\n",
        ),
        _OFFENDER,
        True,
    ),
    (
        "applier-call-suppressed",  # get_memory's own import fails inside it
        _sw(
            "with contextlib.suppress(ImportError):\n    get_memory()\n" + _SW_SINK,
            _SW_MODULE_GM,
        ),
        _OFFENDER,
        True,
    ),
    (
        "control-applier-call-unprotected",
        _sw("get_memory()\n" + _SW_SINK, _SW_MODULE_GM),
        [],
        False,
    ),
    (
        "control-applier-call-under-except-KeyError",
        _sw(
            "try:\n    get_memory()\nexcept KeyError:\n    pass\n" + _SW_SINK,
            _SW_MODULE_GM,
        ),
        [],
        False,
    ),
    (
        # cli_code.code_index_cmd's shape: both handlers raise ClickException.
        "control-shipped-click-exception-handlers",
        _sw(
            "try:\n    from smartmemory_app.storage import get_memory\n"
            "except Exception as exc:\n    raise click.ClickException(str(exc))\n"
            "try:\n    memory = get_memory()\n"
            "except Exception as exc:\n    raise click.ClickException(str(exc))\n"
            + _SW_SINK
        ),
        [],
        False,
    ),
]


def _reaches_sink(cli_source: str) -> bool:
    """Run the command with storage's dependency missing; True if the sink ran."""
    events: list[str] = []
    loading = [True]
    real_import = builtins.__import__

    def missing(*args, **kwargs):
        raise ModuleNotFoundError("synthetic missing storage dependency")

    def fake_import(name, *args, **kwargs):
        if name == "smartmemory_app.storage":
            if loading[0]:
                return types.SimpleNamespace(get_memory=missing)
            missing()
        if name == "smartmemory.utils.llm":
            return types.SimpleNamespace(call_llm=lambda: events.append("sink"))
        return real_import(name, *args, **kwargs)

    namespace = {
        "__builtins__": {**vars(builtins), "__import__": fake_import},
        "log": lambda: events.append("log"),
    }
    exec(compile(cli_source, "<review-cli>", "exec"), namespace)
    loading[0] = False
    try:
        namespace["review_command"].callback()
    except BaseException:  # the command stopped; only whether the sink ran matters
        pass
    return "sink" in events


@pytest.mark.parametrize(
    "sources, expected, reaches_sink",
    [row[1:] for row in SWALLOW_ROWS],
    ids=[row[0] for row in SWALLOW_ROWS],
)
def test_swallowed_import_earns_no_applier_credit(sources, expected, reaches_sink):
    assert _unconfigured_llm_commands(sources) == expected
    assert _reaches_sink(sources["cli"]) is reaches_sink
    assert expected or not reaches_sink  # a command that reaches the sink is flagged


def test_non_swallowing_managers_never_suppress():
    """The allow-list holds only managers whose ``__exit__`` returns a false value."""
    import contextlib
    import sys

    for manager in (
        contextlib.redirect_stdout(sys.stdout),
        contextlib.redirect_stderr(sys.stderr),
        contextlib.nullcontext(),
    ):
        manager.__enter__()
        assert not manager.__exit__(ImportError, ImportError("x"), None)
    assert NON_SWALLOWING_MANAGERS == {
        "contextlib.redirect_stdout",
        "contextlib.redirect_stderr",
        "contextlib.nullcontext",
    }


# CLI-GUARD-HARDEN-2: review r1 of HARDEN-1 (``units/CLI-GUARD-HARDEN-1/review-r1.md``)
# found a decorator, an attribute store on ``contextlib`` and a ``globals()`` write
# that each swallow the failed import with credit kept, and sinks evaluated inside a
# stopping handler. Rows named after a review probe are that probe. Every row is also
# run, in a fresh interpreter with the real builtins, because several rows write the
# shared ``contextlib`` and ``builtins`` namespaces.
_SW_SETUP_CALL = "def setup():\n" + _ind(_SW_IMPORT) + "\nsetup()\n" + _SW_SINK
_SW_QUIET_CM = (
    "@contextlib.contextmanager\ndef quiet():\n"
    "    try:\n        yield\n    except ImportError:\n        pass\n"
)
_SW_QUIET_FN = (
    "def quiet(f):\n    def run():\n        try:\n            return f()\n"
    "        except ImportError:\n            pass\n    return run\n"
)
_SW_NULL_WITH = (
    "with contextlib.nullcontext(ImportError):\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK
)
_SW_LLM = "from smartmemory.utils.llm import call_llm"

# (id, module sources, offending commands expected, command really reaches the sink)
BYPASS_ROWS = [
    # R1-H1: a decorator replaces the helper, so its body's credit is not the caller's.
    (
        "decorator-suppresses-helper",  # review probe: a call, a local contextmanager
        _sw("@quiet()\n" + _SW_SETUP_CALL, _SW_QUIET_CM),
        _OFFENDER,
        True,
    ),
    (
        "decorator-bare-name",
        _sw("@quiet\n" + _SW_SETUP_CALL, _SW_QUIET_FN),
        _OFFENDER,
        True,
    ),
    (
        "decorator-attribute",
        _sw(
            "@deco.quiet\n" + _SW_SETUP_CALL,
            "class deco:\n    @staticmethod\n" + _ind(_SW_QUIET_FN),
        ),
        _OFFENDER,
        True,
    ),
    (
        "decorators-stacked-one-unsafe",
        _sw(
            "@functools.wraps(print)\n@quiet\n" + _SW_SETUP_CALL,
            "import functools\n" + _SW_QUIET_FN,
        ),
        _OFFENDER,
        True,
    ),
    (
        "decorator-is-a-class",
        _sw(
            "@quiet\n" + _SW_SETUP_CALL,
            "class quiet:\n    def __init__(self, f):\n        self.f = f\n"
            "    def __call__(self):\n        try:\n            return self.f()\n"
            "        except ImportError:\n            pass\n",
        ),
        _OFFENDER,
        True,
    ),
    (
        "allow-listed-decorator-rebound-locally",
        _sw(
            "@wraps(print)\n" + _SW_SETUP_CALL,
            "from functools import wraps\n"
            + _SW_QUIET_FN
            + "wraps = lambda wrapped: quiet\n",
        ),
        _OFFENDER,
        True,
    ),
    (
        # Conservative: a bare ``wraps`` makes ``setup()`` a TypeError, never a swallow.
        "allow-listed-decorator-not-called",
        _sw("@functools.wraps\n" + _SW_SETUP_CALL, "import functools"),
        _OFFENDER,
        False,
    ),
    (
        "control-allow-listed-decorator-call",
        _sw("@functools.wraps(print)\n" + _SW_SETUP_CALL, "import functools"),
        [],
        False,
    ),
    # R1-H2/H3: a namespace written without a lexical binding, one row per form.
    (
        "allowlisted-manager-attribute-rebound",  # review probe
        _sw("contextlib.nullcontext = contextlib.suppress\n" + _SW_NULL_WITH),
        _OFFENDER,
        True,
    ),
    (
        "globals-dict-rebinds-KeyError",  # review probe: subscript store
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            'globals()["KeyError"] = ImportError',
        ),
        _OFFENDER,
        True,
    ),
    (
        "globals-update-rebinds-KeyError",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            "globals().update(KeyError=ImportError)",
        ),
        _OFFENDER,
        True,
    ),
    (
        "globals-aliased-then-written",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            'namespace = globals()\nnamespace["KeyError"] = ImportError',
        ),
        _OFFENDER,
        True,
    ),
    (
        "module-locals-rebinds-KeyError",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            'locals()["KeyError"] = ImportError',
        ),
        _OFFENDER,
        True,
    ),
    (
        "exec-rebinds-KeyError",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            'exec("KeyError = ImportError")',
        ),
        _OFFENDER,
        True,
    ),
    (
        "builtins-attribute-store",
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            "import builtins\nbuiltins.KeyError = ImportError",
        ),
        _OFFENDER,
        True,
    ),
    (
        "annotation-writes-a-namespace",  # runs at def time, owned by no scope
        _sw(
            _sw_catch("pass", "KeyError") + "\n" + _SW_SINK,
            "def helper(arg: globals().update(KeyError=ImportError)):\n    pass",
        ),
        _OFFENDER,
        True,
    ),
    (
        "vars-of-a-module-updated",
        _sw(
            "vars(contextlib).update(nullcontext=contextlib.suppress)\n" + _SW_NULL_WITH
        ),
        _OFFENDER,
        True,
    ),
    (
        "module-dict-subscript-store",
        _sw(
            'contextlib.__dict__["nullcontext"] = contextlib.suppress\n' + _SW_NULL_WITH
        ),
        _OFFENDER,
        True,
    ),
    (
        "setattr-on-an-allow-listed-module",
        _sw(
            'setattr(contextlib, "nullcontext", contextlib.suppress)\n' + _SW_NULL_WITH
        ),
        _OFFENDER,
        True,
    ),
    (
        "setattr-through-sys-modules",
        _sw(
            'setattr(sys.modules["contextlib"], "nullcontext", contextlib.suppress)\n'
            + _SW_NULL_WITH,
            "import sys",
        ),
        _OFFENDER,
        True,
    ),
    (
        "allow-listed-manager-method-patched",
        _sw(
            "with contextlib.nullcontext():\n" + _ind(_SW_IMPORT) + "\n" + _SW_SINK,
            "contextlib.nullcontext.__exit__ = lambda *exc: True",
        ),
        _OFFENDER,
        True,
    ),
    (
        "scanned-module-attribute-store",  # the applier itself replaced
        _sw(
            "storage.get_memory()\n" + _SW_SINK,
            "from smartmemory_app import storage\nstorage.get_memory = lambda: None",
        ),
        _OFFENDER,
        True,
    ),
    (
        # Conservative here (the oracle runs only cli), but in the CLI the other
        # module may load first: a write anywhere voids the proofs everywhere.
        "write-in-another-module-voids-the-proof",
        {
            **_sw(_SW_NULL_WITH),
            "patcher": "import contextlib\n"
            "contextlib.nullcontext = contextlib.suppress\n",
        },
        _OFFENDER,
        False,
    ),
    (
        # Shipped shape (storage, doctor, hf_progress patch modules no proof reads).
        "control-store-on-a-module-no-proof-reads",
        _sw(
            "get_memory()\n" + _SW_SINK,
            "import socket\nsocket.review_marker = 1\n" + _SW_MODULE_GM,
        ),
        [],
        False,
    ),
    (
        "control-setattr-on-a-local-object",  # shipped shape: setattr(cfg, key, value)
        _sw(
            'state = types.SimpleNamespace()\nsetattr(state, "x", 1)\nget_memory()\n'
            + _SW_SINK,
            "import types\n" + _SW_MODULE_GM,
        ),
        [],
        False,
    ),
    # R1-M1: a stopping handler that reaches a sink while it raises.
    (
        "sink-in-raise-expression",  # review probe
        _sw(_sw_catch("raise RuntimeError(call_llm())"), _SW_LLM),
        _OFFENDER,
        True,
    ),
    (
        "sink-in-raise-cause",
        _sw(_sw_catch("raise RuntimeError('stop') from call_llm()"), _SW_LLM),
        _OFFENDER,
        True,
    ),
    (
        "sink-in-handler-lambda",  # review probe
        _sw(_sw_catch("(lambda: call_llm())()\nraise"), _SW_LLM),
        _OFFENDER,
        True,
    ),
    (
        "sink-in-handler-comprehension",  # review probe
        _sw(_sw_catch("[call_llm() for _ in (0,)]\nraise"), _SW_LLM),
        _OFFENDER,
        True,
    ),
]

# ``_reaches_sink`` in a fresh interpreter: real builtins, so a write to ``builtins``
# or ``contextlib`` acts as it would in the CLI, and stays in that process. Module
# ``smartmemory_app`` resolves to a stand-in whose ``storage.get_memory`` fails.
_ISOLATED_DRIVER = """
import builtins, json, sys, types
events, loading = [], [True]
real_import = builtins.__import__
def missing(*args, **kwargs):
    raise ModuleNotFoundError("synthetic missing storage dependency")
def fake_import(name, *args, **kwargs):
    if name in ("smartmemory_app", "smartmemory_app.storage"):
        if not loading[0]:
            missing()
        storage = types.SimpleNamespace(get_memory=missing)
        if name == "smartmemory_app":
            return types.SimpleNamespace(storage=storage)
        return storage
    if name == "smartmemory.utils.llm":
        return types.SimpleNamespace(call_llm=lambda: events.append("sink"))
    return real_import(name, *args, **kwargs)
builtins.__import__ = fake_import
namespace = {"__name__": "review_cli", "log": lambda: events.append("log")}
exec(compile(sys.stdin.read(), "<review-cli>", "exec"), namespace)
loading[0] = False
try:
    namespace["review_command"].callback()
except BaseException:  # the command stopped; only whether the sink ran matters
    pass
sys.stdout.write(json.dumps("sink" in events))
"""


def _reaches_sink_isolated(cli_source: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _ISOLATED_DRIVER],
        input=cli_source,
        capture_output=True,
        text=True,
        env={},  # no provider key reaches the child
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "sources, expected, reaches_sink",
    [row[1:] for row in BYPASS_ROWS],
    ids=[row[0] for row in BYPASS_ROWS],
)
def test_decorated_and_dynamic_bypasses_earn_no_credit(sources, expected, reaches_sink):
    """The guarantee is over lexical bindings plus a flat refusal. A decorated helper
    lends credit only through ``SAFE_DECORATORS``; a handler that reaches a sink in
    what it evaluates before it leaves does not stop. Namespace writes the lexical
    proof cannot see (``globals()``/``vars()``/``locals()``, ``exec``, ``builtins``,
    ``__dict__``, ``setattr``, an attribute store on a scanned module or on a module
    the proofs read) are not modelled: they void every builtin and allow-list proof
    and flag the commands in, or reaching, the writing module."""
    assert _unconfigured_llm_commands(sources) == expected
    assert _reaches_sink_isolated(sources["cli"]) is reaches_sink
    assert expected or not reaches_sink  # a command that reaches the sink is flagged


def test_safe_decorators_return_the_function_itself():
    """The decorator allow-list holds only decorators that hand back the function."""
    import functools

    def body():
        pass

    assert functools.wraps(print)(body) is body
    assert SAFE_DECORATORS == {"functools.wraps"}
    assert _PROOF_ROOTS == {"builtins", "__builtins__", "contextlib", "functools"}
