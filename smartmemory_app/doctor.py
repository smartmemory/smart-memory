"""SETUP-DOCTOR-1: one table that says what the CLI will actually use.

Every row names the setting the CLI resolved, the source that won (flag, env var,
config file, keychain, default) and a one-line fix when it is not PASS. A row
that cannot be checked is WARN naming what was not checked, never silent.

Precedence mirrored here (verified against the code, see the unit report):
  mode           SMARTMEMORY_MODE > config [smartmemory].mode > unconfigured
                 (storage.get_memory then writes a local config on first use)
  API URL        SMARTMEMORY_API_URL > config [remote].api_url > default
  API key        SMARTMEMORY_API_KEY > OS keychain > Windows credential file
  workspace      SMARTMEMORY_TEAM_ID > config [remote].team_id > hosted default
  data dir       SMARTMEMORY_DATA_DIR > ~/.smartmemory (config data_dir is not read)
  daemon port    SMARTMEMORY_DAEMON_PORT > config [local].daemon_port > 9014
  LLM provider   SMARTMEMORY_LLM_PROVIDER > config [local].llm_provider > none
  LLM keys       env > OS keychain > shell profile export (daemon start only)

Secrets are never printed: rows show key NAMES and yes/no only. The
inherited-variable row shows variable names and where they are exported, never
their values. Non-secret effective settings (mode, URL host, workspace id, data
dir, port) are printed because they are what this command exists to show.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

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
    from smartmemory_app.setup import _read_env_from_profile

    return bool(_read_env_from_profile(name))


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


def check_config(ctx: Context) -> Row:
    path = ctx.config_path()
    if ctx.platform == "win32":
        moved_by = "APPDATA" if ctx.environ.get("APPDATA") else None
    else:
        moved_by = "XDG_CONFIG_HOME" if ctx.environ.get("XDG_CONFIG_HOME") else None
    where = f"{path} (location set by env {moved_by})" if moved_by else str(path)
    if not path.exists():
        return Row(
            "Config file",
            WARN,
            f"none at {where}. The first sm command will create a local-mode config.",
            "Run: sm setup",
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return Row(
            "Config file",
            FAIL,
            f"{where} cannot be read ({type(exc).__name__})",
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


def _workspace_source(ctx: Context, cfg) -> tuple[str, str]:
    """(workspace id or '', where it came from) for the hosted X-Workspace-Id."""
    if ctx.environ.get("SMARTMEMORY_TEAM_ID"):
        return cfg.team_id, _env_source(ctx, "SMARTMEMORY_TEAM_ID")
    if cfg.team_id:
        return cfg.team_id, "config file team_id"
    return "", "account default"


def check_tenant(ctx: Context) -> Row:
    cfg = _config(ctx)
    if cfg.mode != "remote":
        recall_ws = ctx.environ.get("SMARTMEMORY_WORKSPACE_ID")
        if recall_ws and _env_decision(ctx, "SMARTMEMORY_WORKSPACE_ID") not in (
            "use",
            "override",
        ):
            return Row(
                "Tenant and workspace",
                WARN,
                "local store, no tenant. Recall workspace $SMARTMEMORY_WORKSPACE_ID comes from "
                f"{_env_source(ctx, 'SMARTMEMORY_WORKSPACE_ID')}, not from this folder.",
                "Unset SMARTMEMORY_WORKSPACE_ID to scope recall by the current project.",
            )
        return Row(
            "Tenant and workspace",
            PASS,
            "local store, no tenant. Recall workspace comes from the current project folder.",
        )
    workspace, where = _workspace_source(ctx, cfg)
    ws_label = _shown(ctx, "SMARTMEMORY_TEAM_ID", workspace) if workspace else ""
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
            f"so the account behind the CLI is unknown. Workspace: {ws_label or 'account default'} (from {where}).",
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
            f"Workspace: {ws_label or 'account default'} (from {where}).",
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
            f"Workspace: {ws_label or 'account default'} (from {where}).",
            "Run sm doctor again. If it persists, run: sm report",
        )
    account = body.get("email") or body.get("id") or "unknown account"
    tenant = body.get("tenant_id") or "unknown"
    console_ws = body.get("active_workspace_id") or body.get("default_workspace_id")
    effective = ws_label or console_ws or "unknown"
    detail = f"account {account}, tenant {tenant}, workspace {effective} (from {where})"
    if workspace and console_ws and workspace != console_ws:
        return Row(
            "Tenant and workspace",
            WARN,
            f"{detail}. Your account's default workspace, which the web console opens, is {console_ws}.",
            "To match the console, unset SMARTMEMORY_TEAM_ID or remove team_id from "
            f"{ctx.config_path()}.",
        )
    return Row(
        "Tenant and workspace",
        PASS,
        f"{detail}. Compare with the workspace shown in the web console.",
    )


def _llm_key_sources(ctx: Context, name: str) -> tuple[list[str], list[str]]:
    """(sources holding the key, sources that could not be read). Never values."""
    found, unread = [], []
    if ctx.environ.get(name):
        found.append("env")
    try:
        if ctx.keyring_has("smartmemory", name):
            found.append("OS keychain")
    except CheckUnavailable:
        unread.append("OS keychain")
    try:
        if ctx.profile_has(name):
            found.append("shell profile")
    except Exception:
        unread.append("shell profile")
    return found, unread


def check_llm(ctx: Context, context: str = "doctor") -> Row:
    from smartmemory_app.config import LLM_KEY_ENV_VARS

    cfg = _config(ctx)
    if cfg.mode == "remote":
        return Row("LLM", PASS, "hosted: the SmartMemory service supplies the LLM")
    provider = (cfg.llm_provider or "none").lower()
    prov_src = (
        _env_source(ctx, "SMARTMEMORY_LLM_PROVIDER")
        if ctx.environ.get("SMARTMEMORY_LLM_PROVIDER")
        else "config file"
    )
    where_keys = {}
    unread_all: set[str] = set()
    for name in LLM_KEY_ENV_VARS:
        found, unread = _llm_key_sources(ctx, name)
        unread_all.update(unread)
        if found:
            where_keys[name] = found
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

    from smartmemory_app.config import _LOCAL_OPENAI_PROVIDERS

    if provider in _LOCAL_OPENAI_PROVIDERS:
        base = (cfg.llm_base_url or _LOCAL_OPENAI_PROVIDERS[provider]).rstrip("/")
        base_label = _shown(ctx, "SMARTMEMORY_LLM_BASE_URL", _safe_url(base))
        try:
            status, _ = ctx.cached(
                f"llm_{provider}", lambda: ctx.http_get(f"{base}/models", timeout=2.0)
            )
        except Exception as exc:
            return Row(
                "LLM",
                FAIL,
                f"{provider} (from {prov_src}) is not answering at {base_label} ({type(exc).__name__})",
                f"Start {provider}, or run: sm config llm_provider none",
            )
        if status >= 400:
            return Row(
                "LLM",
                FAIL,
                f"{provider} (from {prov_src}) answered HTTP {status} at {base_label}",
                f"Check {provider} is serving its OpenAI-compatible API, then run: sm doctor",
            )
        return Row(
            "LLM",
            PASS,
            f"{provider} (from {prov_src}), server answering, no key needed",
        )
    if provider == "claude-agent":
        return Row(
            "LLM",
            WARN,
            f"claude-agent (from {prov_src}) uses your Claude Code sign-in, which is not checked here",
            "Run claude once to sign in, then: sm doctor",
        )
    if provider in _PROVIDER_KEYS:
        name = _PROVIDER_KEYS[provider]
        if name not in where_keys:
            return Row(
                "LLM",
                FAIL,
                f"{provider} (from {prov_src}) but {name} is missing, so {ask_off} and LLM extraction fail.{not_checked}",
                f"Set {name}, then run: sm restart",
            )
    elif provider != "none":
        return Row(
            "LLM",
            WARN,
            f"provider {_shown(ctx, 'SMARTMEMORY_LLM_PROVIDER', provider)} (from {prov_src}) is not one doctor knows how to check. Keys: {keys_text}.",
            "Run: sm config llm_provider to see the supported values.",
        )
    elif not where_keys:
        return Row(
            "LLM",
            WARN,
            f"no LLM key ({', '.join(LLM_KEY_ENV_VARS)} all absent), so {ask_off} and LLM extraction are off.{not_checked}",
            "Set GROQ_API_KEY (or another provider key), run sm config llm_provider groq, then sm restart",
        )
    detail = f"provider {provider} (from {prov_src}). Keys: {keys_text}.{not_checked}"
    if context != "tour":
        try:
            info = ctx.cached("daemon", ctx.daemon_status)
        except Exception:
            info = None
        if isinstance(info, dict) and info.get("llm_key_present") is False:
            return Row(
                "LLM",
                WARN,
                f"{detail} The running daemon started without a key, so it is not using one.",
                "Run: sm restart",
            )
    return Row("LLM", PASS, detail)


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
        return Row(
            "SmartMemory service",
            FAIL,
            f"{url} unreachable ({type(exc).__name__})",
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
    raw_dir = ctx.environ.get("SMARTMEMORY_DATA_DIR")
    data_dir = Path(raw_dir) if raw_dir else ctx.home / ".smartmemory"
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
    if not db.exists():
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


def render(rows: list[Row]) -> str:
    if not rows:
        return ""
    width = max(len(row.check) for row in rows)
    lines = ["What the CLI will use:"]
    for row in rows:
        lines.append(f"  {row.status.upper():<4}  {row.check:<{width}}  {row.detail}")
        if row.fix and row.status != PASS:
            lines.append(f"        {'':<{width}}  fix: {row.fix}")
    return "\n".join(lines)


def to_json(rows: list[Row]) -> list[dict]:
    return [asdict(row) for row in rows]


def require_healthy(context: str) -> list[Row]:
    """Print the table; raise a ClickException naming the fix when a row FAILs."""
    import click

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
