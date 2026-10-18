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
    # subscription billing, whatever provider the user configured.
    "capture_worker": "deliberate Groq pin, not the configured provider (policy)",
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
    ``("alias", value_node, scope)`` (``x = a.b``), ``("caught", spec)`` (an import
    in a ``try`` whose handler may swallow an ImportError) or ``("opaque",)`` (an
    argument, a loop target, any other value).
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


def _may_catch_import_error(handler: ast.ExceptHandler) -> bool:
    """Can this handler swallow an ImportError? A bare ``except:`` can. So can a
    builtin exception class that is ImportError, a subclass of it
    (``ModuleNotFoundError``) or a base of it (``Exception``, ``BaseException``), alone
    or in a tuple. A name that is not a builtin exception, or a starred or computed
    tuple member, might be an alias of one: it counts (fail closed)."""
    if handler.type is None:
        return True
    types_ = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for expr in types_:
        name = (
            expr.id
            if isinstance(expr, ast.Name)
            else expr.attr
            if isinstance(expr, ast.Attribute)
            else None
        )
        cls = getattr(builtins, name, None) if name else None
        if not (isinstance(cls, type) and issubclass(cls, BaseException)):
            return True
        if issubclass(cls, ImportError) or issubclass(ImportError, cls):
            return True
    return False


def _caught_imports(nodes: list[ast.AST]) -> set[int]:
    """ids of the import statements of one scope that sit in the BODY of a ``try``
    with a handler that may swallow an ImportError. A handler, ``else`` or ``finally``
    is not protected by its own ``try``; a nested def's body is another scope."""
    caught: set[int] = set()
    for node in nodes:
        if isinstance(node, _TRIES) and any(
            _may_catch_import_error(h)
            and not any(isinstance(s, ast.Raise) for s in h.body)
            for h in node.handlers
        ):
            caught |= {
                id(n)
                for n in _scope_nodes(node.body)
                if isinstance(n, (ast.Import, ast.ImportFrom))
            }
    return caught


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

    caught = _caught_imports(scope.nodes)
    for stmt in scope.nodes:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for bound, spec in _import_specs(stmt, modules):
                # A swallowed ImportError means the name may never have been bound.
                scope.bind(bound, ("caught", spec) if id(stmt) in caught else spec)
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
        for module, source in sources.items():
            tree = ast.parse(source)
            module_scopes[module] = _build_scope(
                "module", module, "", None, tree, tree.body, modules, self.funcs
            )
        self.resolver = _Resolver(modules, module_scopes)
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
            self.wild[key] = up is not None or _star_in(scope)

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
                if runs and all(_is_applier(i, self.funcs) for i in idents):
                    self.appliers[key] |= {i[1:] for i in idents}
                if runs and hits and len(hits) == len(idents):
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
    inside a ``try`` whose handler may swallow an ImportError (``ImportError``,
    ``ModuleNotFoundError``, ``Exception``, ``BaseException``, a bare ``except:``, a
    tuple holding one, or a name that is not a builtin exception): the import may have
    failed and the command run on, wherever the call sits. Not modelled: a target
    module whose own top-level import fails (the failure is not in this command's
    ``try``). A call that resolves to a sink is
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
