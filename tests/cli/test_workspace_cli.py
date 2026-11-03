"""CLI-WORKSPACE-UX-1: ``sm workspace`` in both modes, the scope header on and off, pin injection.

One row per subcommand x mode, plus one row per header rule. Every row runs the real
Click commands with a tmp config dir. Remote rows answer through an httpx mock transport
with the same three reads the web console makes (``/auth/me``, ``/memory/workspaces``,
``/memory/teams``).
"""

from __future__ import annotations

import inspect
import json
import tomllib

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, scope_header, storage


TENANT = "tenant_demo"
DEFAULT_TEAM = "team_personal"
OTHER_WS = "ws_other000000000000000000000000"
ME = {
    "email": "demo@example.com",
    "tenant_id": TENANT,
    "default_team_id": DEFAULT_TEAM,
    "active_workspace_id": DEFAULT_TEAM,
}
WORKSPACES = [
    {
        "workspace_id": DEFAULT_TEAM,
        "tenant_id": TENANT,
        "name": "personal",
        "is_personal": True,
        "effective_permission": "admin",
    },
    {
        "workspace_id": OTHER_WS,
        "tenant_id": TENANT,
        "name": "Demo",
        "is_personal": False,
        "effective_permission": "edit",
    },
    {
        "workspace_id": "team_system",
        "tenant_id": TENANT,
        "name": "system",
        "is_personal": False,
        "effective_permission": "admin",
    },
    {
        "workspace_id": "ws_no_access",
        "tenant_id": TENANT,
        "name": "hidden",
        "is_personal": False,
        "effective_permission": None,
    },
]
TEAMS = [
    {
        "team_id": DEFAULT_TEAM,
        "name": "Personal",
        "is_system": False,
        "is_default": True,
    },
    {
        "team_id": "team_system",
        "name": "System",
        "is_system": True,
        "is_default": False,
    },
]


def runner() -> CliRunner:
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def invoke(args, **kwargs):
    return runner().invoke(cli_module.cli, args, catch_exceptions=False, **kwargs)


def stderr_of(result) -> str:
    try:
        return result.stderr
    except ValueError:
        return ""


def stdout_of(result) -> str:
    """Only stdout: click 8.2+ interleaves stderr into ``result.output``."""
    return getattr(result, "stdout", result.output)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Tmp config dir, tmp data dir, no inherited SMARTMEMORY_* workspace or team vars."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setattr(config, "config_path", lambda: cfg_dir / "config.toml")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    for var in (
        "SMARTMEMORY_WORKSPACE_ID",
        "SMARTMEMORY_TEAM_ID",
        "SMARTMEMORY_QUIET",
        "SMARTMEMORY_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(storage, "_remote_memory", None)
    yield tmp_path


@pytest.fixture
def local(isolated, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    calls = []

    def daemon(method, path, timeout=120, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/memory/recall":
            return {"context": "recalled"}
        if path == "/memory/ingest":
            return {"item_id": "item_1"}
        if path == "/memory/search":
            return {"items": []}
        return {}

    monkeypatch.setattr(cli_module, "_daemon_request", daemon)
    return calls


@pytest.fixture
def remote(isolated, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-remote.invalid/api")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_remote_key")
    monkeypatch.setattr(
        cli_module,
        "_daemon_request",
        lambda *a, **k: pytest.fail("local daemon touched in remote mode"),
    )
    calls = []
    state = {"me": dict(ME), "workspaces": [dict(w) for w in WORKSPACES]}

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test_remote_key"
        path = request.url.path
        if path.endswith("/auth/me"):
            return httpx.Response(200, json=state["me"])
        if path.endswith("/memory/workspaces") and request.method == "GET":
            return httpx.Response(200, json={"workspaces": state["workspaces"]})
        if path.endswith("/memory/workspaces") and request.method == "POST":
            body = json.loads(request.content)
            new = {
                "workspace_id": "ws_created00000000000000000000000",
                "tenant_id": TENANT,
                "name": body["name"],
                "is_personal": False,
                "effective_permission": "admin",
            }
            state["workspaces"].append(new)
            return httpx.Response(201, json=new)
        if path.endswith("/memory/teams"):
            return httpx.Response(200, json=TEAMS)
        if path.endswith("/memory/ingest"):
            return httpx.Response(200, json={"item_id": "remote_item"})
        if path.endswith("/memory/routing/session") and request.method == "PUT":
            # WS-ROUTING-4: svc-api's session lever; `session_status` makes it refuse.
            status = state.get("session_status", 200)
            body = json.loads(request.content)
            if status != 200:
                return httpx.Response(
                    status, json={"detail": "insufficient_team_permissions"}
                )
            return httpx.Response(200, json={**body, "set_via": "cli_switch"})
        return httpx.Response(404, json={"detail": f"unexpected {path}"})

    transport = httpx.MockTransport(handle)
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx._api, "Client", lambda **kw: original_client(transport=transport, **kw)
    )
    return calls, state


def saved_team_id() -> str:
    return tomllib.loads(config.config_path().read_text())["remote"]["team_id"]


def registry() -> dict:
    return scope_header.load_registry()


# ── Subcommand x mode ──────────────────────────────────────────────────────


LOCAL_ROWS = [
    {
        "id": "local-current-unpinned",
        "args": ["workspace", "current"],
        "exit": 0,
        "out": ["Mode:       local", "Tenant:     none", "untagged", "Hooks here tag"],
    },
    {
        "id": "local-list-empty-store",
        "args": ["workspace", "list"],
        "exit": 0,
        "out": ["Local workspaces", "(untagged)", "hooks here", "Nothing pinned"],
    },
    {
        "id": "local-create",
        "args": ["workspace", "create", "demo"],
        "exit": 0,
        "out": ["Created local workspace demo (ws_", "sm workspace switch demo"],
        "registry": lambda r: set(r["local"]["names"]) == {"demo"}
        and "pinned" not in r["local"],
    },
    {
        "id": "local-create-switch",
        "args": ["workspace", "create", "demo", "--switch"],
        "exit": 0,
        "out": ["Created local workspace demo"],
        "registry": lambda r: r["local"]["pinned"] == r["local"]["names"]["demo"],
    },
    {
        "id": "local-create-bad-name",
        "args": ["workspace", "create", "no spaces"],
        "exit": 1,
        "out": ["letters, digits, dots, dashes and underscores"],
    },
    {
        "id": "local-switch-by-name",
        "pre": [["workspace", "create", "demo"]],
        "args": ["workspace", "switch", "demo"],
        "exit": 0,
        "out": [
            "Switched to local workspace demo (ws_",
            "sm add and sm recall use it now",
        ],
        "registry": lambda r: r["local"]["pinned"] == r["local"]["names"]["demo"],
    },
    {
        "id": "local-switch-auto",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["workspace", "switch", "auto"],
        "exit": 0,
        "out": ["Unpinned"],
        "registry": lambda r: "pinned" not in r["local"],
    },
    {
        "id": "local-switch-unknown",
        "args": ["workspace", "switch", "nope"],
        "exit": 1,
        "out": ["No local workspace named 'nope'", "sm workspace create nope"],
    },
    {
        "id": "local-current-pinned-json",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["workspace", "current", "--json"],
        "exit": 0,
        "json": lambda d: d["workspace_name"] == "demo"
        and d["workspace_id"] == d["pinned"]
        and d["workspace_source"] == "pinned by sm workspace switch"
        and d["tenant_id"] is None,
    },
    {
        "id": "local-current-env-wins-over-pin",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "env": {"SMARTMEMORY_WORKSPACE_ID": "ws_fromenv000000"},
        "args": ["workspace", "current"],
        "exit": 0,
        "out": [
            "ws_fromenv000000 (SMARTMEMORY_WORKSPACE_ID env var)",
            "wins over the pin",
        ],
    },
]


@pytest.mark.parametrize("row", LOCAL_ROWS, ids=[r["id"] for r in LOCAL_ROWS])
def test_workspace_local(local, monkeypatch, row):
    for pre in row.get("pre", []):
        assert invoke(pre).exit_code == 0
    for k, v in row.get("env", {}).items():
        monkeypatch.setenv(k, v)
    result = invoke(row["args"])
    assert result.exit_code == row["exit"], result.output + stderr_of(result)
    for needle in row.get("out", []):
        assert needle in result.output, (needle, result.output)
    if "json" in row:
        assert row["json"](json.loads(result.output)), result.output
    if "registry" in row:
        assert row["registry"](registry()), registry()
    assert not local, "sm workspace must not touch the daemon"


REMOTE_ROWS = [
    {
        "id": "remote-current-match-default",
        "args": ["workspace", "current"],
        "exit": 0,
        "out": [
            "Mode:       remote",
            f"Tenant:     {TENANT} (demo@example.com)",
            f"Personal ({DEFAULT_TEAM}, config team_id)",
            "MATCH: this CLI and the web console use the same tenant and workspace",
        ],
    },
    {
        "id": "remote-current-mismatch-loud",
        "team": OTHER_WS,
        "args": ["workspace", "current"],
        "exit": 0,
        "out": [
            f"MISMATCH: this CLI uses Demo ({OTHER_WS}), the web console opens Personal ({DEFAULT_TEAM})",
            "will not appear in the console's default view",
            f"Fix: sm workspace switch {DEFAULT_TEAM}",
        ],
    },
    {
        "id": "remote-current-check-exits-1-on-mismatch",
        "team": OTHER_WS,
        "args": ["workspace", "current", "--check"],
        "exit": 1,
        "out": ["MISMATCH"],
    },
    {
        "id": "remote-current-check-exits-0-on-match",
        "args": ["workspace", "current", "--check"],
        "exit": 0,
        "out": ["MATCH"],
    },
    {
        "id": "remote-current-not-selectable",
        "team": "ws_stranger00000",
        "args": ["workspace", "current"],
        "exit": 0,
        "out": [
            "WARNING: workspace ws_stranger00000 is not selectable in the web console"
        ],
    },
    {
        "id": "remote-current-json",
        "args": ["workspace", "current", "--json"],
        "exit": 0,
        "json": lambda d: d["tenant_id"] == TENANT
        and d["workspace_id"] == DEFAULT_TEAM
        and d["console"]["match"] is True
        and d["console"]["console_workspace_id"] == DEFAULT_TEAM,
    },
    {
        "id": "remote-list-console-selectable-only",
        "args": ["workspace", "list"],
        "exit": 0,
        "out": [
            "Account: demo@example.com",
            "Personal",
            "* active, console default, personal",
            "Demo",
        ],
        "not_out": ["system", "hidden", "MISMATCH"],
    },
    {
        "id": "remote-list-flags-mismatch",
        "team": OTHER_WS,
        "args": ["workspace", "list"],
        "exit": 0,
        "out": ["MISMATCH: this CLI does not use the console's default workspace"],
    },
    {
        "id": "remote-create",
        "args": ["workspace", "create", "Launch"],
        "exit": 0,
        "out": ["Created workspace Launch (ws_created", "sm workspace switch Launch"],
        "posted": {"name": "Launch", "team_id": DEFAULT_TEAM},
        "team_after": DEFAULT_TEAM,
        "session_puts": [],
    },
    {
        "id": "remote-create-switch-custom-team",
        "args": ["workspace", "create", "Launch", "--team", "team_x", "--switch"],
        "exit": 0,
        "out": ["Created workspace Launch"],
        "posted": {"name": "Launch", "team_id": "team_x"},
        "team_after": "ws_created00000000000000000000000",
        "session_puts": [
            {
                "workspace_id": "ws_created00000000000000000000000",
                "client_key": "app:cli",
            }
        ],
    },
    {
        "id": "remote-create-duplicate-name",
        "args": ["workspace", "create", "Demo"],
        "exit": 1,
        "out": ["already exists", "sm workspace switch Demo"],
    },
    {
        "id": "remote-switch-by-name-case-insensitive",
        "args": ["workspace", "switch", "demo"],
        "exit": 0,
        "out": [f"Switched to workspace Demo ({OTHER_WS})", "MISMATCH"],
        "team_after": OTHER_WS,
        "session_puts": [{"workspace_id": OTHER_WS, "client_key": "app:cli"}],
    },
    {
        "id": "remote-switch-by-id",
        "team": OTHER_WS,
        "args": ["workspace", "switch", DEFAULT_TEAM],
        "exit": 0,
        "out": ["Switched to workspace Personal", "MATCH"],
        "team_after": DEFAULT_TEAM,
        "session_puts": [{"workspace_id": DEFAULT_TEAM, "client_key": "app:cli"}],
    },
    {
        "id": "remote-switch-unknown",
        "args": ["workspace", "switch", "nope"],
        "exit": 1,
        "out": ["No workspace named or numbered 'nope'", "--force"],
        "team_after": DEFAULT_TEAM,
        "session_puts": [],
    },
    {
        "id": "remote-switch-env-var-wins-warning",
        "env": {"SMARTMEMORY_TEAM_ID": DEFAULT_TEAM},
        "args": ["workspace", "switch", "Demo"],
        "exit": 0,
        "err": ["SMARTMEMORY_TEAM_ID is set in this shell and wins"],
        "team_after": OTHER_WS,
    },
    # WS-ROUTING-4: the server refuses or predates the session lever. The local switch still happens, with a WARNING.
    {
        "id": "remote-switch-session-refused-warns",
        "session_status": 403,
        "args": ["workspace", "switch", "Demo"],
        "exit": 0,
        "out": [f"Switched to workspace Demo ({OTHER_WS})"],
        "err": [
            "WARNING: server-side selection not saved",
            "HTTP 403",
            "still uses the new workspace",
        ],
        "team_after": OTHER_WS,
        "session_puts": [{"workspace_id": OTHER_WS, "client_key": "app:cli"}],
    },
    {
        "id": "remote-switch-older-service-warns",
        "session_status": 404,
        "args": ["workspace", "switch", "Demo"],
        "exit": 0,
        "err": ["WARNING: server-side selection not saved", "HTTP 404"],
        "team_after": OTHER_WS,
    },
]


@pytest.mark.parametrize("row", REMOTE_ROWS, ids=[r["id"] for r in REMOTE_ROWS])
def test_workspace_remote(remote, monkeypatch, row):
    calls, state = remote
    if "session_status" in row:
        state["session_status"] = row["session_status"]
    cfg = config.load_config()
    cfg.team_id = row.get("team", DEFAULT_TEAM)
    config.save_config(cfg)
    for k, v in row.get("env", {}).items():
        monkeypatch.setenv(k, v)
    result = invoke(row["args"])
    assert result.exit_code == row["exit"], result.output + stderr_of(result)
    for needle in row.get("out", []):
        assert needle in result.output, (needle, result.output)
    for needle in row.get("not_out", []):
        assert needle not in result.output, (needle, result.output)
    for needle in row.get("err", []):
        assert needle in stderr_of(result), (needle, stderr_of(result))
    if "json" in row:
        assert row["json"](json.loads(result.output)), result.output
    if "posted" in row:
        posts = [c for c in calls if c.method == "POST"]
        assert json.loads(posts[-1].content) == row["posted"]
    if "team_after" in row:
        assert saved_team_id() == row["team_after"]
    if "session_puts" in row:
        puts = [
            c
            for c in calls
            if c.method == "PUT" and c.url.path == "/api/memory/routing/session"
        ]
        assert [json.loads(c.content) for c in puts] == row["session_puts"]
    paths = {c.url.path for c in calls}
    assert {"/api/auth/me", "/api/memory/workspaces", "/api/memory/teams"} <= paths


def test_remote_identity_is_cached_without_the_key(remote):
    assert invoke(["workspace", "current"]).exit_code == 0
    cached = registry()["remote"]
    assert cached["tenant_id"] == TENANT and cached["email"] == "demo@example.com"
    assert cached["names"] == {DEFAULT_TEAM: "Personal", OTHER_WS: "Demo"}
    assert (
        "test_remote_key"
        not in config.config_path().parent.joinpath("workspaces.toml").read_text()
    )
    assert cached["key_fingerprint"] == scope_header.key_fingerprint("test_remote_key")


# ── Header on and off ──────────────────────────────────────────────────────


HEADER = "[smartmemory] mode="
HEADER_ROWS = [
    {
        "id": "local-recall-header-on",
        "mode": "local",
        "args": ["recall", "--top-k", "1"],
        "header": "[smartmemory] mode=local  tenant=none (local store)  workspace=untagged (visible from every workspace)",
    },
    {
        "id": "local-recall-cwd-header-names-folder",
        "mode": "local",
        "args": ["recall", "--cwd", ".", "--top-k", "1"],
        "header": "workspace=ws_",
        "header_also": ["auto from folder "],
    },
    {
        "id": "local-add-header-pinned-name",
        "mode": "local",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["add", "hello"],
        "header": "workspace=demo (ws_",
        "header_also": ["pinned by sm workspace switch"],
    },
    {
        "id": "local-search-header-not-scoped",
        "mode": "local",
        "args": ["search", "hello"],
        "header": "workspace=all (sm search is not workspace scoped locally)",
    },
    {
        "id": "local-search-json-header-off",
        "mode": "local",
        "args": ["search", "hello", "--json"],
        "header": None,
    },
    {
        "id": "local-quiet-flag-header-off",
        "mode": "local",
        "args": ["--quiet", "recall", "--top-k", "1"],
        "header": None,
    },
    {
        "id": "local-quiet-env-header-off",
        "mode": "local",
        "env": {"SMARTMEMORY_QUIET": "1"},
        "args": ["recall", "--top-k", "1"],
        "header": None,
    },
    {
        "id": "remote-add-header-cached-identity",
        "mode": "remote",
        "pre": [["workspace", "current"]],
        "args": ["add", "hello"],
        "header": f"[smartmemory] mode=remote  tenant={TENANT} (demo@example.com)  workspace=Personal ({DEFAULT_TEAM}, config team_id)",
    },
    {
        "id": "remote-add-header-identity-not-checked",
        "mode": "remote",
        "args": ["add", "hello"],
        "header": f"tenant=not checked (run: sm workspace current)  workspace={DEFAULT_TEAM} (config team_id)",
    },
    {
        "id": "remote-add-header-env-team",
        "mode": "remote",
        "env": {"SMARTMEMORY_TEAM_ID": OTHER_WS},
        "args": ["add", "hello"],
        "header": f"workspace={OTHER_WS} (SMARTMEMORY_TEAM_ID env var)",
    },
]


@pytest.fixture
def either_mode(request, isolated, monkeypatch):
    mode = request.param
    if mode == "local":
        monkeypatch.setenv("SMARTMEMORY_MODE", "local")
        calls = []

        def daemon(method, path, timeout=120, **kwargs):
            calls.append((method, path, kwargs))
            return {
                "/memory/recall": {"context": "recalled"},
                "/memory/ingest": {"item_id": "i"},
            }.get(path, {"items": []})

        monkeypatch.setattr(cli_module, "_daemon_request", daemon)
        return calls
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test-remote.invalid/api")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_remote_key")
    cfg = config.load_config()
    cfg.team_id = DEFAULT_TEAM
    config.save_config(cfg)

    def handle(request):
        path = request.url.path
        if path.endswith("/auth/me"):
            return httpx.Response(200, json=ME)
        if path.endswith("/memory/workspaces"):
            return httpx.Response(200, json={"workspaces": WORKSPACES})
        if path.endswith("/memory/teams"):
            return httpx.Response(200, json=TEAMS)
        if path.endswith("/memory/ingest"):
            return httpx.Response(200, json={"item_id": "remote_item"})
        return httpx.Response(404, json={"detail": path})

    transport = httpx.MockTransport(handle)
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx._api, "Client", lambda **kw: original_client(transport=transport, **kw)
    )
    return []


@pytest.mark.parametrize(
    "either_mode,row",
    [(r["mode"], r) for r in HEADER_ROWS],
    ids=[r["id"] for r in HEADER_ROWS],
    indirect=["either_mode"],
)
def test_scope_header(either_mode, monkeypatch, row):
    for pre in row.get("pre", []):
        assert invoke(pre).exit_code == 0
    for k, v in row.get("env", {}).items():
        monkeypatch.setenv(k, v)
    result = invoke(row["args"])
    assert result.exit_code == 0, result.output + stderr_of(result)
    err = stderr_of(result)
    assert HEADER not in stdout_of(result), "the header never goes to stdout"
    if row["header"] is None:
        assert HEADER not in err, err
        return
    assert err.count(HEADER) == 1, err
    assert row["header"] in err, (row["header"], err)
    for needle in row.get("header_also", []):
        assert needle in err, (needle, err)


def test_header_skips_machine_invoked_lifecycle(local, monkeypatch):
    monkeypatch.setattr(
        cli_module, "_lifecycle_request", lambda path, body: {"context": ""}
    )
    result = invoke(["lifecycle", "orient", "--cwd", "."])
    assert HEADER not in stderr_of(result), stderr_of(result)


# ── Pin injection ──────────────────────────────────────────────────────────


PIN_ROWS = [
    {
        "id": "recall-gets-pin",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["recall", "--top-k", "1"],
        "path": "/memory/recall",
        "field": ("params", "workspace_id"),
        "expect": "pin",
    },
    {
        "id": "ingest-gets-pin",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["add", "hello"],
        "path": "/memory/ingest",
        "field": ("json", "workspace_id"),
        "expect": "pin",
    },
    {
        "id": "explicit-workspace-wins-over-pin",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["recall", "--workspace", "ws_explicit00000", "--top-k", "1"],
        "path": "/memory/recall",
        "field": ("params", "workspace_id"),
        "expect": "ws_explicit00000",
    },
    {
        # CLI-WORKSPACE-UX-1 fix 1 (F-1): the env var beats the pin AND is sent, because the
        # daemon is another process and never sees the CLI's environment.
        "id": "env-var-beats-pin-and-is-sent",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "env": {"SMARTMEMORY_WORKSPACE_ID": "ws_fromenv000000"},
        "args": ["recall", "--top-k", "1"],
        "path": "/memory/recall",
        "field": ("params", "workspace_id"),
        "expect": "ws_fromenv000000",
    },
    {
        "id": "unpinned-sends-nothing",
        "args": ["add", "hello"],
        "path": "/memory/ingest",
        "field": ("json", "workspace_id"),
        "expect": None,
    },
    {
        "id": "search-untouched",
        "pre": [["workspace", "create", "demo", "--switch"]],
        "args": ["search", "hello"],
        "path": "/memory/search",
        "field": ("json", "workspace_id"),
        "expect": None,
    },
]


FALLBACK_ROWS = [
    {
        "id": "add-fallback-honors-pin",
        "args": ["add", "hello"],
        "store_fn": "ingest",
        "expect": "pin",
    },
    {
        "id": "recall-fallback-honors-pin",
        "args": ["recall", "--top-k", "1"],
        "store_fn": "recall",
        "expect": "pin",
    },
    {
        "id": "recall-fallback-explicit-wins",
        "args": ["recall", "--workspace", "ws_explicit00000", "--top-k", "1"],
        "store_fn": "recall",
        "expect": "ws_explicit00000",
    },
]


@pytest.mark.parametrize("row", FALLBACK_ROWS, ids=[r["id"] for r in FALLBACK_ROWS])
def test_daemon_down_fallback_honors_pin(isolated, monkeypatch, row):
    """Daemon down: the in-process fallback stamps the same workspace the header names."""
    import smartmemory_app.storage as storage

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    assert invoke(["workspace", "create", "demo", "--switch"]).exit_code == 0
    monkeypatch.setattr(cli_module, "_daemon_request", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "_prepare_direct_access", lambda *a, **k: None)
    seen = {}

    def fake_ingest(content, memory_type, properties=None, origin=None, **kw):
        seen["workspace_id"] = (properties or {}).get("workspace_id")
        return "item_fallback"

    def fake_recall(cwd, top_k, query=None, workspace_id=None, **kw):
        seen["workspace_id"] = workspace_id
        return "recalled"

    monkeypatch.setattr(storage, "ingest", fake_ingest)
    monkeypatch.setattr(storage, "recall", fake_recall)
    result = invoke(row["args"])
    assert result.exit_code == 0, result.output + stderr_of(result)
    expected = row["expect"]
    if expected == "pin":
        expected = registry()["local"]["pinned"]
    assert seen.get("workspace_id") == expected, (seen, expected)
    # The header names the same workspace the fallback stored or read.
    assert expected in stderr_of(result)


@pytest.mark.parametrize("row", PIN_ROWS, ids=[r["id"] for r in PIN_ROWS])
def test_local_pin_injection(local, monkeypatch, row):
    for pre in row.get("pre", []):
        assert invoke(pre).exit_code == 0
    for k, v in row.get("env", {}).items():
        monkeypatch.setenv(k, v)
    result = invoke(row["args"])
    assert result.exit_code == 0, result.output + stderr_of(result)
    call = next(c for c in local if c[1] == row["path"])
    container, key = row["field"]
    sent = (call[2].get(container) or {}).get(key)
    expected = row["expect"]
    if expected == "pin":
        expected = registry()["local"]["pinned"]
    assert sent == expected, (sent, expected)


# ── The header and the request name the same workspace (F-1, F-3) ───────────

ENV_WS = "ws_fromenv000000"
EXPLICIT_WS = "ws_explicit00000"

# command x arm. The arms are the ones the resolver has for a local request: nothing,
# the env var, the pin, env plus pin (env wins), and recall's --workspace (add has no
# explicit flag). Each combination exists in `resolve_local_workspace`.
SAME_WS_ARMS = [
    ("none", {}, False, None),
    ("env", {"SMARTMEMORY_WORKSPACE_ID": ENV_WS}, False, ENV_WS),
    ("pin", {}, True, "pin"),
    ("env+pin", {"SMARTMEMORY_WORKSPACE_ID": ENV_WS}, True, ENV_WS),
]
SAME_WS_COMMANDS = {
    "add": (["add", "hello"], "ingest", "/memory/ingest", ("json", "workspace_id")),
    "recall": (
        ["recall", "--top-k", "1"],
        "recall",
        "/memory/recall",
        ("params", "workspace_id"),
    ),
}
SAME_WS_ROWS = [
    {"id": f"{cmd}-{where}-{arm[0]}", "cmd": cmd, "where": where, "arm": arm}
    for cmd in SAME_WS_COMMANDS
    for where in ("daemon", "fallback")
    for arm in SAME_WS_ARMS
] + [
    {
        "id": f"recall-{where}-explicit",
        "cmd": "recall",
        "where": where,
        "arm": ("explicit", {}, True, EXPLICIT_WS),
        "extra": ["--workspace", EXPLICIT_WS],
    }
    for where in ("daemon", "fallback")
]


def _header_ids(err: str) -> set[str]:
    import re

    line = next(ln for ln in err.splitlines() if ln.startswith(HEADER))
    return set(re.findall(r"ws_[0-9a-z]+", line))


@pytest.mark.parametrize("row", SAME_WS_ROWS, ids=[r["id"] for r in SAME_WS_ROWS])
def test_header_workspace_equals_request_workspace(isolated, monkeypatch, row):
    """Whatever workspace the header names is what the request carries, on both paths.

    daemon: the body or query the daemon receives. fallback: the daemon is down, so
    the in-process store call gets it. An untagged header means nothing is sent.
    """
    arm, env, pinned, expected = row["arm"]
    args, store_fn, path, (container, key) = SAME_WS_COMMANDS[row["cmd"]]
    args = args + row.get("extra", [])
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    if pinned:
        assert invoke(["workspace", "create", "demo", "--switch"]).exit_code == 0
    if expected == "pin":
        expected = registry()["local"]["pinned"]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    seen: dict = {}
    if row["where"] == "daemon":

        def daemon(method, p, timeout=120, **kwargs):
            if p == path:
                seen["sent"] = (kwargs.get(container) or {}).get(key) or None
            return (
                {"context": "recalled"} if p == "/memory/recall" else {"item_id": "i"}
            )

        monkeypatch.setattr(cli_module, "_daemon_request", daemon)
    else:
        monkeypatch.setattr(cli_module, "_daemon_request", lambda *a, **k: None)
        monkeypatch.setattr(cli_module, "_prepare_direct_access", lambda *a, **k: None)

        def fake_ingest(content, memory_type, properties=None, origin=None, **kw):
            seen["sent"] = (properties or {}).get("workspace_id")
            return "item_fallback"

        def fake_recall(cwd, top_k, query=None, workspace_id=None, **kw):
            seen["sent"] = workspace_id
            return "recalled"

        monkeypatch.setattr(storage, "ingest", fake_ingest)
        monkeypatch.setattr(storage, "recall", fake_recall)
    result = invoke(args)
    assert result.exit_code == 0, result.output + stderr_of(result)
    assert seen.get("sent") == expected, (seen, expected)
    err = stderr_of(result)
    if expected is None:
        assert "untagged" in err and not _header_ids(err), err
    else:
        assert _header_ids(err) == {expected}, (expected, err)


# F-3: the stamped workspace is the only way to set the workspace. A caller property
# cannot override it, on the CLI, in the daemon, or in the daemon-down fallback.
USER_PROP_ROWS = [
    {
        "id": "prop-flag-pinned",
        "args": ["--prop", "workspace_id=ws_sneaky000001"],
        "pin": True,
    },
    {
        "id": "prop-flag-unpinned",
        "args": ["--prop", "workspace_id=ws_sneaky000001"],
        "pin": False,
    },
    {
        "id": "free-flag-pinned",
        "args": ["--workspace_id", "ws_sneaky000001"],
        "pin": True,
    },
]


@pytest.mark.parametrize("row", USER_PROP_ROWS, ids=[r["id"] for r in USER_PROP_ROWS])
def test_user_cannot_set_workspace_through_properties(local, monkeypatch, row):
    if row["pin"]:
        assert invoke(["workspace", "create", "demo", "--switch"]).exit_code == 0
    result = invoke(["add", "hello", *row["args"]])
    assert result.exit_code != 0
    assert "Reserved properties: workspace_id" in result.output + stderr_of(result)
    assert not [c for c in local if c[1] == "/memory/ingest"], (
        "nothing reaches the daemon"
    )


def test_fallback_stamp_beats_a_caller_property():
    body = {"workspace_id": "ws_stamped000000"}
    merged = scope_header.fallback_properties(
        body, {"workspace_id": "ws_caller0000000"}
    )
    assert merged == {"workspace_id": "ws_stamped000000"}
    assert scope_header.fallback_properties({}, {"a": "1"}) == {"a": "1"}


def test_workspace_id_is_user_reserved_but_still_ingestable():
    """Lifecycle hooks, the daemon stamp and the fallback pass workspace_id to ingest()."""
    assert "workspace_id" in storage.USER_RESERVED_PROPERTIES
    assert storage.RESERVED_INGEST_PROPERTIES <= storage.USER_RESERVED_PROPERTIES
    assert "workspace_id" not in storage.RESERVED_INGEST_PROPERTIES
