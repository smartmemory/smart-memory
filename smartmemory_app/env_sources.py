"""CLI-ENV-DETECT-1: find SMARTMEMORY_* variables inherited from the shell.

A ``SMARTMEMORY_*`` variable exported in ``~/.zshrc`` for one project silently
steers every ``sm`` command in every other project (owner demo, 2026-10-17).
This module answers "which ones are set, where were they exported, and what has
this project decided about them". It never reads, stores or returns a value.

``inherited_vars`` is the interface SETUP-DOCTOR-1 calls. The per-project
decisions it reports are written by ``smartmemory_app.env_pin`` into the user
config file (``config.config_path()``) under::

    [projects."<project root>".env]
    SMARTMEMORY_MODE = "ignore"      # "use" | "ignore" | "override"

    [projects."<project root>".env_override]
    SMARTMEMORY_MODE = "local"       # only for "override"
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

PREFIX = "SMARTMEMORY_"
DECISIONS = ("use", "ignore", "override")

# Meta switch, not a setting: `SMARTMEMORY_ENV_CHECK=0` silences the prompt and
# the warning (CI images and test suites that set SMARTMEMORY_* on purpose).
# Never reported as an inherited variable itself.
CHECK_SWITCH = "SMARTMEMORY_ENV_CHECK"

# Shell startup files, per shell, in load order. A later assignment wins in the
# shell, so the last match in a group is the one that set the variable.
_RC_FILES = {
    "zsh": (".zshenv", ".zprofile", ".zshrc", ".zlogin"),
    "bash": (".profile", ".bash_profile", ".bash_login", ".bashrc"),
    "fish": (".config/fish/config.fish", ".config/fish/conf.d/*.fish"),
    "sh": (".profile",),
}
_MAX_RC_BYTES = 1_000_000

# Names present in os.environ before env_pin applied this project's decisions.
# Applying "ignore" removes a name from os.environ, so without this snapshot a
# later caller (the doctor) could no longer see that the shell set it.
_pre_pin_names: frozenset[str] | None = None


@dataclass(frozen=True)
class InheritedVar:
    name: str
    source: str | None  # "path:line" where exported, e.g. "~/.zshrc:42"
    decision: str | None  # "use" | "ignore" | "override" | None (undecided)


def record_pre_pin_names(names) -> None:
    """Remember which SMARTMEMORY_* names the process inherited (env_pin calls this)."""
    global _pre_pin_names
    if _pre_pin_names is None:
        _pre_pin_names = frozenset(n for n in names if _is_setting(n))


def _is_setting(name: str) -> bool:
    return name.startswith(PREFIX) and name != CHECK_SWITCH


def inherited_vars(
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike | None = None,
    project_dir: str | os.PathLike | None = None,
) -> list[InheritedVar]:
    """Every SMARTMEMORY_* variable set in ``environ``, sorted by name.

    ``environ`` defaults to the process environment as inherited, before this
    project's ignore/override decisions were applied. ``home`` is where shell
    startup files and the user config are looked up (default: the real home).
    ``project_dir`` defaults to the current directory. Values are never read
    from ``environ`` and never returned.
    """
    if environ is None:
        names = (
            set(_pre_pin_names)
            if _pre_pin_names is not None
            else {n for n in os.environ if _is_setting(n)}
        )
        env: Mapping[str, str] = os.environ
    else:
        names = {n for n in environ if _is_setting(n)}
        env = environ
    if not names:
        return []
    home_path = Path(home) if home is not None else Path.home()
    decisions = project_decisions(project_key(project_dir), config_file(environ, home))
    sources = rc_sources(names, home_path, shell=env.get("SHELL", ""))
    return [
        InheritedVar(name=n, source=sources.get(n), decision=decisions.get(n))
        for n in sorted(names)
    ]


# ── Where each variable was exported ──────────────────────────────────────────


def _assignment_pattern(names) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(
        rf"^\s*(?:(?:export|declare\s+-x|typeset\s+-x)\s+)?(?P<n>{alt})=",
    )


def _fish_pattern(names) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"^\s*set\s+(?:-[A-Za-z]+\s+)*(?P<n>{alt})(?:\s|$)")


def _rc_paths(home: Path, rel: str) -> list[Path]:
    if "*" in rel:
        return sorted(home.glob(rel))
    return [home / rel]


def _display(home: Path, path: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def rc_sources(names, home: Path, shell: str = "") -> dict[str, str]:
    """Map each name to "path:line" of its last assignment in shell startup files.

    The current shell's files are searched first; other shells' files only for
    names not found there. Lines are matched, never parsed for values.
    """
    names = set(names)
    current = next((s for s in ("zsh", "bash", "fish") if s in Path(shell).name), "")
    order = [current] if current else []
    order += [s for s in ("zsh", "bash", "fish", "sh") if s not in order]
    found: dict[str, str] = {}
    seen: set[Path] = set()
    for shell_name in order:
        pending = names - set(found)
        if not pending:
            break
        pattern = (_fish_pattern if shell_name == "fish" else _assignment_pattern)(
            pending
        )
        group: dict[str, str] = {}
        for rel in _RC_FILES[shell_name]:
            for path in _rc_paths(home, rel):
                if path in seen:
                    continue
                seen.add(path)
                for lineno, line in _read_lines(path):
                    if line.lstrip().startswith("#"):
                        continue
                    if m := pattern.match(line):
                        group[m.group("n")] = f"{_display(home, path)}:{lineno}"
        found.update(group)
    return found


def _read_lines(path: Path):
    try:
        if not path.is_file() or path.stat().st_size > _MAX_RC_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Shell startup file %s unreadable (%s)", path, type(exc).__name__)
        return []
    return enumerate(text.splitlines(), start=1)


# ── Per-project decisions (read side; env_pin writes) ─────────────────────────


def project_key(project_dir: str | os.PathLike | None = None) -> str:
    """The project a decision belongs to: the git top level, else the directory."""
    start = os.path.realpath(project_dir if project_dir is not None else os.getcwd())
    try:
        result = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
        )
        top = result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        top = ""
    return os.path.realpath(top) if top else start


def config_file(
    environ: Mapping[str, str] | None = None, home: str | os.PathLike | None = None
) -> Path:
    """The user config file (same path rules as ``config.config_path``)."""
    if environ is None and home is None:
        from smartmemory_app.config import config_path

        return config_path()
    env = os.environ if environ is None else environ
    base_home = Path(home) if home is not None else Path.home()
    if os.name == "nt":
        base = Path(env.get("APPDATA") or base_home)
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or base_home / ".config")
    return base / "smartmemory" / "config.toml"


def read_config_table(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning(
            "Config %s unreadable, project env decisions ignored (%s)",
            path,
            type(exc).__name__,
        )
        return {}


def project_entry(raw: dict, key: str) -> dict:
    projects = raw.get("projects")
    entry = projects.get(key) if isinstance(projects, dict) else None
    return entry if isinstance(entry, dict) else {}


def project_decisions(key: str, path: Path) -> dict[str, str]:
    """name -> decision for one project. Unknown decision strings are dropped."""
    env = project_entry(read_config_table(path), key).get("env")
    if not isinstance(env, dict):
        return {}
    return {n: d for n, d in env.items() if _is_setting(n) and d in DECISIONS}
