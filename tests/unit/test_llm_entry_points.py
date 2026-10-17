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


def _unconfigured_llm_commands(sources: dict[str, str]) -> list[str]:
    """``module:command`` for every Click command whose reachable code starts an LLM
    sink and never calls a config applier.

    Reachable means the command body plus the module-level functions it calls by
    bare name, in its own module or imported from ``smartmemory_app``. Appliers are
    not expanded (storage.get_memory is a real sink holder, and is checked below).
    Blind spots: method calls on objects, calls through containers, and call order
    inside one command (an applier after the sink still passes).
    """
    funcs: dict[tuple[str, str], ast.AST] = {}
    imports: dict[str, dict[str, tuple[str, str]]] = {}
    commands = []
    for module, source in sources.items():
        tree = ast.parse(source)
        imports[module] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "smartmemory_app"
            ):
                origin = node.module.rsplit(".", 1)[-1]
                for alias in node.names:
                    imports[module][alias.asname or alias.name] = (origin, alias.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault((module, node.name), node)
                if _is_click_command(node):
                    commands.append((module, node.name))

    def reach(module, name, seen):
        if (module, name) in seen or (module, name) not in funcs:
            return set(), set()
        seen.add((module, name))
        fn = funcs[(module, name)]
        sinks, appliers, follow = set(), set(), set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                label = node.id
            elif isinstance(node, ast.Attribute):
                label = node.attr
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in LLM_SINKS:
                        sinks.add(alias.name)
                    if alias.name in CONFIG_APPLIERS:
                        appliers.add(alias.name)
                    follow.add(alias.name)
                continue
            else:
                continue
            if label in LLM_SINKS:
                sinks.add(label)
            if isinstance(node, ast.Name):
                follow.add(label)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = node.func
                label = getattr(callee, "id", None) or getattr(callee, "attr", None)
                if label in CONFIG_APPLIERS:
                    appliers.add(label)
        for label in follow - LLM_SINKS - CONFIG_APPLIERS:
            target = (module, label) if (module, label) in funcs else None
            if target is None and label in imports[module]:
                target = imports[module][label]
            if target:
                more_sinks, more_appliers = reach(*target, seen)
                sinks |= more_sinks
                appliers |= more_appliers
        return sinks, appliers

    offenders = []
    for module, name in sorted(commands):
        sinks, appliers = reach(module, name, set())
        if sinks and not appliers:
            offenders.append(f"{module}:{name}")
    return offenders


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
