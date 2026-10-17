"""SETUP-DOCTOR-1: one table that says what the CLI will actually use.

Every row names the setting the CLI resolved, the source that won (flag, env var,
config file, keychain, default) and a one-line fix when it is not PASS. A row
that cannot be checked is WARN naming what was not checked, never silent.

Where a resolver exists, doctor calls it instead of re-deriving the answer
(fix round 1 of the unit review):
  mode, API URL, team, data dir, port, LLM settings   config.load_config
  hosted X-Workspace-Id   remote_backend.resolve_team_id (RemoteMemory uses it)
  local recall workspace  recall_format.derive_workspace_id
  LLM env translation     config._apply_local_llm_env, storage.pin_llm_model
  LLM provider and model  core llm.provider_pin, get_default_model and the
                          extractor config call_llm reads
  LLM endpoint            LLMSingleExtractor._resolve_api_base, then call_llm's
                          own order (core has no single endpoint function)
  API key                 SMARTMEMORY_API_KEY > OS keychain > Windows credential file
  LLM keys                env > OS keychain > shell profile export (daemon start only)

No env value is ever printed: a setting that came from an env variable is shown
as ``$NAME``, and ``mask_env_values`` scrubs every line ``sm doctor`` prints.
Rows show key NAMES and yes/no only. Values that come from the config file or
the account (workspace id, URL host, data dir, port) are printed because they
are what this command exists to show.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote, urlsplit

log = logging.getLogger(__name__)

PASS, WARN, FAIL = "pass", "warn", "fail"

# Variables that change where the CLI reads or writes, or which LLM it uses.
# An undecided inherited one of these is a FAIL; any other SMARTMEMORY_* is a WARN.
STEERING_VARS = frozenset(
    {
        "SMARTMEMORY_MODE",
        "SMARTMEMORY_API_URL",
        "SMARTMEMORY_API_KEY",
        "SMARTMEMORY_TEAM_ID",
        "SMARTMEMORY_WORKSPACE_ID",
        "SMARTMEMORY_DATA_DIR",
        "SMARTMEMORY_DAEMON_PORT",
        "SMARTMEMORY_LLM_PROVIDER",
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_LLM_BASE_URL",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "SMARTMEMORY_CONFIG",
        "SMARTMEMORY_CONFIG_DIR",
    }
)

_PROVIDER_KEYS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

_DEFAULT_PORT = 9014

# Which rows each caller needs. The tour runs its own isolated local daemon and
# store, so the user's daemon, tenant and store rows do not apply to it.
_CONTEXT_ROWS = {
    "doctor": (
        "python",
        "config",
        "mode",
        "inherited",
        "credentials",
        "tenant",
        "llm",
        "service",
        "graph",
    ),
    "setup": (
        "python",
        "config",
        "mode",
        "inherited",
        "credentials",
        "tenant",
        "llm",
        "service",
        "graph",
    ),
    "tour": ("python", "config", "mode", "inherited", "llm"),
}


@dataclass(frozen=True)
class Row:
    check: str
    status: str
    detail: str
    fix: str = ""


class CheckUnavailable(Exception):
    """A probe could not run. The row becomes WARN naming what was not checked."""


def _default_detector():
    try:
        from smartmemory_app.env_sources import inherited_vars  # CLI-ENV-DETECT-1
    except ImportError:
        return None
    return inherited_vars


def _default_http_get(url: str, headers: dict | None = None, timeout: float = 5.0):
    """Return (status_code, parsed JSON or None). Transport errors raise."""
    import httpx

    # trust_env stays at its default so proxies apply exactly as they do for the
    # CLI's own hosted calls (remote_backend uses httpx defaults too).
    response = httpx.get(url, headers=headers or {}, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


def _default_keyring_has(service: str, name: str) -> bool:
    try:
        import keyring

        if keyring.get_password(service, name):
            return True
    except Exception as exc:
        raise CheckUnavailable(f"OS keychain ({type(exc).__name__})") from exc
    if sys.platform == "win32" and name == "api_key":
        from smartmemory_app.windows_credentials import read_key

        try:
            return bool(read_key())
        except OSError as exc:
            raise CheckUnavailable(
                f"Windows credential file ({type(exc).__name__})"
            ) from exc
    return False


def _default_profile_has(name: str) -> bool:
    """The same profile lookup the daemon start uses, keeping "unreadable" apart from "absent"."""
    from smartmemory_app.setup import _profile_lookup

    found = _profile_lookup(name)
    if found.error:
        raise CheckUnavailable(f"shell profile {found.profile} ({found.error})")
    return bool(found.value)


def _default_daemon_status():
    from smartmemory_app.daemon import get_status

    return get_status()


def _default_daemon_expected() -> bool:
    from smartmemory_app.daemon import should_be_running

    return should_be_running()


def _default_load_config():
    from smartmemory_app.config import load_config

    return load_config()


def _default_api_key() -> str:
    from smartmemory_app.config import get_api_key

    return get_api_key()


def _default_config_path() -> Path:
    from smartmemory_app.config import config_path

    return config_path()


@dataclass
class Context:
    """Everything the checks read. Tests replace any field with a fake."""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    cwd: Path = field(default_factory=Path.cwd)
    platform: str = sys.platform
    python_version: tuple = tuple(sys.version_info[:3])
    prefix: str = sys.prefix
    base_prefix: str = sys.base_prefix
    executable: str = sys.executable
    config_path: Callable[[], Path] = _default_config_path
    load_config: Callable[[], Any] = _default_load_config
    http_get: Callable[..., tuple] = _default_http_get
    keyring_has: Callable[[str, str], bool] = _default_keyring_has
    profile_has: Callable[[str], bool] = _default_profile_has
    daemon_status: Callable[[], Optional[dict]] = _default_daemon_status
    daemon_expected: Callable[[], bool] = _default_daemon_expected
    api_key: Callable[[], str] = _default_api_key
    detect: Optional[Callable[..., list]] = field(default_factory=_default_detector)
    # True when ``environ`` is this process's own environment. The detector then
    # reads the names the shell passed in, including ones a per-project "ignore"
    # decision removed (CLI-ENV-DETECT-1 env_pin), which ``environ`` no longer has.
    from_process: bool = False
    _cache: dict = field(default_factory=dict, repr=False)

    def cached(self, key: str, compute: Callable[[], Any]) -> Any:
        """Run each probe once per doctor pass, re-raising its failure each time."""
        if key not in self._cache:
            try:
                self._cache[key] = ("ok", compute())
            except Exception as exc:
                self._cache[key] = ("error", exc)
        kind, value = self._cache[key]
        if kind == "error":
            raise value
        return value


# ── helpers ─────────────────────────────────────────────────────────────────


def _safe_url(url: str) -> str:
    """scheme://host[:port]/path only: never userinfo, query or fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "<unknown>"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}{parts.path.rstrip('/')}"
    except Exception:
        return "<invalid URL>"


def _shell_env(ctx: Context) -> dict:
    """The environment as the shell passed it, after this project's sm env
    decisions but before SmartMemory translated config settings into env names
    (config.DERIVED_ENV). Resolvers then run that translation again on a copy."""
    env = dict(ctx.environ)
    if ctx.from_process:
        from smartmemory_app.config import DERIVED_ENV

        for name, (_origin, value) in DERIVED_ENV.items():
            if env.get(name) == value:
                env.pop(name)
    return env


@contextmanager
def _patched_environ(env: Mapping[str, str]):
    """Run a resolver that reads os.environ against ``env``, then restore it.

    Core's LLM resolvers take no environ argument. Doctor runs in the CLI's main
    thread before any engine starts, so nothing else reads os.environ meanwhile.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# Env names whose values ``mask_env_values`` replaces. Every SMARTMEMORY_* name
# is masked as well. These are the non-prefixed inputs the CLI and core read.
_MASKED_NAMES = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "LLM_API_KEY",
    "LLM_PROVIDER",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "BEDROCK_MODEL_ID",
)
# Values doctor checked against a closed set may be printed: they say nothing
# about the user beyond the choice itself, and masking them would garble text
# ("local" mode, a port number, a provider name).
_CLOSED_VALUES = frozenset(
    {
        "local",
        "remote",
        "none",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "groq",
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "ollama",
        "lmstudio",
        "localai",
        "bedrock",
        "claude-agent",
    }
)


# Name prefixes whose values are masked. EXTRACTOR_* are core's config leaf
# overrides for the extractor section (configuration/environment.py), which
# call_llm reads for the model.
_MASKED_PREFIXES = ("SMARTMEMORY_", "EXTRACTOR_")
# Env names doctor found supplying a value core read (a config leaf override
# under another prefix, for example a namespace overlay). Filled while doctor runs.
_ATTRIBUTED: set[str] = set()


def _value_forms(value: str, min_len: int = 4) -> set[str]:
    """``value`` plus the forms the CLI prints it in (trimmed, expanded, resolved, URL host).

    Forms shorter than ``min_len`` are dropped.
    """
    forms = {value, value.rstrip("/")}
    try:
        expanded = os.path.expanduser(value)
        forms |= {expanded, os.path.realpath(expanded), os.path.abspath(expanded)}
    except (OSError, ValueError):
        pass
    if "://" in value:
        safe = _safe_url(value)
        forms.add(safe)
        try:
            parts = urlsplit(value)
            if parts.hostname:
                forms.add(parts.hostname)
                if parts.port:
                    forms.add(f"{parts.hostname}:{parts.port}")
        except ValueError:
            pass
    # A value inside a JSON record, a repr or a URL is printed escaped, and the
    # escaped text no longer contains the raw value.
    for form in list(forms):
        forms |= {json.dumps(form)[1:-1], repr(form)[1:-1], quote(form, safe="")}
    return {form for form in forms if len(form) >= max(min_len, 1)}


def _value_patterns(value: str) -> list[str]:
    """Regexes for every printed form of an env value, matched case-insensitively.

    This is the guessing pass for values nobody declared secret (``sm doctor``
    output). Supplied secrets and identifiers use ``_exact_value_patterns``.

    Forms of 4+ characters match anywhere. A shorter value matches as a whole
    token when it holds a digit or symbol, and only quoted or after ``=`` when it
    is plain letters, so masking a value like ``abc`` never eats ordinary words
    (core logs a bad value as ``NAME='abc'``).
    """
    patterns = [re.escape(form) for form in _value_forms(value)]
    for short in {value, value.strip(), value.rstrip("/")}:
        if not 0 < len(short) < 4:
            continue
        escaped = re.escape(short)
        if short.isalpha():
            patterns.append(rf"(?<=['\"=]){escaped}(?=['\"]|[\s,.)]|$)")
        else:
            patterns.append(rf"(?<!\w){escaped}(?!\w)")
    return patterns


def _exact_value_patterns(value: str) -> list[str]:
    """Regexes for every printed form of a supplied secret or identifier.

    No length or character-class rule decides whether a form is masked: a form of
    4+ characters matches anywhere, a shorter one (one letter, two digits, ``a1``)
    matches as a whole token, so ``abc`` goes in ``team abc``, ``'abc'``, a JSON
    record or a message that is only ``abc``, and stays in ``abcdef``.
    """
    forms = set()
    for raw in {value, value.strip()}:
        if raw:
            forms |= _value_forms(raw, min_len=1)
    return [
        re.escape(form) if len(form) >= 4 else rf"(?<!\w){re.escape(form)}(?!\w)"
        for form in forms
    ]


def _mask_spans(text: str, labelled_patterns: Iterable[tuple[str, str]]) -> str:
    """Replace every match of every pattern in ``text`` with its label, in one pass.

    Matches are found on the original text and overlapping matches are merged, so
    one label can never hide part of another value or be rewritten by a later one.
    """
    spans = sorted(
        (match.start(), match.end(), label)
        for pattern, label in labelled_patterns
        for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        if match.end() > match.start()
    )
    merged: list[tuple[int, int, str]] = []
    for start, end, label in spans:
        if merged and start < merged[-1][1]:
            first, last, kept = merged[-1]
            if end - start > last - first:
                kept = label
            merged[-1] = (first, max(last, end), kept)
        else:
            merged.append((start, end, label))
    pieces, position = [], 0
    for start, end, label in merged:
        pieces += [text[position:start], label]
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


# An identifier (team, workspace, tenant id) names who the user is, so a purely
# numeric one is masked like any other. A bare number in other env values (a port)
# says nothing about the user and stays visible.
_IDENTIFIER_NAME = re.compile(r"(?:TEAM|WORKSPACE|TENANT)_ID$", re.IGNORECASE)


def is_identifier_name(name: str) -> bool:
    return bool(_IDENTIFIER_NAME.search(name))


def _masked_name(name: str) -> bool:
    return (
        name.startswith(_MASKED_PREFIXES)
        or name in _MASKED_NAMES
        or name in _ATTRIBUTED
    )


def mask_env_values(text: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """Replace every env-sourced value in ``text`` with ``$NAME (env)``.

    Covers every SMARTMEMORY_* and EXTRACTOR_* name, the LLM inputs in
    ``_MASKED_NAMES`` and any name doctor attributed a core value to, whatever
    the value's length or letter case. Names SmartMemory itself derived from the
    config file (config.DERIVED_ENV) are config values, not env values, and stay
    visible.
    """
    from smartmemory_app.config import DERIVED_ENV

    env = os.environ if environ is None else environ
    pairs = []
    for name, value in env.items():
        if not _masked_name(name):
            continue
        derived = DERIVED_ENV.get(name)
        if not value or (derived and derived[1] == value):
            continue
        if value.strip().lower() in _CLOSED_VALUES or (
            value.strip().isdigit() and not is_identifier_name(name)
        ):
            continue
        pairs.append((value, f"${name} (env)"))
    return _mask_spans(
        text,
        [
            (pattern, label)
            for value, label in pairs
            if value and value.strip()
            for pattern in _value_patterns(value)
        ],
    )


def mask_exact_values(text: str, labelled: Iterable[tuple[str, str]]) -> str:
    """Replace every printed form of each ``(value, label)`` in ``text`` with its label.

    Matching is by the exact supplied value, whatever its length, character class,
    letter case or escaping (JSON, repr, URL), never by a pattern that guesses what a
    secret looks like (``_exact_value_patterns``).
    """
    return _mask_spans(
        text,
        [
            (pattern, label)
            for value, label in labelled
            if value and value.strip()
            for pattern in _exact_value_patterns(value)
        ],
    )


class _MaskingFilter(logging.Filter):
    """Masks env values in every log record a handler emits while doctor runs.

    Core logs some inputs verbatim (an unknown LLM_PROVIDER pin, a config read
    error naming the path), so masking only the finished rows is not enough.
    """

    def __init__(self, environs: list[Mapping[str, str]]):
        super().__init__()
        self._environs = environs

    def _mask(self, text: str) -> str:
        for environ in [*self._environs, os.environ]:
            text = mask_env_values(text, environ)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        masked = self._mask(message)
        if masked != message:
            record.msg, record.args = masked, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._mask(record.exc_text)
        if record.stack_info:
            record.stack_info = self._mask(record.stack_info)
        return True


@contextmanager
def masked_logging():
    """Mask env values in every log line any handler prints until the block ends."""
    mask = _MaskingFilter([dict(os.environ)])
    handlers = set(logging.getLogger().handlers)
    for logger in list(logging.Logger.manager.loggerDict.values()):
        handlers.update(getattr(logger, "handlers", ()))
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    for handler in handlers:
        handler.addFilter(mask)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(mask)


def _file_settings(ctx: Context) -> dict:
    """Raw config file sections, for attributing a value to the file. {} if absent."""
    path = ctx.config_path()
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _inherited(ctx: Context) -> Optional[dict]:
    """name -> InheritedVar from CLI-ENV-DETECT-1, or None when not installed."""
    if ctx.detect is None:
        return None

    def run():
        environ = None if ctx.from_process else dict(ctx.environ)
        found = ctx.detect(environ=environ, home=ctx.home, project_dir=ctx.cwd)
        return {var.name: var for var in found}

    return ctx.cached("inherited", run)


def _env_source(ctx: Context, name: str) -> str:
    """'env NAME' plus where it is exported, when the detector knows."""
    label = f"env {name}"
    try:
        found = _inherited(ctx)
    except Exception:
        return label
    var = (found or {}).get(name)
    if var is not None and var.source:
        label += f", exported at {var.source}"
    return label


def _shown(ctx: Context, name: str, text: str, env_text: Optional[str] = None) -> str:
    """``text``, or ``$NAME`` when the setting came from that env variable.

    Doctor never prints a SMARTMEMORY_* value. The only exceptions are values it
    has checked against a closed set: SMARTMEMORY_MODE (local or remote),
    SMARTMEMORY_DAEMON_PORT (a whole number) and a known LLM provider name.
    """
    if ctx.environ.get(name):
        return env_text if env_text is not None else f"${name}"
    return text


def _env_decision(ctx: Context, name: str) -> Optional[str]:
    try:
        found = _inherited(ctx)
    except Exception:
        return None
    var = (found or {}).get(name)
    return var.decision if var is not None else None


def _config(ctx: Context):
    return ctx.cached("config", ctx.load_config)


def _invalid_env(ctx: Context) -> Optional[tuple[str, str]]:
    """(var name, reason) for an env value that makes load_config raise."""
    mode = ctx.environ.get("SMARTMEMORY_MODE")
    if mode and mode not in ("local", "remote"):
        return "SMARTMEMORY_MODE", "must be local or remote"
    port = ctx.environ.get("SMARTMEMORY_DAEMON_PORT")
    if port:
        try:
            int(port)
        except ValueError:
            return "SMARTMEMORY_DAEMON_PORT", "must be a whole number"
    return None


def _mode(ctx: Context) -> Optional[str]:
    return _config(ctx).mode


def _api_url(ctx: Context) -> str:
    return _config(ctx).api_url.rstrip("/")


def _api_key_source(ctx: Context) -> Optional[str]:
    """Where the API key comes from, or None when there is none. Never the value."""
    if ctx.environ.get("SMARTMEMORY_API_KEY"):
        return _env_source(ctx, "SMARTMEMORY_API_KEY")
    if ctx.keyring_has("smartmemory", "api_key"):
        return "OS keychain"
    return None


def _api_key_value(ctx: Context) -> str:
    """Used only for the Authorization header. Never rendered."""
    if ctx.environ.get("SMARTMEMORY_API_KEY"):
        return ctx.environ["SMARTMEMORY_API_KEY"]
    return ctx.api_key()


def _ready(ctx: Context) -> tuple:
    return ctx.cached(
        "ready", lambda: ctx.http_get(f"{_api_url(ctx)}/ready", timeout=5.0)
    )


# ── rows ────────────────────────────────────────────────────────────────────


def check_python(ctx: Context) -> Row:
    version = ".".join(str(part) for part in ctx.python_version)
    in_venv = ctx.prefix != ctx.base_prefix
    where = f"venv {ctx.prefix}" if in_venv else f"system Python {ctx.executable}"
    if tuple(ctx.python_version[:2]) < (3, 11):
        return Row(
            "Python",
            FAIL,
            f"{version} ({where}). SmartMemory needs 3.11 or newer.",
            "Install Python 3.11+, then reinstall SmartMemory in a fresh venv.",
        )
    if not in_venv:
        return Row(
            "Python",
            WARN,
            f"{version}, not in a virtual environment ({ctx.executable})",
            "Install with pipx or uv tool, or in a venv, so other projects cannot change its packages.",
        )
    return Row("Python", PASS, f"{version}, {where}")


def _is_loopback(url: str) -> bool:
    """True for a URL on this machine (localhost or a loopback address)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_config(ctx: Context) -> Row:
    path = ctx.config_path()
    if ctx.platform == "win32":
        moved_by = "APPDATA" if ctx.environ.get("APPDATA") else None
    else:
        moved_by = "XDG_CONFIG_HOME" if ctx.environ.get("XDG_CONFIG_HOME") else None
    where = f"{path} (location set by env {moved_by})" if moved_by else str(path)
    try:
        config_exists = path.exists()
    except OSError as exc:
        return Row(
            "Config file",
            WARN,
            f"not checked: {where} cannot be read ({type(exc).__name__})",
            f"Fix the permissions on {path.parent}, then run: sm doctor",
        )
    if not config_exists:
        return Row(
            "Config file",
            WARN,
            f"none at {where}. The first sm command will create a local-mode config.",
            "Run: sm setup",
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        # Cannot check: an unreadable file says nothing about what is in it.
        return Row(
            "Config file",
            WARN,
            f"not checked: {where} cannot be read ({type(exc).__name__})",
            f"Fix the permissions on {path}, then run: sm doctor",
        )
    except tomllib.TOMLDecodeError:
        return Row(
            "Config file",
            FAIL,
            f"{where} is not valid TOML, so SmartMemory treats it as unconfigured",
            f"Fix or delete {path}, then run: sm setup",
        )
    file_mode = raw.get("smartmemory", {}).get("mode")
    if file_mode is not None and file_mode not in ("local", "remote"):
        return Row(
            "Config file",
            FAIL,
            f"{where} has an invalid mode, so SmartMemory treats it as unconfigured",
            "Run: sm config mode local (or remote)",
        )
    return Row("Config file", PASS, where)


def check_mode(ctx: Context, context: str = "doctor") -> Row:
    invalid = _invalid_env(ctx)
    if invalid:
        name, reason = invalid
        return Row(
            "Mode",
            FAIL,
            f"{_env_source(ctx, name)} is invalid ({reason}), so every sm command stops with an error",
            f"Unset {name} or give it a valid value.",
        )
    mode = _mode(ctx)
    try:
        file_mode = _file_settings(ctx).get("smartmemory", {}).get("mode")
    except Exception:
        file_mode = None  # check_config reports the unreadable file
    env_mode = ctx.environ.get("SMARTMEMORY_MODE")
    if env_mode:
        why = _env_source(ctx, "SMARTMEMORY_MODE")
    elif mode:
        why = "config file"
    else:
        why = "nothing set"
    if context == "tour" and mode == "remote":
        return Row(
            "Mode",
            FAIL,
            f"remote (from {why}). The tour runs only in local mode.",
            "Unset SMARTMEMORY_MODE"
            if env_mode
            else "Run: sm config mode local (switch back later with sm config mode remote)",
        )
    if mode is None:
        return Row(
            "Mode",
            WARN,
            "not configured. The first sm command will choose local mode.",
            "Run: sm setup",
        )
    if env_mode and _env_decision(ctx, "SMARTMEMORY_MODE") not in ("use", "override"):
        if file_mode and file_mode != env_mode:
            detail = f"{mode} (from {why}), overriding {file_mode} in the config file"
        else:
            detail = f"{mode} (from {why}), not from the config file"
        return Row(
            "Mode",
            WARN,
            detail,
            f"If you did not mean this, unset SMARTMEMORY_MODE, or run: sm config mode {mode}",
        )
    return Row("Mode", PASS, f"{mode} (from {why})")


def check_inherited(ctx: Context) -> Row:
    present = sorted(
        name
        for name in ctx.environ
        if name.startswith("SMARTMEMORY_") and name != "SMARTMEMORY_ENV_CHECK"
    )
    if ctx.detect is None:
        names = ", ".join(present) if present else "none set in this shell"
        return Row(
            "Inherited SMARTMEMORY_* vars",
            WARN,
            f"inherited-variable detection not installed (CLI-ENV-DETECT-1), "
            f"so their sources were not checked. Present: {names}",
            "Unset any SMARTMEMORY_* variable you did not set for this project."
            if present
            else "",
        )
    try:
        found = _inherited(ctx)
    except Exception as exc:
        log.warning("Inherited-variable detection failed: %s", type(exc).__name__)
        return Row(
            "Inherited SMARTMEMORY_* vars",
            WARN,
            f"detection failed ({type(exc).__name__}), so inherited variables were not checked",
            "Run: sm doctor --json and include it in a bug report (sm report).",
        )
    if not found:
        return Row("Inherited SMARTMEMORY_* vars", PASS, "none")

    def label(var) -> str:
        where = var.source or "source not found"
        decided = f", {var.decision}" if var.decision else ""
        return f"{var.name} ({where}{decided})"

    undecided = [var for var in found.values() if var.decision is None]
    steering = [var for var in undecided if var.name in STEERING_VARS]
    listing = ", ".join(
        label(var) for var in sorted(found.values(), key=lambda v: v.name)
    )
    fix = "Unset them where they are exported, or run: sm env (choose use, ignore or override for this project)"
    if steering:
        return Row(
            "Inherited SMARTMEMORY_* vars",
            FAIL,
            f"these change where the CLI reads and writes, and nobody chose them for this project: {listing}",
            fix,
        )
    if undecided:
        return Row("Inherited SMARTMEMORY_* vars", WARN, listing, fix)
    return Row("Inherited SMARTMEMORY_* vars", PASS, listing)


def check_credentials(ctx: Context) -> Row:
    if _mode(ctx) != "remote":
        return Row("API key", PASS, "not needed in local mode")
    try:
        source = _api_key_source(ctx)
    except CheckUnavailable as exc:
        return Row(
            "API key",
            WARN,
            f"not checked: {exc} could not be read",
            "Set SMARTMEMORY_API_KEY for this shell, or run: sm setup --mode remote --api-key KEY",
        )
    if source is None:
        return Row(
            "API key",
            FAIL,
            "none found (checked env SMARTMEMORY_API_KEY and the OS keychain)",
            "Run: sm setup --mode remote --api-key KEY",
        )
    return Row("API key", PASS, f"present (from {source})")


def _decided_env_label(ctx: Context, name: str) -> Optional[str]:
    """How an sm env decision labels a value the CLI took from ``name``, or None when undecided."""
    decision = _env_decision(ctx, name)
    if decision == "use":
        return f"{name} (env, accepted for this project)"
    if decision == "override":
        return f"the project override of {name} (sm env)"
    return None


def _team_source(ctx: Context, cfg) -> tuple[str, str]:
    """(configured team id or '', where it came from). load_config merges env over file."""
    if not cfg.team_id:
        return "", ""
    if ctx.environ.get("SMARTMEMORY_TEAM_ID"):
        label = _decided_env_label(ctx, "SMARTMEMORY_TEAM_ID")
        return "$SMARTMEMORY_TEAM_ID", label or _env_source(ctx, "SMARTMEMORY_TEAM_ID")
    return cfg.team_id, "config file team_id"


def _local_workspace(ctx: Context) -> Row:
    """Ask recall_format.derive_workspace_id, the resolver recall uses, and name its source."""
    from smartmemory_app.recall_format import derive_workspace_id

    with _patched_environ(ctx.environ):
        workspace = derive_workspace_id(str(ctx.cwd))
    env_value = ctx.environ.get("SMARTMEMORY_WORKSPACE_ID")
    if not (env_value and workspace == env_value):
        return Row(
            "Tenant and workspace",
            PASS,
            f"local store, no tenant. Recall workspace {workspace} comes from the current project folder.",
        )
    label = _decided_env_label(ctx, "SMARTMEMORY_WORKSPACE_ID")
    if label:
        return Row(
            "Tenant and workspace",
            PASS,
            f"local store, no tenant. Recall workspace comes from {label}, not from this folder.",
        )
    return Row(
        "Tenant and workspace",
        WARN,
        "local store, no tenant. Recall workspace $SMARTMEMORY_WORKSPACE_ID comes from "
        f"{_env_source(ctx, 'SMARTMEMORY_WORKSPACE_ID')}, not from this folder.",
        "Unset SMARTMEMORY_WORKSPACE_ID to scope recall by the current project, or run: sm env",
    )


def check_tenant(ctx: Context) -> Row:
    from smartmemory_app.remote_backend import resolve_team_id

    cfg = _config(ctx)
    if cfg.mode != "remote":
        return _local_workspace(ctx)
    team_label, team_where = _team_source(ctx, cfg)
    if team_label:
        ws_text = f"the CLI sends workspace {team_label} (from {team_where})"
    else:
        ws_text = "the CLI sends the account's default team from /auth/me"
    try:
        source = _api_key_source(ctx)
    except CheckUnavailable as exc:
        source = None
        unread = str(exc)
    else:
        unread = ""
    if source is None:
        return Row(
            "Tenant and workspace",
            WARN,
            f"not checked: no API key{' (' + unread + ' unreadable)' if unread else ''}, "
            f"so the account behind the CLI is unknown. Workspace: {ws_text}.",
            "Run: sm setup --mode remote --api-key KEY",
        )
    url = f"{_api_url(ctx)}/auth/me"
    url_label = _shown(
        ctx, "SMARTMEMORY_API_URL", _safe_url(url), "$SMARTMEMORY_API_URL/auth/me"
    )
    try:
        status, body = ctx.cached(
            "auth_me",
            lambda: ctx.http_get(
                url,
                headers={"Authorization": f"Bearer {_api_key_value(ctx)}"},
                timeout=5.0,
            ),
        )
    except Exception as exc:
        return Row(
            "Tenant and workspace",
            WARN,
            f"not checked: {url_label} unreachable ({type(exc).__name__}). "
            f"Workspace: {ws_text}.",
            "Check your network, then run: sm doctor",
        )
    if status == 401:
        return Row(
            "Tenant and workspace",
            FAIL,
            "the service rejected the API key (HTTP 401)",
            "Create a new key in the console, then run: sm setup --mode remote --api-key KEY",
        )
    if status != 200 or not isinstance(body, dict):
        return Row(
            "Tenant and workspace",
            WARN,
            f"not checked: {url_label} answered HTTP {status}. "
            f"Workspace: {ws_text}, which needs /auth/me when no team is set.",
            "Run sm doctor again. If it persists, run: sm report",
        )
    account = body.get("email") or body.get("id") or "unknown account"
    tenant = body.get("tenant_id") or "unknown"
    # The same resolver RemoteMemory uses for the X-Workspace-Id header.
    sent = resolve_team_id(cfg.team_id, body)
    if not team_label:
        team_label, team_where = sent, "account default_team_id from /auth/me"
    detail = f"account {account}, tenant {tenant}"
    if not sent:
        return Row(
            "Tenant and workspace",
            WARN,
            f"{detail}. The CLI sends no workspace: no team is set and the account has no default team.",
            f"Set team_id in {ctx.config_path()}, or set SMARTMEMORY_TEAM_ID for this shell.",
        )
    detail += f". The CLI sends workspace {team_label} (from {team_where})"
    console_ws = body.get("active_workspace_id") or body.get("default_workspace_id")
    if console_ws and sent != console_ws:
        return Row(
            "Tenant and workspace",
            WARN,
            f"{detail}, but the web console opens workspace {console_ws}, so the CLI and the console show different memories.",
            f"To use the console's workspace from the CLI, set team_id = {console_ws} under [remote] in "
            f"{ctx.config_path()} (an env SMARTMEMORY_TEAM_ID still wins over it).",
        )
    return Row(
        "Tenant and workspace",
        PASS,
        f"{detail}, the same workspace the web console opens."
        if console_ws
        else f"{detail}. Compare with the workspace shown in the web console.",
    )


def _llm_key_sources(ctx: Context, name: str) -> tuple[list[str], list[str]]:
    """(sources holding the key, sources that could not be read, named). Never values."""
    found, unread = [], []
    if ctx.environ.get(name):
        found.append("env")
    try:
        if ctx.keyring_has("smartmemory", name):
            found.append("OS keychain")
    except CheckUnavailable as exc:
        unread.append(str(exc) or "OS keychain")
    try:
        if ctx.profile_has(name):
            found.append("shell profile")
    except CheckUnavailable as exc:
        unread.append(str(exc) or "shell profile")
    except Exception as exc:
        unread.append(f"shell profile ({type(exc).__name__})")
    return found, unread


@dataclass(frozen=True)
class LlmRoute:
    """Provider, model and endpoint the CLI's LLM calls use, each with its source."""

    provider: str
    provider_src: str
    model: str  # display form: a value from env is shown as $NAME
    model_src: str
    endpoint: str  # display form, "" for the Claude Agent SDK
    endpoint_src: str
    probe_url: str  # the real URL, for probing a local server. Never rendered.
    pin: Optional[str]  # core LLM_PROVIDER pin
    from_base_url: bool  # endpoint is an explicit OpenAI-compatible base URL
    model_value: str = ""  # the model core sends, for tests. Never rendered.
    unread: tuple = ()  # core sources that could not be read, named
    refusal: str = ""  # why core would not send the call with this env

    def text(self) -> str:
        endpoint = (
            f"endpoint {self.endpoint} (from {self.endpoint_src})"
            if self.endpoint
            else f"no HTTP endpoint ({self.endpoint_src})"
        )
        return (
            f"provider {self.provider} (from {self.provider_src}), "
            f"model {self.model} (from {self.model_src}), {endpoint}"
        )


def _env_label(ctx: Context, shell: Mapping[str, str], name: str) -> str:
    label = _decided_env_label(ctx, name) if name.startswith("SMARTMEMORY_") else None
    return label or _env_source(ctx, name)


class _DryRunStop(Exception):
    """Raised where core would send the provider request, so doctor never sends it."""


@dataclass
class _CoreCall:
    """What core's own LLM code decided for one Tier-2 extraction call."""

    route: str = ""  # openai, claude-agent, bedrock, or "" when core sent nothing
    chosen: str = ""  # the model call_llm picked, before its route step rewrote it
    model: str = ""  # the model core sends
    api_base: Optional[str] = None
    refusal: str = ""
    config_path: str = ""
    config_llm: dict = field(default_factory=dict)
    unread: list = field(default_factory=list)


class _Records(logging.Handler):
    def __init__(self, level: int):
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _captured(name: str, level: int):
    """Collect the records ``name`` logs at ``level`` or above, whatever its configured level."""
    logger = logging.getLogger(name)
    handler = _Records(level)
    saved = (logger.level, logger.disabled)
    if logger.getEffectiveLevel() > level:
        logger.setLevel(level)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.disabled = saved[1]


@contextmanager
def _fresh_core_config():
    """Empty core's get_config cache for the block, so core reads its config file
    now, under the patched env, and doctor sees that read. Restored after."""
    from smartmemory import utils as core_utils

    with core_utils._config_lock:
        saved = dict(core_utils._config_cache)
        core_utils._config_cache.clear()
        try:
            yield
        finally:
            core_utils._config_cache.clear()
            core_utils._config_cache.update(saved)


@contextmanager
def _no_network():
    """In this thread, any socket connect or DNS lookup raises instead of leaving the machine.

    The dry run replaces core's three provider calls. If a newer core adds a
    route doctor does not know, its request stops here and is never sent.
    """
    import socket
    import threading

    owner = threading.get_ident()
    saved = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
    }
    real_getaddrinfo = socket.getaddrinfo

    def guard(real):
        def refuse(*args, **kwargs):
            if threading.get_ident() == owner:
                raise _DryRunStop("network")
            return real(*args, **kwargs)

        return refuse

    socket.socket.connect = guard(saved["connect"])
    socket.socket.connect_ex = guard(saved["connect_ex"])
    socket.getaddrinfo = guard(real_getaddrinfo)
    try:
        yield
    finally:
        socket.socket.connect = saved["connect"]
        socket.socket.connect_ex = saved["connect_ex"]
        socket.getaddrinfo = real_getaddrinfo


def _core_model(llm_cfg: Mapping) -> str:
    return (llm_cfg.get("model_name") or llm_cfg.get("model") or "") if llm_cfg else ""


def _core_extractor_llm() -> dict:
    from smartmemory.utils import get_config

    section = get_config("extractor")
    llm_cfg = section.get("llm") if hasattr(section, "get") else None
    return dict(llm_cfg) if isinstance(llm_cfg, Mapping) else {}


def _core_dry_run() -> _CoreCall:
    """Run core's real Tier-2 extraction call with the provider request replaced.

    LLMSingleExtractor._extract_impl -> call_llm runs unchanged: model choice,
    alias expansion, pin and key checks, endpoint choice. The three functions
    that would send (call_openai_chat, _call_claude_agent, _call_bedrock_converse)
    record their arguments and stop. Core's config file is read fresh, and a
    read core logs and swallows is recorded as unread. Run under _patched_environ.
    """
    from smartmemory.configuration.environment import EnvironmentHandler
    from smartmemory.plugins.extractors.llm_single import LLMSingleExtractor
    from smartmemory.utils import llm as core_llm

    call = _CoreCall()
    real_key_for = core_llm.request_api_key_for_model

    def key_for(model, api_base=None):  # call_llm calls it once, with its chosen model
        call.chosen = call.chosen or (model or "")
        return real_key_for(model, api_base)

    def terminal(route: str):
        def stop(*args, **kwargs):
            call.route, call.model = route, kwargs.get("model") or ""
            call.api_base = kwargs.get("api_base")
            raise _DryRunStop(route)

        return stop

    patches = {
        "request_api_key_for_model": key_for,
        "call_openai_chat": terminal("openai"),
        "_call_claude_agent": terminal("claude-agent"),
        "_call_bedrock_converse": terminal("bedrock"),
    }
    saved = {name: getattr(core_llm, name) for name in patches}
    call.config_path = EnvironmentHandler.resolve_config_path(
        os.environ.get("SMARTMEMORY_CONFIG", "config.json")
    )
    with (
        _fresh_core_config(),
        _captured("smartmemory.configuration.manager", logging.ERROR) as failed,
        _no_network(),
    ):
        for name, fake in patches.items():
            setattr(core_llm, name, fake)
        try:
            LLMSingleExtractor()._extract_impl("doctor dry run")
        except _DryRunStop as stop:
            if str(stop) == "network":
                call.route = ""
                call.refusal = (
                    "core tried a route doctor does not know, nothing was sent"
                )
        except (ValueError, RuntimeError) as exc:
            call.refusal = f"{type(exc).__name__}: {exc}"
        finally:
            for name, real in saved.items():
                setattr(core_llm, name, real)
        try:  # the same cached load call_llm used, so no second read
            call.config_llm = _core_extractor_llm()
        except Exception as exc:
            call.unread.append(f"core config {call.config_path} ({type(exc).__name__})")
    if failed and not call.unread:
        call.unread.append(
            f"core config {call.config_path} (core logged a read error and used an empty config)"
        )
    if call.route == "bedrock":
        call.chosen = call.chosen or call.model
    return call


def _leaf_override(env: Mapping[str, str], model: str) -> Optional[str]:
    """The env name core's config loader took ``model`` from, or None.

    Core applies env leaf overrides inside get_config (EXTRACTOR_LLM_MODEL_NAME
    over extractor.llm.model_name). Rather than rebuild its naming, doctor
    reloads core config without each env name holding that value and keeps the
    one whose removal changes the model.
    """
    for name, value in env.items():
        if value != model:
            continue
        without = {key: val for key, val in env.items() if key != name}
        try:
            with _patched_environ(without), _fresh_core_config():
                if _core_model(_core_extractor_llm()) != model:
                    return name
        except Exception:
            continue
    return None


def _llm_route(ctx: Context, cfg) -> LlmRoute:
    """Resolve the LLM route with the code every LLM call runs.

    1. The env the shell passed (``_shell_env``), then SmartMemory's own
       translation of the config into env names on a copy:
       config._apply_local_llm_env (load_config) and storage.pin_llm_model
       (apply_runtime_config, run before every local engine starts).
    2. Under that env, core's real extraction call runs with its provider
       request replaced (``_core_dry_run``). The model, the model sent (after
       alias expansion and prefix stripping) and the endpoint are what core
       decided, never re-derived here.
    3. The labels below only say which input produced core's answer. They follow
       call_llm's order (core utils/llm.py). When core refuses the call (no key),
       nothing is sent and the labels give the route it would take with a key.
    """
    from smartmemory.plugins.extractors.llm_single import LLMSingleExtractor
    from smartmemory.utils import llm as core_llm

    from smartmemory_app.config import _LOCAL_OPENAI_PROVIDERS, _apply_local_llm_env
    from smartmemory_app.storage import pin_llm_model

    shell = _shell_env(ctx)
    env = dict(shell)
    derived = {**_apply_local_llm_env(cfg, env), **pin_llm_model(cfg, env)}
    with _patched_environ(env):
        core = _core_dry_run()
        pin = core_llm.provider_pin()
        core_model = _core_model(core.config_llm)
        model = core.chosen or core_model or core_llm.get_default_model()
        extractor_base = LLMSingleExtractor()._resolve_api_base()
        base_url = os.environ.get("OPENAI_BASE_URL")
        agent = core.route == "claude-agent" or (
            not core.route
            and core_llm._is_claude_agent_model(model)
            and core_llm._claude_agent_sdk_available()
        )
        pinned = core_llm._PINNED_PROVIDERS
        groq_model = core_llm.is_groq_model(model)
    leaf = _leaf_override(env, model) if core_model and core_model == model else None

    def origin(name: str) -> str:
        """Where an env name the resolvers read came from."""
        if name in derived:
            setting = derived[name]
            if setting in ("llm_base_url", "llm_model"):
                env_name = f"SMARTMEMORY_{setting.upper()}"
                if shell.get(env_name):
                    return _env_label(ctx, shell, env_name)
                return f"config file {setting}"
            return f"{setting} for llm_provider {cfg.llm_provider}"
        return _env_label(ctx, shell, name)

    def shown(name: str, value: str) -> str:
        """A value is printed only when it did not come from an env variable."""
        if name in derived and derived[name] in ("llm_model", "llm_base_url"):
            env_name = f"SMARTMEMORY_{derived[name].upper()}"
            return f"${env_name}" if shell.get(env_name) else value
        if name in derived:
            return value  # a provider default
        return f"${name}"

    # Model, in call_llm's order: core config first, then get_default_model's.
    if leaf:
        _ATTRIBUTED.add(leaf)
        model_label, model_src = f"${leaf}", _env_label(ctx, shell, leaf)
    elif core_model and core_model == model:
        model_label = core_model
        model_src = f"extractor.llm.model_name in core config {core.config_path}"
    elif env.get("SMARTMEMORY_LLM_MODEL"):
        model_label = shown("SMARTMEMORY_LLM_MODEL", model)
        model_src = origin("SMARTMEMORY_LLM_MODEL")
    elif pin == "bedrock":
        model_label = "$BEDROCK_MODEL_ID" if env.get("BEDROCK_MODEL_ID") else "none"
        model_src = _env_label(ctx, shell, "BEDROCK_MODEL_ID")
    elif pin in pinned:
        model_label, model_src = model, f"default for LLM_PROVIDER {pin}"
    elif model == "sonnet" and agent:
        model_label, model_src = model, "Claude Agent SDK default"
    elif env.get("GROQ_API_KEY") and groq_model:
        model_label, model_src = model, "default when GROQ_API_KEY is set"
    elif env.get("GEMINI_API_KEY") and model.startswith("gemini"):
        model_label, model_src = model, "default when GEMINI_API_KEY is set"
    elif env.get("OPENAI_MODEL"):
        model_label, model_src = "$OPENAI_MODEL", _env_label(ctx, shell, "OPENAI_MODEL")
    else:
        model_label, model_src = model, "core default"
    sent = core.model or model
    if core.route and sent != model:  # alias expansion or a provider prefix strip
        model_label += (
            ", which core rewrites before sending"
            if model_label.startswith("$")
            else f", sent as {sent}"
        )

    # Endpoint: where core sent the request. Labels name the input that chose it.
    probe_url, from_base_url = "", False
    groq_url = "https://api.groq.com/openai/v1"
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    if core.route == "bedrock" or (not core.route and pin == "bedrock"):
        endpoint, endpoint_src = "AWS Bedrock", _env_label(ctx, shell, "LLM_PROVIDER")
    elif agent:
        endpoint, endpoint_src = "", "Claude Agent SDK, uses your Claude Code sign-in"
    else:
        api_base = core.api_base if core.route else None
        if not core.route:  # core refused: the route it takes once a key is set
            api_base = extractor_base or (
                None
                if base_url
                else pinned[pin][0]
                if pin in pinned
                else groq_url
                if groq_model
                else gemini_url
                if model.lower().startswith("gemini")
                else None
            )
        if api_base:
            endpoint = probe_url = api_base
            if api_base == extractor_base:
                endpoint_src = "the extractor's Groq route, GROQ_API_KEY is set"
            elif pin in pinned and api_base == pinned[pin][0]:
                endpoint_src = f"LLM_PROVIDER {pin}"
            elif api_base == groq_url:
                endpoint_src = "a Groq model name"
            elif api_base == gemini_url:
                endpoint_src = "a Gemini model name"
            else:
                endpoint_src = "core's extractor route"
        elif base_url:  # the OpenAI client reads OPENAI_BASE_URL itself
            probe_url, from_base_url = base_url, True
            endpoint = (
                _safe_url(base_url)
                if shown("OPENAI_BASE_URL", base_url) == base_url
                else shown("OPENAI_BASE_URL", base_url)
            )
            endpoint_src = origin("OPENAI_BASE_URL")
        else:
            endpoint = probe_url = "https://api.openai.com/v1"
            endpoint_src = "the OpenAI client default"

    # Provider: a core pin routes every call. Otherwise the CLI's llm_provider.
    app_provider = (cfg.llm_provider or "none").lower()
    if pin:
        provider, provider_src = pin, _env_label(ctx, shell, "LLM_PROVIDER")
        if app_provider not in ("none", pin):
            provider_src += f", which overrides llm_provider {app_provider}"
    else:
        provider = app_provider
        provider_src = (
            _env_label(ctx, shell, "SMARTMEMORY_LLM_PROVIDER")
            if shell.get("SMARTMEMORY_LLM_PROVIDER")
            else "config file"
            if cfg.llm_provider
            else "default"
        )
        if provider not in _LOCAL_OPENAI_PROVIDERS and provider not in (
            "none",
            "claude-agent",
            *_PROVIDER_KEYS,
        ):
            provider = _shown(ctx, "SMARTMEMORY_LLM_PROVIDER", provider)
    return LlmRoute(
        provider,
        provider_src,
        model_label,
        model_src,
        endpoint,
        endpoint_src,
        probe_url,
        pin,
        from_base_url,
        sent,
        tuple(core.unread),
        core.refusal,
    )


def check_llm(ctx: Context, context: str = "doctor") -> Row:
    from smartmemory_app.config import (
        LLM_KEY_ENV_VARS,
        NO_LOCAL_MODEL_OFF_LINE,
        _LOCAL_OPENAI_PROVIDERS,
        local_llm_without_model,
    )

    cfg = _config(ctx)
    if cfg.mode == "remote":
        return Row("LLM", PASS, "hosted: the SmartMemory service supplies the LLM")
    try:
        route = _llm_route(ctx, cfg)
    except Exception as exc:
        log.warning("doctor could not resolve the LLM route: %s", type(exc).__name__)
        return Row(
            "LLM",
            WARN,
            f"not checked: the LLM provider, model and endpoint could not be resolved ({type(exc).__name__})",
            "Run: sm report to send the details.",
        )
    where_keys: dict[str, list[str]] = {}
    unread_all: set[str] = set()
    for name in LLM_KEY_ENV_VARS:
        found, unread = _llm_key_sources(ctx, name)
        unread_all.update(unread)
        if found:
            where_keys[name] = found
    if ctx.environ.get("LLM_API_KEY"):  # core's key for a pinned provider
        where_keys["LLM_API_KEY"] = ["env"]
    unread_all.update(route.unread)  # a core config read core swallowed (R2-H3)
    keys_text = (
        ", ".join(
            f"{name} yes ({' + '.join(srcs)})" for name, srcs in where_keys.items()
        )
        or "no provider key found"
    )
    not_checked = (
        f" Not checked: {', '.join(sorted(unread_all))}." if unread_all else ""
    )
    ask_off = "sm ask" + (" in the tour" if context == "tour" else "")
    route_text = route.text()

    def row(status: str, detail: str, fix: str = "") -> Row:
        # A source that could not be read never leaves the row at PASS (H5).
        if unread_all:
            detail += not_checked
            if status == PASS:
                status = WARN
                fix = (
                    f"Make {', '.join(sorted(unread_all))} readable (unlock the keychain, "
                    "or fix the file), then run: sm doctor"
                )
        return Row("LLM", status, detail, fix)

    provider = route.provider
    app_provider = (cfg.llm_provider or "none").lower()
    # storage.apply_runtime_config holds core's route off while a local provider has
    # no model (CLI-SETUP-UX-1), whatever else is set: an OPENAI_BASE_URL, a provider
    # key, a pinned provider. So no branch below may answer PASS for it.
    held = local_llm_without_model(cfg)
    no_model = f"provider {app_provider} has no model chosen. {NO_LOCAL_MODEL_OFF_LINE}"
    no_model_fix = "Run: sm config llm_model <model name>, then sm restart"
    if held and not route.from_base_url:
        return row(WARN, no_model, no_model_fix)
    # An explicit base URL wins over a pin too (call_llm skips the pin then).
    if app_provider in _LOCAL_OPENAI_PROVIDERS and (
        not route.pin or route.from_base_url
    ):
        if not route.from_base_url:
            return row(
                WARN,
                f"{route_text}. llm_provider {app_provider} is set, but LLM calls do not go to it.",
                "Unset GROQ_API_KEY or SMARTMEMORY_LLM_MODEL if you meant the local server, then run: sm doctor",
            )
        try:
            status, _ = ctx.cached(
                f"llm_{route.probe_url}",
                lambda: ctx.http_get(
                    f"{route.probe_url.rstrip('/')}/models", timeout=2.0
                ),
            )
        except Exception as exc:
            if not _is_loopback(route.probe_url):
                # Cannot check: a remote endpoint may be unreachable only from here.
                return row(
                    WARN,
                    f"not checked: {route_text}, not answering ({type(exc).__name__})",
                    "Check your network and the endpoint, then run: sm doctor",
                )
            return row(
                FAIL,
                f"{route_text}, not answering ({type(exc).__name__})",
                f"Start {app_provider} at that endpoint, or run: sm config llm_provider none",
            )
        if status >= 400:
            return row(
                FAIL,
                f"{route_text}, answered HTTP {status}",
                f"Check {app_provider} is serving its OpenAI-compatible API, then run: sm doctor",
            )
        if held:
            # An explicit endpoint is probed (it still tells you the server is up),
            # but extraction stays off until a model is chosen.
            return row(
                WARN, f"{route_text}, server answering. {no_model}", no_model_fix
            )
        return row(PASS, f"{route_text}, server answering, no key needed")
    if not route.pin and app_provider == "claude-agent":
        return row(
            WARN,
            f"claude-agent (from {route.provider_src}) uses your Claude Code sign-in, which is not checked here. {route_text}.",
            "Run claude once to sign in, then: sm doctor",
        )
    if route.pin == "bedrock":
        return row(
            WARN,
            f"{route_text}. AWS credentials and the Bedrock model allowlist are not checked here.",
            "Check AWS_REGION, BEDROCK_ENABLED and BEDROCK_MODEL_ID, then run: sm doctor",
        )
    if route.pin:
        from smartmemory.utils.llm import _PINNED_PROVIDERS

        needed = _PINNED_PROVIDERS.get(route.pin, ("", ("OPENAI_API_KEY",), ""))[1]
        if not any(name in where_keys for name in needed):
            return row(
                FAIL,
                f"{route_text}, but none of {', '.join(needed)} is set, so {ask_off} and LLM extraction fail.",
                f"Set {needed[-1]}, or unset LLM_PROVIDER, then run: sm restart",
            )
    elif provider in _PROVIDER_KEYS:
        name = _PROVIDER_KEYS[provider]
        if name not in where_keys:
            return row(
                FAIL,
                f"{route_text}, but {name} is missing, so {ask_off} and LLM extraction fail.",
                f"Set {name}, then run: sm restart",
            )
    elif provider != "none":
        return row(
            WARN,
            f"{route_text}. That provider is not one doctor knows how to check. Keys: {keys_text}.",
            "Run: sm config llm_provider to see the supported values.",
        )
    elif not where_keys:
        return row(
            WARN,
            f"no LLM key ({', '.join(LLM_KEY_ENV_VARS)} all absent), so {ask_off} and LLM extraction are off.",
            "Set GROQ_API_KEY (or another provider key), run sm config llm_provider groq, then sm restart",
        )
    if not any(name in where_keys for name in LLM_KEY_ENV_VARS):
        # config.llm_key_present gates the daemon worker and Tier-2 enqueue on these names.
        return row(
            WARN,
            f"{route_text}. Keys: {keys_text}. LLM extraction stays off: the CLI starts it only when one of "
            f"{', '.join(LLM_KEY_ENV_VARS)} is set.",
            f"Set the provider's own key ({', '.join(LLM_KEY_ENV_VARS)}), then run: sm restart",
        )
    detail = f"{route_text}. Keys: {keys_text}."
    if context != "tour":
        try:
            info = ctx.cached("daemon", ctx.daemon_status)
        except Exception:
            info = None
        if isinstance(info, dict) and info.get("llm_key_present") is False:
            return row(
                WARN,
                f"{detail} The running daemon started without a key, so it is not using one.",
                "Run: sm restart",
            )
    env_only = all(srcs == ["env"] for srcs in where_keys.values())
    if route.refusal and env_only:
        return row(
            WARN,
            f"{detail} Core would not send the call with these settings ({route.refusal}).",
            "Set the key the message names, then run: sm restart",
        )
    return row(PASS, detail)


def _port_source(ctx: Context, cfg) -> str:
    if ctx.environ.get("SMARTMEMORY_DAEMON_PORT"):
        return _env_source(ctx, "SMARTMEMORY_DAEMON_PORT")
    try:
        if "daemon_port" in _file_settings(ctx).get("local", {}):
            return "config file"
    except Exception:
        pass
    return "default"


def _check_daemon(ctx: Context) -> Row:
    cfg = _config(ctx)
    port = f"port {cfg.daemon_port} (from {_port_source(ctx, cfg)})"
    try:
        info = ctx.cached("daemon", ctx.daemon_status)
    except Exception as exc:
        return Row(
            "Local daemon",
            WARN,
            f"not checked: health probe on {port} failed ({type(exc).__name__})",
            "Run: sm status",
        )
    if info is None:
        try:
            expected = ctx.daemon_expected()
        except Exception:
            expected = False
        if expected:
            return Row(
                "Local daemon",
                FAIL,
                f"should be running but is not answering on {port}",
                "Run: sm restart",
            )
        # Not expected to run: the CLI falls back to an in-process engine.
        return Row(
            "Local daemon",
            WARN,
            f"not running on {port}. Commands still work, but each one loads the models itself, so they are slower.",
            "Run: sm start",
        )
    status = info.get("status", "unknown")
    daemon_mode = info.get("mode")
    if daemon_mode == "remote":
        return Row(
            "Local daemon",
            FAIL,
            f"the daemon on {port} runs in remote mode but this shell's CLI uses local mode",
            "Run: sm restart (from this shell, so both use the same settings)",
        )
    if status == "degraded":
        reason = info.get("degraded_reason") or "no reason reported"
        return Row(
            "Local daemon",
            FAIL,
            f"degraded on {port}: {reason}",
            "Run: sm restart. If it stays degraded, run: sm report",
        )
    if status == "warming":
        return Row(
            "Local daemon",
            WARN,
            f"still loading models on {port}",
            "Wait a minute, then run: sm status",
        )
    if status != "ok":
        return Row(
            "Local daemon", WARN, f"status {status} on {port}", "Run: sm restart"
        )
    return Row("Local daemon", PASS, f"running on {port}, pid {info.get('pid', '?')}")


def _check_hosted_service(ctx: Context) -> Row:
    url = _shown(ctx, "SMARTMEMORY_API_URL", _safe_url(_api_url(ctx)))
    try:
        status, body = _ready(ctx)
    except Exception as exc:
        # Cannot check: no answer may mean this machine is offline, not that the service is down.
        return Row(
            "SmartMemory service",
            WARN,
            f"not checked: {url} unreachable ({type(exc).__name__})",
            "Check your network and SMARTMEMORY_API_URL, then run: sm doctor",
        )
    body = body if isinstance(body, dict) else {}
    version = body.get("version", "unknown")
    if status == 200 and body.get("status") == "ready":
        return Row("SmartMemory service", PASS, f"{url} ready, version {version}")
    failed = [c.get("name", "?") for c in body.get("checks", []) if not c.get("ok")]
    return Row(
        "SmartMemory service",
        FAIL,
        f"{url} not ready (HTTP {status}"
        + (f", failing: {', '.join(failed)}" if failed else "")
        + ")",
        "The hosted service is having trouble. Try again in a few minutes.",
    )


def check_service(ctx: Context) -> Row:
    if _mode(ctx) == "remote":
        return _check_hosted_service(ctx)
    return _check_daemon(ctx)


# SQLite errors that say this process could not read the store, not that the store
# is damaged: another process holds it, or the OS refused the open (permissions, a
# read-only volume, a directory in its place). Damage ("file is not a database",
# "malformed") is a DatabaseError that matches none of these and stays FAIL.
_SQLITE_IN_USE = ("locked", "busy")
_SQLITE_UNREADABLE = (
    "unable to open",
    "permission",
    "readonly",
    "read-only",
    "disk i/o",
)


def _sqlite_in_use(exc: sqlite3.Error) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        word in str(exc).lower() for word in _SQLITE_IN_USE
    )


def _sqlite_unreadable(exc: sqlite3.Error) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        word in str(exc).lower() for word in _SQLITE_UNREADABLE
    )


def _store_not_checked(
    db_label: str, where: str, exc: Exception, ignored: str = ""
) -> Row:
    """Cannot check: the OS would not let doctor read the store, which says nothing about its contents."""
    return Row(
        "Graph backend",
        WARN,
        f"not checked: SQLite store {db_label} (from {where}) cannot be read ({type(exc).__name__}){ignored}",
        f"Fix the permissions on {db_label}, then run: sm doctor",
    )


def check_graph(ctx: Context) -> Row:
    cfg = _config(ctx)
    if cfg.mode == "remote":
        try:
            status, body = _ready(ctx)
        except Exception as exc:
            return Row(
                "Graph backend",
                WARN,
                f"not checked: service unreachable ({type(exc).__name__})",
                "Fix the SmartMemory service row first.",
            )
        checks = {
            c.get("name"): c.get("ok")
            for c in (body or {}).get("checks", [])
            if isinstance(c, dict)
        }
        if "falkordb" not in checks:
            return Row(
                "Graph backend",
                WARN,
                f"not checked: the service did not report its graph store (HTTP {status})",
                "Run: sm status",
            )
        if checks["falkordb"]:
            return Row("Graph backend", PASS, "hosted graph store reachable")
        return Row(
            "Graph backend",
            FAIL,
            "the hosted graph store is down",
            "The hosted service is having trouble. Try again in a few minutes.",
        )
    from smartmemory_app.storage import _resolve_data_dir

    # The resolver every local sm command opens the store with (storage.py).
    with _patched_environ({**ctx.environ, "HOME": str(ctx.home)}):
        data_dir = _resolve_data_dir()
    raw_dir = ctx.environ.get("SMARTMEMORY_DATA_DIR")
    where = _env_source(ctx, "SMARTMEMORY_DATA_DIR") if raw_dir else "default"
    db = data_dir.expanduser() / "memory.db"
    db_label = "$SMARTMEMORY_DATA_DIR/memory.db" if raw_dir else str(db)
    ignored = ""
    try:
        cfg_dir = _file_settings(ctx).get("local", {}).get("data_dir")
    except Exception:
        cfg_dir = None
    if cfg_dir and Path(cfg_dir).expanduser() != data_dir.expanduser():
        ignored = (
            f" The config file data_dir ({cfg_dir}) is not used by sm commands run from"
            " this shell. Only the login auto-start daemon reads it."
        )
    try:
        db_exists = db.exists()
    except OSError as exc:
        return _store_not_checked(db_label, where, exc, ignored)
    if not db_exists:
        return Row(
            "Graph backend",
            WARN,
            f"SQLite store {db_label} (from {where}) does not exist yet. It is created on the first sm add.{ignored}",
            "Run: sm add 'hello' to create it",
        )
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if _sqlite_in_use(exc):
            # Cannot check: another process holds the store, which is normal while it runs.
            return Row(
                "Graph backend",
                WARN,
                f"not checked: SQLite store {db_label} (from {where}) is in use by another process ({type(exc).__name__})",
                "Run: sm stop, then sm doctor",
            )
        if _sqlite_unreadable(exc):
            return _store_not_checked(db_label, where, exc, ignored)
        return Row(
            "Graph backend",
            FAIL,
            f"SQLite store {db_label} (from {where}) cannot be opened ({type(exc).__name__})",
            "Run: sm stop, then sm doctor. If it persists, run: sm report",
        )
    status = WARN if ignored else PASS
    return Row(
        "Graph backend",
        status,
        f"SQLite store {db_label} (from {where}), {tables} schema objects.{ignored}",
        "Set SMARTMEMORY_DATA_DIR to use another store. The config data_dir is not read."
        if ignored
        else "",
    )


_CHECKS: dict[str, Callable[..., Row]] = {
    "python": check_python,
    "config": check_config,
    "mode": check_mode,
    "inherited": check_inherited,
    "credentials": check_credentials,
    "tenant": check_tenant,
    "llm": check_llm,
    "service": check_service,
    "graph": check_graph,
}
_TAKES_CONTEXT = {"mode", "llm"}
_TITLES = {
    "python": "Python",
    "config": "Config file",
    "mode": "Mode",
    "inherited": "Inherited SMARTMEMORY_* vars",
    "credentials": "API key",
    "tenant": "Tenant and workspace",
    "llm": "LLM",
    "service": "Daemon or service",
    "graph": "Graph backend",
}
# Rows that read the merged config cannot run when an env value makes it raise.
_NEEDS_CONFIG = {"credentials", "tenant", "llm", "service", "graph"}


def collect(ctx: Context, context: str = "doctor") -> list[Row]:
    """Run every row for ``context``. A crashing check becomes a WARN row."""
    rows = []
    blocked = _invalid_env(ctx)
    for key in _CONTEXT_ROWS[context]:
        if key in _NEEDS_CONFIG and blocked:
            rows.append(
                Row(
                    _TITLES[key],
                    WARN,
                    f"not checked: the configuration cannot load until {blocked[0]} is fixed",
                    "Fix the Mode row first.",
                )
            )
            continue
        check = _CHECKS[key]
        try:
            row = check(ctx, context) if key in _TAKES_CONTEXT else check(ctx)
        except Exception as exc:
            log.warning("doctor check %s could not run: %s", key, type(exc).__name__)
            row = Row(
                _TITLES[key],
                WARN,
                f"not checked: the check itself failed ({type(exc).__name__})",
                "Run: sm report to send the details.",
            )
        rows.append(row)
    return rows


def run_checks(context: str = "doctor") -> list[Row]:
    """The runtime entry point. Tests patch this to keep unrelated suites offline."""
    try:  # this project's ignore/override decisions first (CLI-ENV-DETECT-1)
        from smartmemory_app.env_pin import ensure_applied

        ensure_applied()
    except ImportError:
        pass
    return collect(Context(from_process=True), context)


def failures(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.status == FAIL]


def _masked(row: Row) -> Row:
    return Row(
        row.check, row.status, mask_env_values(row.detail), mask_env_values(row.fix)
    )


def render(rows: list[Row]) -> str:
    if not rows:
        return ""
    rows = [_masked(row) for row in rows]
    width = max(len(row.check) for row in rows)
    lines = ["What the CLI will use:"]
    for row in rows:
        lines.append(f"  {row.status.upper():<4}  {row.check:<{width}}  {row.detail}")
        if row.fix and row.status != PASS:
            lines.append(f"        {'':<{width}}  fix: {row.fix}")
    return "\n".join(lines)


def to_json(rows: list[Row]) -> list[dict]:
    return [asdict(_masked(row)) for row in rows]


def require_healthy(context: str) -> list[Row]:
    """Print the table; raise a ClickException naming the fix when a row FAILs."""
    import click

    with masked_logging():
        rows = run_checks(context)
    text = render(rows)
    if text:
        click.echo(text)
    failed = failures(rows)
    if failed:
        action = {"tour": "starting the tour", "setup": "using SmartMemory"}.get(
            context, "continuing"
        )
        raise click.ClickException(
            f"{len(failed)} check(s) failed. Fix the FAIL rows above before {action}, "
            "then run: sm doctor"
        )
    return rows


def after_setup(setup_fn):
    """Decorator for ``sm setup``: run doctor when setup returns normally."""

    @functools.wraps(setup_fn)
    def wrapper(*args, **kwargs):
        result = setup_fn(*args, **kwargs)
        post_setup_check()
        return result

    return wrapper


def post_setup_check() -> None:
    import click

    from smartmemory_app.config import is_configured

    try:
        configured = is_configured()
    except ValueError:
        configured = True  # an invalid env value: the Mode row explains it
    if not configured:
        return  # setup was cancelled before writing a config
    click.echo("\nChecking the result (sm doctor):")
    require_healthy("setup")


def doctor_json(rows: list[Row], extra: dict) -> str:
    return json.dumps({"ok": not failures(rows), "checks": to_json(rows), **extra})
