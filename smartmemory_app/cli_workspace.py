"""CLI-WORKSPACE-UX-1: ``sm workspace list | create | switch | current``.

One obvious way to see and change the workspace in both modes, and a check that the
CLI writes where the web console reads.

Local mode: a workspace is a tag on each memory (``metadata.workspace_id``). Hooks tag
by project folder (``derive_workspace_id``), plain ``sm add`` stores untagged unless a
workspace is pinned here. The pin and local names live in ``workspaces.toml``.

Remote mode: the workspace is the ``X-Workspace-Id`` the hosted API scopes every call
by (``SMARTMEMORY_TEAM_ID``, else config ``team_id``, else the account default). The
web console (smart-memory-web ``AuthContext.syncUserState``) opens
``/auth/me``.``default_team_id`` unless the browser saved a selection, and lists
``/memory/workspaces`` minus system teams. ``sm workspace current`` repeats that logic
with the CLI's key and says loudly when the two disagree.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional

import click

from smartmemory_app import scope_header
from smartmemory_app.config import load_config, save_config
from smartmemory_app.scope_header import (
    ENV_TEAM,
    ENV_WORKSPACE,
    Scope,
    load_registry,
    resolve_scope,
    save_registry,
)

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LOCAL_ID_RE = re.compile(r"ws_[0-9a-f]{12}")


def _echo_json(payload: Any) -> None:
    click.echo(json.dumps(payload, sort_keys=True))


def _print_scope(scope: Scope, api_url: Optional[str] = None) -> None:
    click.echo(f"Mode:       {scope.mode}")
    if api_url:
        click.echo(f"API:        {api_url}")
    tenant = scope.tenant
    if scope.tenant_note:
        tenant = f"{tenant} ({scope.tenant_note})"
    click.echo(f"Tenant:     {tenant}")
    click.echo(f"Workspace:  {scope.workspace_label}")


# ── Local helpers ──────────────────────────────────────────────────────────


def _local_counts() -> Optional[dict[str, int]]:
    """Memories per workspace tag in the local store, read-only. None when unreadable."""
    from smartmemory_app.storage import _resolve_data_dir

    try:
        path = (_resolve_data_dir() / "memory.db").expanduser().resolve()
        if not path.exists():
            return {}
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        ) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone():
                return {}
            rows = db.execute(
                "SELECT COALESCE(json_extract(properties, '$.workspace_id'), "
                "json_extract(properties, '$.metadata.workspace_id')) AS ws, COUNT(*) "
                "FROM nodes WHERE memory_type IS NULL OR memory_type != 'Version' "
                "GROUP BY ws"
            ).fetchall()
        return {(ws or ""): int(n) for ws, n in rows}
    except (OSError, sqlite3.Error) as exc:
        click.echo(f"WARNING: local memory counts unavailable: {exc}", err=True)
        return None


def _local_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    names = scope_header.local_names(registry)
    pinned = scope_header.local_pin(registry)
    env = os.environ.get(ENV_WORKSPACE)
    cwd = os.getcwd()
    folder = scope_header.folder_workspace(cwd)
    counts = _local_counts()
    by_id: dict[str, dict[str, Any]] = {}

    def row(ws_id: str) -> dict[str, Any]:
        return by_id.setdefault(
            ws_id,
            {
                "id": ws_id,
                "name": None,
                "memories": None if counts is None else counts.get(ws_id, 0),
                "pinned": ws_id == pinned,
                "env": ws_id == env,
                "folder": ws_id == folder,
            },
        )

    for name, ws_id in names.items():
        row(ws_id)["name"] = name
    if folder:
        row(folder)
    if env:
        row(env)
    if pinned:
        row(pinned)
    if counts:
        for ws_id in counts:
            if ws_id:
                row(ws_id)
    rows = sorted(by_id.values(), key=lambda r: (r["name"] or "~", r["id"]))
    untagged = None if counts is None else counts.get("", 0)
    rows.append(
        {
            "id": None,
            "name": "(untagged)",
            "memories": untagged,
            "pinned": False,
            "env": False,
            "folder": False,
        }
    )
    return rows


def _active_local_marker(r: dict[str, Any]) -> str:
    if r["env"]:
        return f"* {ENV_WORKSPACE}"
    if r["pinned"]:
        return "* pinned"
    if r["folder"]:
        return "  hooks here"
    return ""


# ── Remote helpers ─────────────────────────────────────────────────────────


def _request(cfg, method: str, path: str, **kwargs):
    from smartmemory_app.remote_cli import request_memory

    return request_memory(cfg, method, path, **kwargs)


def _remote_identity(cfg) -> dict[str, Any]:
    """The same three reads the web console does at sign-in, with the CLI's API key."""
    me = _request(cfg, "GET", "/auth/me")
    if not isinstance(me, dict) or not me.get("tenant_id"):
        raise click.ClickException(
            "The SmartMemory service did not return an account for this API key. "
            "Check the key with: sm status"
        )
    listing = _request(cfg, "GET", "/memory/workspaces")
    workspaces = listing.get("workspaces", []) if isinstance(listing, dict) else []
    try:
        teams = _request(cfg, "GET", "/memory/teams", params={"include_system": "true"})
    except click.ClickException as exc:
        click.echo(
            f"WARNING: team names unavailable, showing workspace names only: {exc}",
            err=True,
        )
        teams = []
    if not isinstance(teams, list):
        teams = []
    system_ids = {t.get("team_id") for t in teams if t.get("is_system")}
    team_names = {t.get("team_id"): t.get("name") for t in teams if t.get("team_id")}
    tenant_id = me["tenant_id"]
    selectable = [
        {
            "id": w["workspace_id"],
            "name": team_names.get(w["workspace_id"])
            or w.get("name")
            or w["workspace_id"],
            "permission": w.get("effective_permission"),
            "personal": bool(w.get("is_personal")),
        }
        for w in workspaces
        if w.get("workspace_id")
        and w.get("tenant_id") == tenant_id
        and w.get("effective_permission") is not None
        and w["workspace_id"] not in system_ids
    ]
    identity = {
        "email": me.get("email") or "",
        "tenant_id": tenant_id,
        "default_team_id": me.get("default_team_id") or "",
        "active_workspace_id": me.get("active_workspace_id") or "",
        "workspaces": selectable,
    }
    _cache_identity(cfg, identity)
    return identity


def _cache_identity(cfg, identity: dict[str, Any]) -> None:
    from smartmemory_app.config import get_api_key

    key = get_api_key()
    registry = load_registry()
    registry["remote"] = {
        "api_url": cfg.api_url.rstrip("/"),
        "key_fingerprint": scope_header.key_fingerprint(key) if key else "",
        "email": identity["email"],
        "tenant_id": identity["tenant_id"],
        "default_team_id": identity["default_team_id"],
        "active_workspace_id": identity["active_workspace_id"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "names": {w["id"]: w["name"] for w in identity["workspaces"]},
    }
    save_registry(registry)


def _console_check(cfg, identity: dict[str, Any]) -> dict[str, Any]:
    """Compare the CLI's effective workspace with what the console opens for this account."""
    cli_id, source = scope_header.resolve_remote_workspace(cfg, load_registry())
    effective = cli_id or identity["default_team_id"]
    console_id = identity["default_team_id"]
    names = {w["id"]: w["name"] for w in identity["workspaces"]}
    selectable = effective in names
    match = effective == console_id
    return {
        "cli_workspace_id": effective,
        "cli_workspace_name": names.get(effective),
        "cli_workspace_source": source,
        "console_workspace_id": console_id,
        "console_workspace_name": names.get(console_id),
        "match": match,
        "selectable_in_console": selectable,
    }


def _print_console_check(check: dict[str, Any]) -> None:
    def label(ws_id: Optional[str], name: Optional[str]) -> str:
        if not ws_id:
            return "none"
        return f"{name} ({ws_id})" if name and name != ws_id else ws_id

    console = label(check["console_workspace_id"], check["console_workspace_name"])
    cli = label(check["cli_workspace_id"], check["cli_workspace_name"])
    click.echo(f"Web console: opens {console} by default for this account.")
    if check["match"]:
        click.echo(
            "MATCH: this CLI and the web console use the same tenant and workspace."
        )
    else:
        click.echo(f"MISMATCH: this CLI uses {cli}, the web console opens {console}.")
        click.echo(
            "MISMATCH: memories saved here will not appear in the console's default view."
        )
        click.echo(
            f"  Fix: sm workspace switch {check['console_workspace_id']}   "
            f"(or pick {cli} in the console's workspace selector)"
        )
    if not check["selectable_in_console"]:
        click.echo(
            f"WARNING: workspace {cli} is not selectable in the web console for this account."
        )
    click.echo(
        "Note: the console remembers a workspace picked in that browser. "
        "This check sees the account default, not that browser choice."
    )


def _find_remote(identity: dict[str, Any], target: str) -> Optional[dict[str, Any]]:
    for w in identity["workspaces"]:
        if w["id"] == target:
            return w
    lowered = target.lower()
    hits = [w for w in identity["workspaces"] if (w["name"] or "").lower() == lowered]
    if len(hits) > 1:
        raise click.ClickException(
            f"Several workspaces are named {target!r}. Switch by id: "
            + ", ".join(w["id"] for w in hits)
        )
    return hits[0] if hits else None


def _warn_env_wins(var: str, value: Optional[str], new_id: str) -> None:
    if value and value != new_id:
        click.echo(
            f"WARNING: {var} is set in this shell and wins over the saved choice. "
            f"Unset it or export {var}={new_id} to use the new workspace.",
            err=True,
        )


# ── Commands ───────────────────────────────────────────────────────────────


@click.group("workspace")
def workspace_group() -> None:
    """List, create and switch the workspace that sm reads and writes."""


@workspace_group.command("current")
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
@click.option(
    "--check",
    is_flag=True,
    help="Remote mode: exit 1 when this CLI and the web console disagree.",
)
def current_cmd(as_json: bool, check: bool) -> None:
    """Show the active mode, tenant and workspace, and whether it matches the web console."""
    cfg = load_config()
    if cfg.mode == "remote":
        identity = _remote_identity(cfg)
        verdict = _console_check(cfg, identity)
        scope = resolve_scope(cfg)
        if as_json:
            _echo_json(
                {
                    "mode": "remote",
                    "api_url": cfg.api_url,
                    "email": identity["email"],
                    "tenant_id": identity["tenant_id"],
                    "workspace_id": verdict["cli_workspace_id"],
                    "workspace_name": verdict["cli_workspace_name"],
                    "workspace_source": scope.workspace_source,
                    "console": verdict,
                }
            )
        else:
            _print_scope(scope, api_url=cfg.api_url)
            _print_console_check(verdict)
        if check and not (verdict["match"] and verdict["selectable_in_console"]):
            raise SystemExit(1)
        return

    registry = load_registry()
    cwd = os.getcwd()
    scope = resolve_scope(cfg, command="workspace")
    folder = scope_header.folder_workspace(cwd)
    env = os.environ.get(ENV_WORKSPACE)
    pinned = scope_header.local_pin(registry)
    names = {v: k for k, v in scope_header.local_names(registry).items()}
    if as_json:
        _echo_json(
            {
                "mode": scope.mode,
                "tenant_id": None,
                "workspace_id": scope.workspace_id,
                "workspace_name": scope.workspace_name,
                "workspace_source": scope.workspace_source,
                "pinned": pinned,
                "env": env,
                "folder_workspace_id": folder,
                "cwd": cwd,
            }
        )
        return
    _print_scope(scope)
    if env:
        click.echo(
            f"  {ENV_WORKSPACE} is set in this shell and wins over the pin and the folder."
        )
    elif pinned:
        click.echo("  sm add and sm recall use the pinned workspace.")
    else:
        click.echo(
            "  Nothing is pinned: sm add stores untagged memories (visible from every workspace)."
        )
        click.echo("  Pin one with: sm workspace switch <name>")
    if folder:
        suffix = f" ({names[folder]})" if folder in names else ""
        root, kind = scope_header.folder_root(cwd)
        click.echo(
            f"  Hooks here tag memories with {folder}{suffix} (auto from {kind} {root})."
        )
    click.echo("Web console: not applicable, local memories stay on this machine.")


@workspace_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
def list_cmd(as_json: bool) -> None:
    """List workspaces. Remote: the ones the web console can select. Local: names and tags in the store."""
    cfg = load_config()
    if cfg.mode == "remote":
        identity = _remote_identity(cfg)
        verdict = _console_check(cfg, identity)
        rows = [
            {
                **w,
                "active": w["id"] == verdict["cli_workspace_id"],
                "console_default": w["id"] == identity["default_team_id"],
            }
            for w in identity["workspaces"]
        ]
        if as_json:
            _echo_json(
                {
                    "mode": "remote",
                    "tenant_id": identity["tenant_id"],
                    "workspaces": rows,
                }
            )
            return
        click.echo(f"Account: {identity['email']} (tenant {identity['tenant_id']})")
        if not rows:
            click.echo("No selectable workspaces for this account.")
        for r in rows:
            marks = []
            if r["active"]:
                marks.append("* active")
            if r["console_default"]:
                marks.append("console default")
            if r["personal"]:
                marks.append("personal")
            click.echo(
                f"  {r['name']:<24} {r['id']:<36} {r['permission'] or '-':<6} {', '.join(marks)}"
            )
        if not verdict["match"] or not verdict["selectable_in_console"]:
            click.echo(
                "MISMATCH: this CLI does not use the console's default workspace. Run: sm workspace current"
            )
        return

    registry = load_registry()
    rows = _local_rows(registry)
    if as_json:
        _echo_json({"mode": cfg.mode or "unconfigured", "workspaces": rows})
        return
    click.echo("Local workspaces (tags in the store on this machine):")
    for r in rows:
        count = "?" if r["memories"] is None else r["memories"]
        click.echo(
            f"  {(r['name'] or '-'):<24} {(r['id'] or '-'):<16} {count:>6} memories  {_active_local_marker(r)}"
        )
    if not scope_header.local_pin(registry) and not os.environ.get(ENV_WORKSPACE):
        click.echo("Nothing pinned. Pin one with: sm workspace switch <name>")


@workspace_group.command("create")
@click.argument("name")
@click.option("--description", default=None, help="Remote only: workspace description.")
@click.option(
    "--team",
    "team_id",
    default=None,
    help="Remote only: team that gets edit access (default: your default team).",
)
@click.option("--switch", "do_switch", is_flag=True, help="Switch to it right away.")
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
def create_cmd(
    name: str,
    description: Optional[str],
    team_id: Optional[str],
    do_switch: bool,
    as_json: bool,
) -> None:
    """Create a workspace. Remote: on the hosted service. Local: a named tag for sm add and recall."""
    if not _NAME_RE.fullmatch(name):
        raise click.ClickException(
            "Workspace names use letters, digits, dots, dashes and underscores (max 64)."
        )
    cfg = load_config()
    if cfg.mode == "remote":
        identity = _remote_identity(cfg)
        if _find_remote(identity, name):
            raise click.ClickException(
                f"A workspace named {name!r} already exists. Switch with: sm workspace switch {name}"
            )
        body: dict[str, Any] = {
            "name": name,
            "team_id": team_id or identity["default_team_id"],
        }
        if description:
            body["description"] = description
        if not body["team_id"]:
            raise click.ClickException(
                "No default team on this account. Pass --team <team_id>."
            )
        created = _request(cfg, "POST", "/memory/workspaces", json=body)
        if not isinstance(created, dict) or not created.get("workspace_id"):
            raise click.ClickException(
                "The SmartMemory service returned an invalid workspace response."
            )
        ws_id = created["workspace_id"]
        _remote_identity(cfg)  # refresh the cached names
        if do_switch:
            _switch_remote(cfg, ws_id)
        if as_json:
            _echo_json(
                {"mode": "remote", "id": ws_id, "name": name, "switched": do_switch}
            )
            return
        click.echo(f"Created workspace {name} ({ws_id}) on {cfg.api_url}.")
        if not do_switch:
            click.echo(f"Switch to it with: sm workspace switch {name}")
        return

    registry = load_registry()
    names = scope_header.local_names(registry)
    if name in names:
        raise click.ClickException(
            f"A local workspace named {name!r} already exists ({names[name]})."
        )
    ws_id = "ws_" + uuid.uuid4().hex[:12]
    registry.setdefault("local", {}).setdefault("names", {})[name] = ws_id
    save_registry(registry)
    if do_switch:
        _switch_local(ws_id, name)
    if as_json:
        _echo_json({"mode": "local", "id": ws_id, "name": name, "switched": do_switch})
        return
    click.echo(f"Created local workspace {name} ({ws_id}).")
    if not do_switch:
        click.echo(f"Switch to it with: sm workspace switch {name}")


def _switch_local(ws_id: Optional[str], label: str) -> None:
    registry = load_registry()
    local = registry.setdefault("local", {})
    if ws_id is None:
        local.pop("pinned", None)
    else:
        local["pinned"] = ws_id
    save_registry(registry)
    _warn_env_wins(ENV_WORKSPACE, os.environ.get(ENV_WORKSPACE), ws_id or "")


SESSION_CLIENT_KEY = "app:cli"


def _switch_remote(cfg, ws_id: str) -> None:
    cfg.team_id = ws_id
    save_config(cfg)
    _warn_env_wins(ENV_TEAM, os.environ.get(ENV_TEAM), ws_id)
    _save_session_selection(cfg, ws_id)


def _save_session_selection(cfg, ws_id: str) -> None:
    """WS-ROUTING-4: also save the pick server-side, so a header-less write from this client lands there too.

    The local config stays the CLI's own source of truth, so a refusal or an older service warns and the switch
    still succeeds. No mismatch preflight: the switch itself just changed the workspace on purpose.
    """
    from smartmemory_app.remote_cli import _request_memory

    try:
        _request_memory(
            cfg,
            "PUT",
            "/memory/routing/session",
            json={"workspace_id": ws_id, "client_key": SESSION_CLIENT_KEY},
        )
    except Exception as exc:  # the local switch already happened; say what was lost
        click.echo(
            f"WARNING: server-side selection not saved ({exc}). "
            "The CLI still uses the new workspace from its config.",
            err=True,
        )


@workspace_group.command("switch")
@click.argument("target")
@click.option(
    "--force", is_flag=True, help="Remote only: accept an id that is not in the list."
)
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
def switch_cmd(target: str, force: bool, as_json: bool) -> None:
    """Switch to a workspace by name or id. Local: 'auto' unpins and goes back to folder tagging."""
    cfg = load_config()
    if cfg.mode == "remote":
        identity = _remote_identity(cfg)
        found = _find_remote(identity, target)
        if found is None:
            if not force:
                raise click.ClickException(
                    f"No workspace named or numbered {target!r} for this account. "
                    "See: sm workspace list   (or pass --force to use the id anyway)"
                )
            found = {"id": target, "name": target}
        _switch_remote(cfg, found["id"])
        verdict = _console_check(cfg, identity)
        if as_json:
            _echo_json(
                {
                    "mode": "remote",
                    "id": found["id"],
                    "name": found["name"],
                    "console": verdict,
                }
            )
            return
        click.echo(f"Switched to workspace {found['name']} ({found['id']}).")
        _print_console_check(verdict)
        return

    registry = load_registry()
    names = scope_header.local_names(registry)
    if target == "auto":
        _switch_local(None, "auto")
        if as_json:
            _echo_json({"mode": "local", "id": None, "name": "auto"})
            return
        click.echo(
            "Unpinned. sm add stores untagged memories again, hooks tag by project folder."
        )
        return
    if target in names:
        ws_id, label = names[target], target
    elif _LOCAL_ID_RE.fullmatch(target):
        ws_id = target
        label = next((n for n, i in names.items() if i == target), target)
    else:
        raise click.ClickException(
            f"No local workspace named {target!r}. See: sm workspace list   "
            f"or create it with: sm workspace create {target}"
        )
    _switch_local(ws_id, label)
    if as_json:
        _echo_json({"mode": "local", "id": ws_id, "name": label})
        return
    click.echo(
        f"Switched to local workspace {label} ({ws_id}). sm add and sm recall use it now."
    )
