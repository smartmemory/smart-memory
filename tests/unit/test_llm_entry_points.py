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
# Wrapper calls that run apply_runtime_config() before any of the sinks above.
CONFIG_APPLIERS = {"apply_runtime_config", "get_memory", "run_configured_worker"}

# "module:command" -> reason. A command that reaches a sink without an applier.
COMMAND_EXEMPT: dict[str, str] = {}


def _app_sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in APP.glob("*.py")}


def _is_click_command(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in {"command", "group"}:
            return True
    return False


def _dotted(node: ast.AST) -> list[str] | None:
    """``['a', 'b', 'c']`` for a plain ``a.b.c`` Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _scope_nodes(body: list) -> list[ast.AST]:
    """Nodes of one scope: ``body`` without descending into nested defs or classes."""
    out: list[ast.AST] = []
    stack = list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _local_names(fn: ast.AST) -> set[str]:
    """Names a function binds itself (arguments, assignments, nested defs)."""
    names = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    for node in _scope_nodes(fn.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _app_bindings(nodes, modules: set[str]) -> dict[str, tuple]:
    """Names ``nodes`` bind to wrapper code: ``("func", module, name)``,
    ``("module", module)`` or ``("package",)`` (``import smartmemory_app``).

    An import is only a binding. Nothing here says the bound name is ever called.
    """
    env: dict[str, tuple] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level == 0 and top != "smartmemory_app":
                continue
            origin = (node.module or "").rsplit(".", 1)[-1]
            package_level = not node.module or node.module == "smartmemory_app"
            for alias in node.names:
                bound = alias.asname or alias.name
                if not package_level:
                    env[bound] = ("func", origin, alias.name)
                elif alias.name in modules:
                    env[bound] = ("module", alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] != "smartmemory_app":
                    continue
                if alias.asname:
                    if len(parts) == 2 and parts[1] in modules:
                        env[alias.asname] = ("module", parts[1])
                else:
                    env["smartmemory_app"] = ("package",)
    return env


def _unconfigured_llm_commands(sources: dict[str, str]) -> list[str]:
    """``module:command`` for every Click command whose reachable code starts an LLM
    sink and never calls a config applier.

    Reachable means the command body plus the wrapper functions it calls or names
    statically: a bare name in its own module, a name imported from a
    ``smartmemory_app`` module, or a module-qualified call through ``from
    smartmemory_app import helper``, ``import smartmemory_app.helper as h`` or
    ``import smartmemory_app.helper``. A sink counts when it is named at all, so a
    sink that is merely referenced still flags. An applier counts only when it is
    CALLED, here or in a function reached through a call: an import is a binding,
    not execution, and a helper that is only named does not run. Appliers are not
    expanded (storage.get_memory is a real sink holder, and is checked below).
    Names resolve per scope: module-level imports, shadowed by what the function
    assigns or takes as an argument (a local ``search`` is not ``storage.search``),
    plus the function's own imports. A command's decorators are not read.
    Blind spots: method calls on objects, calls through containers, and call order
    inside one command (an applier after the sink still passes).
    """
    modules = set(sources)
    funcs: dict[tuple[str, str], ast.AST] = {}
    module_env: dict[str, dict[str, tuple]] = {}
    commands = []
    for module, source in sources.items():
        tree = ast.parse(source)
        module_env[module] = _app_bindings(_scope_nodes(tree.body), modules)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault((module, node.name), node)
                if _is_click_command(node):
                    commands.append((module, node.name))

    def target(node, module, env):
        parts = _dotted(node)
        if parts is None:
            return None
        if len(parts) == 1:
            if (module, parts[0]) in funcs:
                return (module, parts[0])
            bound = env.get(parts[0])
            return (bound[1], bound[2]) if bound and bound[0] == "func" else None
        bound = env.get(parts[0])
        if bound and bound[0] == "module" and len(parts) == 2:
            return (bound[1], parts[1])
        if bound and bound[0] == "package" and len(parts) == 3:
            return (parts[1], parts[2])
        return None

    # Direct facts per function, then propagate along the edges to a fixed point.
    sinks: dict[tuple[str, str], set] = {}
    appliers: dict[tuple[str, str], set] = {}
    call_edges: dict[tuple[str, str], set] = {}
    ref_edges: dict[tuple[str, str], set] = {}
    for key, fn in funcs.items():
        module = key[0]
        body = [n for stmt in fn.body for n in ast.walk(stmt)]
        # Module-level imports, minus names this function rebinds, plus its own
        # imports. Decorators (``@group.command``) name the parent group, they do
        # not run it, so only the body is read.
        local = _local_names(fn)
        env = {k: v for k, v in module_env[module].items() if k not in local}
        env.update(_app_bindings(body, modules))
        sinks[key], appliers[key] = set(), set()
        call_edges[key], ref_edges[key] = set(), set()
        for node in body:
            if isinstance(node, ast.ImportFrom):
                sinks[key] |= {a.name for a in node.names} & LLM_SINKS
            elif isinstance(node, ast.Call):
                label = (_dotted(node.func) or [None])[-1]
                if label in CONFIG_APPLIERS:
                    appliers[key].add(label)
                if (hit := target(node.func, module, env)) is not None:
                    call_edges[key].add(hit)
            elif isinstance(node, (ast.Name, ast.Attribute)):
                label = node.id if isinstance(node, ast.Name) else node.attr
                if label in LLM_SINKS:
                    sinks[key].add(label)
                if (hit := target(node, module, env)) is not None:
                    ref_edges[key].add(hit)
    changed = True
    while changed:
        changed = False
        for key in funcs:
            for hit in call_edges[key] | ref_edges[key]:
                if hit not in funcs:
                    continue
                if not sinks[hit] <= sinks[key]:
                    sinks[key] |= sinks[hit]
                    changed = True
                if hit in call_edges[key] and not appliers[hit] <= appliers[key]:
                    appliers[key] |= appliers[hit]
                    changed = True

    return [
        f"{module}:{name}"
        for module, name in sorted(commands)
        if sinks[(module, name)] and not appliers[(module, name)]
    ]


def test_every_llm_command_applies_config_or_is_exempt():
    offenders = set(_unconfigured_llm_commands(_app_sources()))

    assert offenders <= set(COMMAND_EXEMPT), (
        "Click command reaches core's LLM without apply_runtime_config(): "
        f"{sorted(offenders - set(COMMAND_EXEMPT))}"
    )
    assert set(COMMAND_EXEMPT) <= offenders, "stale command exemption"


def test_config_appliers_really_apply_the_config():
    """Each applier calls apply_runtime_config() itself or in a helper one hop away."""
    sources = _app_sources()
    for module, name in (
        ("storage", "get_memory"),
        ("worker_entry", "run_configured_worker"),
    ):
        defs = {
            n.name: n
            for n in ast.walk(ast.parse(sources[module]))
            if isinstance(n, ast.FunctionDef)
        }
        called = {
            n.func.id
            for n in ast.walk(defs[name])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        bodies = [ast.unparse(defs[name])] + [
            ast.unparse(defs[c]) for c in called if c in defs
        ]
        assert any("apply_runtime_config(" in b for b in bodies), name


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
    sources = {"cli": _CMD.format(body=body), **helpers}

    assert _unconfigured_llm_commands(sources) == expected
