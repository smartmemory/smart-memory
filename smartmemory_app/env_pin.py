"""CLI-ENV-DETECT-1: per-project decisions about inherited SMARTMEMORY_* variables.

Where decisions live: the existing user config file (``config.config_path()``,
the same TOML ``smartmemory setup`` and ``smartmemory config`` write), under a
``[projects."<project root>"]`` table. There is no other project-level store in
the CLI to reuse, and a file inside the project could be committed along with
an override value. See ``env_sources`` for the layout.

Effect, applied to ``os.environ`` once per process before settings resolve:

- ``ignore``: the variable is removed, so this project behaves as if it were unset.
- ``override``: the stored project value replaces the shell value.
- ``use``: nothing changes, the question is just not asked again.

Resulting precedence for a setting: command-line flag > this project's decision >
environment variable > user config file > built-in default.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

import click
import tomli_w

from smartmemory_app import env_sources
from smartmemory_app.env_sources import (
    ConfigUnreadableError,
    CHECK_SWITCH,
    DECISIONS,
    InheritedVar,
    inherited_vars,
    project_entry,
    project_key,
    read_config_table,
    read_config_table_strict,
)

log = logging.getLogger(__name__)

# Tables save_config() writes itself. Anything else in the file (``projects``)
# belongs to other writers and is carried over when save_config() rewrites it.
_MANAGED_TABLES = frozenset({"smartmemory", "local", "remote"})
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

# Commands that never prompt or warn: hooks run by other tools (lifecycle), the
# doctor (it reports these variables itself), help, and the env command itself.
# Decisions still apply to them.
_QUIET_COMMANDS = frozenset({"lifecycle", "doctor", "help", "server", "env"})

_applied = False


def is_secret(name: str) -> bool:
    return name.endswith(_SECRET_SUFFIXES)


def override_error(name: str, value: str) -> str | None:
    """Why ``value`` cannot be stored as an override for ``name``, or None."""
    if is_secret(name):
        return (
            f"{name} holds a secret, so it cannot be stored as a project value. "
            "Choose use or ignore, and set the secret with `smartmemory setup`."
        )
    if not value:
        return "An override needs a value."
    if name == "SMARTMEMORY_MODE" and value not in ("local", "remote"):
        return "SMARTMEMORY_MODE must be local or remote."
    if name == "SMARTMEMORY_DAEMON_PORT" and not (
        value.isdigit() and 0 < int(value) < 65536
    ):
        return "SMARTMEMORY_DAEMON_PORT must be a port number."
    return None


def _config_file() -> Path:
    from smartmemory_app.config import config_path

    return config_path()


# ── Apply ─────────────────────────────────────────────────────────────────────


def apply_project_env(
    environ=None, project_dir=None, path: Path | None = None
) -> dict[str, str]:
    """Apply this project's ignore/override decisions to ``environ``.

    Returns ``{name: decision}`` for every variable changed. Values are never logged.
    """
    environ = os.environ if environ is None else environ
    if environ is os.environ:
        env_sources.record_pre_pin_names(list(environ))
    raw = read_config_table(path or _config_file())
    if not raw.get("projects"):
        return {}
    entry = project_entry(raw, project_key(project_dir))
    decisions = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    values = entry.get("env_override")
    values = values if isinstance(values, dict) else {}
    changed: dict[str, str] = {}
    for name, decision in decisions.items():
        if not name.startswith(env_sources.PREFIX) or name == CHECK_SWITCH:
            continue
        if decision == "ignore":
            if environ.pop(name, None) is not None:
                changed[name] = "ignore"
        elif decision == "override":
            value = values.get(name)
            if isinstance(value, str) and not override_error(name, value):
                environ[name] = value
                changed[name] = "override"
            else:
                log.warning(
                    "Project override for %s has no valid stored value, "
                    "so the shell value stays in effect",
                    name,
                )
    if changed:
        log.info("Project env decisions applied: %s", sorted(changed.items()))
    return changed


def ensure_applied() -> None:
    """Apply decisions once per process (called where settings resolve)."""
    global _applied
    if _applied:
        return
    _applied = True
    try:
        apply_project_env()
    except Exception as exc:  # never break settings resolution
        log.warning(
            "Project env decisions not applied (%s). Inherited SMARTMEMORY_* "
            "variables are used as set",
            type(exc).__name__,
        )


# ── Store ─────────────────────────────────────────────────────────────────────


def _write_table(path: Path, raw: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tomli_w.dumps(raw))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_decision(
    name: str,
    decision: str,
    value: str | None = None,
    project_dir=None,
    path: Path | None = None,
) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision == "override":
        if err := override_error(name, value or ""):
            raise ValueError(err)
    path = path or _config_file()
    raw = read_config_table_strict(path)
    projects = raw.setdefault("projects", {})
    entry = projects.setdefault(project_key(project_dir), {})
    entry.setdefault("env", {})[name] = decision
    overrides = entry.setdefault("env_override", {})
    if decision == "override":
        overrides[name] = value
    else:
        overrides.pop(name, None)
    if not overrides:
        del entry["env_override"]
    _write_table(path, raw)


def clear_decisions(
    name: str | None = None, project_dir=None, path: Path | None = None
) -> list[str]:
    """Forget one (or every) decision for this project. Returns names removed."""
    path = path or _config_file()
    raw = read_config_table_strict(path)
    key = project_key(project_dir)
    entry = project_entry(raw, key)
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    removed = [n for n in env if name is None or n == name]
    if not removed:
        return []
    for n in removed:
        env.pop(n, None)
        if isinstance(entry.get("env_override"), dict):
            entry["env_override"].pop(n, None)
    entry = {k: v for k, v in entry.items() if v}
    if entry:
        raw["projects"][key] = entry
    else:
        del raw["projects"][key]
        if not raw["projects"]:
            del raw["projects"]
    _write_table(path, raw)
    return removed


def carry_over_tables(path: Path, data: dict) -> None:
    """Copy tables save_config() does not own (``projects``) from the existing file.

    Raises ConfigUnreadableError when the file exists but cannot be read, so
    save_config() never replaces it with defaults.
    """
    for key, value in read_config_table_strict(path).items():
        if key not in _MANAGED_TABLES and key not in data:
            data[key] = value


# ── Ask or warn ───────────────────────────────────────────────────────────────


def _where(var: InheritedVar) -> str:
    return f"from {var.source}" if var.source else "source unknown"


def warning_line(pending: list[InheritedVar]) -> str:
    names = ", ".join(
        f"{v.name} ({v.source})" if v.source else f"{v.name} (source unknown)"
        for v in pending
    )
    return (
        "WARNING: SmartMemory is using settings inherited from your shell that "
        f"this project has not reviewed: {names}. Run `smartmemory env` in a "
        "terminal to use, ignore or override them."
    )


def _choices(var: InheritedVar) -> list[str]:
    return ["use", "ignore"] if is_secret(var.name) else list(DECISIONS)


def _ask_value(var: InheritedVar) -> str:
    while True:
        value = click.prompt(f"  Value of {var.name} for this project", err=True)
        if not (err := override_error(var.name, value)):
            return value
        click.echo(f"  {err}", err=True)


def prompt_decisions(pending: list[InheritedVar], key: str) -> dict[str, str]:
    """Ask once for ``pending`` and save the answers. Returns {name: decision}.

    Raises ConfigUnreadableError before asking when the config file is broken.
    """
    read_config_table_strict(_config_file())
    echo = lambda msg="": click.echo(msg, err=True)  # noqa: E731
    width = max(len(v.name) for v in pending)
    echo("SmartMemory found settings inherited from your shell:")
    for v in pending:
        echo(f"  {v.name.ljust(width)}  {_where(v)}")
    echo(f"They may belong to a different project. For this project ({key}):")
    echo("  use       keep using them and stop asking")
    echo("  ignore    act as if they were not set, in this project only")
    echo("  override  enter a value for this project that beats the shell value")
    if len(pending) > 1:
        echo("  each      decide one variable at a time")
    options = list(DECISIONS) + (["each"] if len(pending) > 1 else [])
    choice = click.prompt(
        "Choice", type=click.Choice(options), err=True, show_choices=True
    )
    answers: dict[str, str] = {}
    for v in pending:
        decision = choice
        if choice == "each" or decision not in _choices(v):
            if decision == "override":
                echo(f"  {v.name} holds a secret, so it can only be used or ignored.")
            decision = click.prompt(
                f"  {v.name} ({_where(v)})",
                type=click.Choice(_choices(v)),
                err=True,
                show_choices=True,
            )
        value = _ask_value(v) if decision == "override" else None
        set_decision(v.name, decision, value, project_dir=key)
        answers[v.name] = decision
    echo(
        f"Saved for this project in {_config_file()}. Change it with `smartmemory env`."
    )
    if any(d != "use" for d in answers.values()):
        echo(
            "If the local service is already running, run `smartmemory restart` "
            "so it picks up the change."
        )
    return answers


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def check_inherited(command: str | None, is_tty=None) -> None:
    """CLI entry hook: apply decisions, then ask (TTY) or warn once (no TTY)."""
    ensure_applied()
    if os.environ.get(CHECK_SWITCH) == "0" or command in _QUIET_COMMANDS:
        return
    pending = [v for v in inherited_vars() if v.decision is None]
    if not pending:
        return
    if not (_stdin_is_tty() if is_tty is None else is_tty):
        click.echo(warning_line(pending), err=True)
        return
    try:
        prompt_decisions(pending, project_key())
    except ConfigUnreadableError as exc:
        # Never lose the user's command over a broken config: say why, carry on.
        click.echo(exc.format_message(), err=True)
        return
    apply_project_env()  # this command already runs with the new decisions


# ── `smartmemory env` ─────────────────────────────────────────────────────────


@click.group("env", invoke_without_command=True)
@click.pass_context
def env_cmd(ctx) -> None:
    """Review SMARTMEMORY_* settings inherited from your shell, per project.

    \b
    smartmemory env                          List them and decide (asks in a terminal)
    smartmemory env set NAME use|ignore      Decide one without a prompt
    smartmemory env set NAME override VALUE  Store a project value that beats the shell
    smartmemory env reset [NAME]             Forget decisions so you are asked again
    """
    if ctx.invoked_subcommand is not None:
        return
    ensure_applied()
    key = project_key()
    found = inherited_vars()
    click.echo(f"Project: {key}")
    click.echo(f"Decisions file: {_config_file()}")
    if not found:
        click.echo("No SMARTMEMORY_* settings are inherited from your shell.")
        return
    width = max(len(v.name) for v in found)
    for v in found:
        click.echo(
            f"  {v.name.ljust(width)}  {v.decision or 'undecided':9}  {_where(v)}"
        )
    pending = [v for v in found if v.decision is None]
    if pending and _stdin_is_tty():
        prompt_decisions(pending, key)
    elif pending:
        click.echo(
            "Decide without a prompt: smartmemory env set NAME use|ignore, "
            "or smartmemory env set NAME override VALUE"
        )


def _check_name(name: str) -> None:
    if not name.startswith(env_sources.PREFIX) or name == CHECK_SWITCH:
        raise click.BadParameter(
            f"only {env_sources.PREFIX}* settings can be decided", param_hint="NAME"
        )


@env_cmd.command("set")
@click.argument("name")
@click.argument("decision", type=click.Choice(list(DECISIONS)))
@click.argument("value", required=False)
def env_set_cmd(name: str, decision: str, value: str | None) -> None:
    """Decide one inherited setting for this project."""
    _check_name(name)
    if decision != "override" and value is not None:
        raise click.UsageError("VALUE is only used with override.")
    if decision == "override" and value is None:
        if not _stdin_is_tty():
            raise click.UsageError("override needs a VALUE.")
        value = _ask_value(InheritedVar(name, None, None))
    try:
        set_decision(name, decision, value)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    click.echo(f"{name}: {decision} for project {project_key()}")


@env_cmd.command("reset")
@click.argument("name", required=False)
def env_reset_cmd(name: str | None) -> None:
    """Forget decisions for this project so you are asked again."""
    removed = clear_decisions(name)
    if removed:
        click.echo(f"Forgot: {', '.join(removed)}")
    else:
        click.echo("Nothing to forget for this project.")
