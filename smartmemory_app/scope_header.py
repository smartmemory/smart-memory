"""CLI-WORKSPACE-UX-1: one answer to "which mode, tenant and workspace will this command touch?"

The daemon decides a local item's workspace in ``local_api.ingest_endpoint`` and
``local_api.recall``: explicit ``workspace_id`` first, then ``derive_workspace_id(cwd)``
(which honours ``SMARTMEMORY_WORKSPACE_ID``), then none (an untagged item). This module
mirrors that rule, adds the workspace pinned by ``sm workspace switch`` between the env
var and the folder, and prints a one-line header on stderr so the scope is never silent.

Remote mode sends ``X-Workspace-Id`` from ``SMARTMEMORY_TEAM_ID`` or the config
``team_id``; the hosted service picks the account's default workspace when that is
empty. The tenant is only known from ``/auth/me``, so the header reads the identity
cached by the last ``sm workspace`` command instead of making a network call.

State lives in ``workspaces.toml`` next to ``config.toml``: the local pin, local
workspace names, and the cached remote identity (never the API key, only a fingerprint).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import click
import tomli_w

from smartmemory_app import config as _config

log = logging.getLogger(__name__)

ENV_WORKSPACE = "SMARTMEMORY_WORKSPACE_ID"
ENV_TEAM = "SMARTMEMORY_TEAM_ID"
ENV_QUIET = "SMARTMEMORY_QUIET"

# Machine-invoked or self-describing commands never get the header.
SKIP_COMMANDS = frozenset({"lifecycle", "server", "worker", "workspace"})
# The only local daemon paths with workspace semantics (local_api.py).
LOCAL_SCOPED_PATHS = frozenset({"/memory/ingest", "/memory/recall"})

# Per-invocation state lives on the click root context, so one process prints one
# header and test runners that invoke the CLI many times in one process stay isolated.
_PRINTED = "smartmemory.scope_header.printed"
_QUIET = "smartmemory.scope_header.quiet"
_no_context_state: dict[str, bool] = {}


def _invocation_state() -> dict:
    try:
        return click.get_current_context().find_root().meta
    except RuntimeError:
        return _no_context_state


# ── Registry file ──────────────────────────────────────────────────────────


def workspaces_path() -> Path:
    """``workspaces.toml`` beside the config file (same XDG rules, same test patches)."""
    return _config.config_path().parent / "workspaces.toml"


def load_registry() -> dict[str, Any]:
    """Parsed ``workspaces.toml``. Missing or unreadable means empty, with a WARNING."""
    path = workspaces_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning(
            "Workspace registry %s is unreadable, treating it as empty: %s", path, exc
        )
        return {}


def save_registry(data: dict[str, Any]) -> None:
    path = workspaces_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(_drop_none(data)), encoding="utf-8")


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


def key_fingerprint(api_key: str) -> str:
    """Short digest that ties a cached identity to one API key without storing it."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def cached_remote_identity(cfg, registry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The identity cached by the last ``sm workspace`` command, if it is for this key and API."""
    remote = registry.get("remote") or {}
    if not remote.get("tenant_id"):
        return None
    if remote.get("api_url") != cfg.api_url.rstrip("/"):
        return None
    try:
        key = _config.get_api_key()
    except Exception:
        key = ""
    if not key or remote.get("key_fingerprint") != key_fingerprint(key):
        return None
    return remote


# ── Resolution ─────────────────────────────────────────────────────────────


@dataclass
class Scope:
    mode: str  # "local" | "remote" | "unconfigured"
    tenant: str  # tenant id, or a plain-words placeholder
    tenant_note: str  # "", an email, or why the tenant is unknown
    workspace_id: Optional[str]
    workspace_name: Optional[str]
    workspace_source: str  # plain words: where the workspace came from

    @property
    def workspace_label(self) -> str:
        if self.workspace_id is None:
            return self.workspace_source
        if self.workspace_name and self.workspace_name != self.workspace_id:
            return (
                f"{self.workspace_name} ({self.workspace_id}, {self.workspace_source})"
            )
        return f"{self.workspace_id} ({self.workspace_source})"


def _request_scope_inputs(
    path: Optional[str], kwargs: Optional[dict]
) -> tuple[Optional[str], Optional[str]]:
    """(explicit workspace_id, cwd) as the daemon will see them for this request."""
    kwargs = kwargs or {}
    if path == "/memory/ingest":
        body = kwargs.get("json") or {}
        return body.get("workspace_id") or None, body.get("cwd") or None
    if path == "/memory/recall":
        params = kwargs.get("params") or {}
        return params.get("workspace_id") or None, params.get("cwd") or None
    return None, None


def local_pin(registry: Optional[dict[str, Any]] = None) -> Optional[str]:
    registry = load_registry() if registry is None else registry
    return (registry.get("local") or {}).get("pinned") or None


def local_names(registry: Optional[dict[str, Any]] = None) -> dict[str, str]:
    registry = load_registry() if registry is None else registry
    names = (registry.get("local") or {}).get("names") or {}
    return {str(k): str(v) for k, v in names.items()}


def folder_root(cwd: str) -> tuple[str, str]:
    """(path the hash is taken from, "git root" or "folder") for a working directory."""
    import subprocess

    canonical = os.path.realpath(cwd)
    try:
        result = subprocess.run(
            ["git", "-C", canonical, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1,
            check=True,
        )
        top = result.stdout.strip()
        if top:
            return os.path.realpath(top), "git root"
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        pass
    return canonical, "folder"


def folder_workspace(cwd: Optional[str]) -> Optional[str]:
    """What hooks in ``cwd`` use: the git root hash (env var excluded, that is reported separately).

    A plain folder is a normal case here, so the derivation's own "lost git-root"
    WARNING is held back for this one call. ``folder_root`` already said which it is.
    """
    if not cwd:
        return None
    from smartmemory_app import recall_format

    saved = os.environ.pop(ENV_WORKSPACE, None)
    derive_log = logging.getLogger(recall_format.__name__)
    level = derive_log.level
    derive_log.setLevel(logging.ERROR)
    try:
        return recall_format.derive_workspace_id(folder_root(cwd)[0])
    finally:
        derive_log.setLevel(level)
        if saved is not None:
            os.environ[ENV_WORKSPACE] = saved


def local_override(
    registry: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], str]:
    """(workspace_id, source) the CLI itself chose: the env var first, then the pin.

    The one place both the header and the request sender read. The daemon is a
    separate process, so whatever the header names here must also be sent.
    """
    env = os.environ.get(ENV_WORKSPACE)
    if env:
        return env, f"{ENV_WORKSPACE} env var"
    pinned = local_pin(registry)
    if pinned:
        return pinned, "pinned by sm workspace switch"
    return None, ""


def resolve_local_workspace(
    path: Optional[str],
    kwargs: Optional[dict],
    registry: dict[str, Any],
    command: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """(workspace_id, source) for a local request, in the daemon's own precedence."""
    if path is not None and path not in LOCAL_SCOPED_PATHS:
        label = f"sm {command}" if command else "this command"
        return None, f"all ({label} is not workspace scoped locally)"
    explicit, cwd = _request_scope_inputs(path, kwargs)
    if explicit:
        return explicit, "explicit --workspace"
    chosen, source = local_override(registry)
    if chosen:
        return chosen, source
    if cwd:
        root, kind = folder_root(cwd)
        return folder_workspace(cwd), f"auto from {kind} {root}"
    return None, "untagged (visible from every workspace)"


def resolve_remote_workspace(
    cfg, registry: dict[str, Any]
) -> tuple[Optional[str], str]:
    env = os.environ.get(ENV_TEAM)
    if env:
        return env, f"{ENV_TEAM} env var"
    if cfg.team_id:
        return cfg.team_id, "config team_id"
    cached = cached_remote_identity(cfg, registry)
    if cached and cached.get("default_team_id"):
        return cached["default_team_id"], "account default, chosen by the service"
    return None, "account default, chosen by the service (run: sm workspace current)"


def resolve_scope(
    cfg=None,
    *,
    path: Optional[str] = None,
    kwargs: Optional[dict] = None,
    command: Optional[str] = None,
) -> Scope:
    cfg = _config.load_config() if cfg is None else cfg
    registry = load_registry()
    if cfg.mode == "remote":
        ws_id, source = resolve_remote_workspace(cfg, registry)
        cached = cached_remote_identity(cfg, registry)
        names = (registry.get("remote") or {}).get("names") or {}
        if cached:
            tenant, note = cached["tenant_id"], cached.get("email") or ""
        else:
            tenant, note = "not checked", "run: sm workspace current"
        return Scope(
            mode="remote",
            tenant=tenant,
            tenant_note=note,
            workspace_id=ws_id,
            workspace_name=names.get(ws_id) if ws_id else None,
            workspace_source=source,
        )
    ws_id, source = resolve_local_workspace(path, kwargs, registry, command)
    names = {v: k for k, v in local_names(registry).items()}
    return Scope(
        mode=cfg.mode or "unconfigured",
        tenant="none",
        tenant_note="local store",
        workspace_id=ws_id,
        workspace_name=names.get(ws_id) if ws_id else None,
        workspace_source=source,
    )


# ── Pin injection ──────────────────────────────────────────────────────────


def apply_local_pin(cfg, path: str, kwargs: dict) -> dict:
    """Send the workspace the header names, since the daemon cannot read the CLI's env or pin.

    Only local mode and the two scoped paths. An explicit value in the request wins
    (that is what the header reports). Otherwise the CLI's own choice from
    ``local_override`` (env var first, then the pin) goes out as the explicit
    ``workspace_id``. The body or params dict is updated in place, so the caller's
    own copy carries it too and its daemon-down fallback stores the same workspace.
    """
    if cfg.mode == "remote" or path not in LOCAL_SCOPED_PATHS:
        return kwargs
    chosen, _ = local_override()
    if not chosen:
        return kwargs
    container = "json" if path == "/memory/ingest" else "params"
    payload = kwargs.get(container)
    if not isinstance(payload, dict):
        payload = {}
        kwargs = {**kwargs, container: payload}
    if not payload.get("workspace_id"):
        payload["workspace_id"] = chosen
    return kwargs


def fallback_properties(body: dict, props: Optional[dict]) -> Optional[dict]:
    """Properties for the in-process ingest fallback, stamped like the daemon does.

    local_api stamps ``properties["workspace_id"]`` from the request body, and the
    stamp wins over a caller property. The daemon-down path must do the same, or the
    header says "pinned" while the item lands untagged or elsewhere.
    """
    merged = dict(props or {})
    workspace_id = (body or {}).get("workspace_id")
    if workspace_id:
        merged["workspace_id"] = workspace_id
    return merged or None


# ── Header ─────────────────────────────────────────────────────────────────


def set_quiet(ctx=None, param=None, value: bool = False) -> None:
    """Click callback for the group-level ``--quiet`` flag."""
    if value:
        (ctx.find_root().meta if ctx is not None else _invocation_state())[_QUIET] = (
            True
        )


def _current_command() -> tuple[Optional[str], dict]:
    """(top-level sm subcommand, merged params of the whole context chain)."""
    try:
        ctx = click.get_current_context()
    except RuntimeError:
        return None, {}
    params: dict = {}
    chain = []
    while ctx is not None:
        chain.append(ctx)
        params.update({k: v for k, v in ctx.params.items() if v})
        ctx = ctx.parent
    # chain[-1] is the root group, chain[-2] the top-level subcommand.
    top = chain[-2].info_name if len(chain) >= 2 else None
    return top, params


def header_suppressed() -> bool:
    if _invocation_state().get(_QUIET) or os.environ.get(ENV_QUIET) == "1":
        return True
    top, params = _current_command()
    if top in SKIP_COMMANDS:
        return True
    return bool(params.get("as_json") or params.get("quiet"))


def format_header(scope: Scope) -> str:
    tenant = scope.tenant
    if scope.tenant_note:
        tenant = f"{tenant} ({scope.tenant_note})"
    return (
        f"[smartmemory] mode={scope.mode}  tenant={tenant}  "
        f"workspace={scope.workspace_label}"
    )


def print_scope_header(
    cfg=None, *, path: Optional[str] = None, kwargs: Optional[dict] = None
) -> None:
    """Print the scope line once per invocation on stderr. Never raises."""
    state = _invocation_state()
    if state.get(_PRINTED):
        return
    try:
        if header_suppressed():
            return
        top, _ = _current_command()
        scope = resolve_scope(cfg, path=path, kwargs=kwargs, command=top)
        state[_PRINTED] = True
        click.echo(format_header(scope), err=True)
    except Exception as exc:  # the header must never break a command
        log.warning("Scope header unavailable: %s", exc)
