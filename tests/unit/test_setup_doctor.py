"""SETUP-DOCTOR-1: one row per doctor row x state (ok / fail / cannot-check).

Env, config file, keychain, shell profile, daemon health, hosted API and the
CLI-ENV-DETECT-1 detector are all injected. Nothing touches the real HOME,
keychain, daemon or network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from smartmemory_app import doctor
from smartmemory_app.doctor import FAIL, PASS, WARN, CheckUnavailable, Context


@dataclass(frozen=True)
class FakeVar:
    name: str
    source: str | None
    decision: str | None


class Down(Exception):
    """Stand-in for a transport error."""


API = "https://api.example.test"
ME_OK = {
    "email": "dev@example.test",
    "tenant_id": "tenant_1",
    "default_workspace_id": "ws_default",
    "active_workspace_id": "ws_default",
}
READY_OK = {
    "status": "ready",
    "version": "9.9.9",
    "checks": [{"name": "falkordb", "ok": True}],
}
READY_GRAPH_DOWN = {
    "status": "not_ready",
    "version": "9.9.9",
    "checks": [{"name": "falkordb", "ok": False}],
}
REMOTE_CFG = f'[smartmemory]\nmode = "remote"\n[remote]\napi_url = "{API}"\n'
LOCAL_CFG = '[smartmemory]\nmode = "local"\n'
_ALWAYS_CLEARED = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
)


def build(
    monkeypatch,
    tmp_path,
    *,
    environ=None,
    config=LOCAL_CFG,
    http=None,
    keyring=None,
    profile=(),
    daemon=None,
    expected=False,
    detect="none",
    python=(3, 12, 1),
    venv=True,
    store=None,
):
    """A Context whose every input is a fake. ``store`` = 'ok' | 'corrupt' | None."""
    import os

    for name in list(os.environ):
        if name == "SMARTMEMORY_CRASH_REPORTS":
            continue  # conftest keeps crash reports off
        if name.startswith("SMARTMEMORY_") or name in _ALWAYS_CLEARED:
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".smartmemory").mkdir(parents=True, exist_ok=True)
    cfg_root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))
    env = {"XDG_CONFIG_HOME": str(cfg_root), **(environ or {})}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if config is not None:
        (cfg_root / "smartmemory").mkdir(parents=True, exist_ok=True)
        (cfg_root / "smartmemory" / "config.toml").write_text(config)
    data_dir = (
        tmp_path / env["SMARTMEMORY_DATA_DIR"]
        if "SMARTMEMORY_DATA_DIR" in env
        else home / ".smartmemory"
    )
    if "SMARTMEMORY_DATA_DIR" in env:
        monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
        env["SMARTMEMORY_DATA_DIR"] = str(data_dir)
    if store == "ok":
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(data_dir / "memory.db")
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.commit()
        conn.close()
    elif store == "corrupt":
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "memory.db").write_bytes(b"not a database at all, just bytes" * 40)

    def http_get(url, headers=None, timeout=5.0):
        answer = (http or {}).get(url)
        if answer is None or isinstance(answer, Exception):
            raise answer or Down(url)
        return answer

    def keyring_has(service, name):
        value = (keyring or {}).get(name)
        if isinstance(value, Exception):
            raise value
        return bool(value)

    def daemon_status():
        if isinstance(daemon, Exception):
            raise daemon
        return daemon

    if detect == "none":
        detector = lambda **kw: []  # noqa: E731
    elif detect is None:
        detector = None
    elif isinstance(detect, Exception):

        def detector(**kw):
            raise detect

    else:
        detector = lambda **kw: list(detect)  # noqa: E731

    from smartmemory_app.config import config_path, load_config

    return Context(
        environ=dict(os.environ),
        home=home,
        cwd=tmp_path,
        platform="darwin",
        python_version=python,
        prefix="/venv" if venv else "/usr",
        base_prefix="/usr",
        executable="/venv/bin/python",
        config_path=config_path,
        load_config=load_config,
        http_get=http_get,
        keyring_has=keyring_has,
        profile_has=lambda name: name in profile,
        daemon_status=daemon_status,
        daemon_expected=lambda: expected,
        api_key=lambda: "keychain-key" if (keyring or {}).get("api_key") else "",
        detect=detector,
    )


@pytest.fixture(autouse=True)
def _no_inherited_shell_env(monkeypatch):
    """The developer's own SMARTMEMORY_* exports must not leak into these tests."""
    import os

    for name in list(os.environ):
        if name.startswith("SMARTMEMORY_") and name != "SMARTMEMORY_CRASH_REPORTS":
            monkeypatch.delenv(name)


def row(ctx, key, context="doctor"):
    rows = doctor.collect(ctx, context)
    titles = dict(zip(doctor._CONTEXT_ROWS[context], rows))
    return titles[key]


KEY = {"SMARTMEMORY_API_KEY": "k"}
OK_DAEMON = {"service": "smartmemory", "status": "ok", "mode": "lite", "pid": 7}

# (id, build kwargs, row key, expected status, text that must appear)
CASES = [
    # python
    ("python-ok", {}, "python", PASS, "3.12.1, venv"),
    ("python-old", {"python": (3, 10, 4)}, "python", FAIL, "needs 3.11"),
    ("python-no-venv", {"venv": False}, "python", WARN, "not in a virtual environment"),
    # config file
    ("config-ok", {}, "config", PASS, "XDG_CONFIG_HOME"),
    (
        "config-missing",
        {"config": None},
        "config",
        WARN,
        "will create a local-mode config",
    ),
    ("config-bad-toml", {"config": "mode = = ="}, "config", FAIL, "not valid TOML"),
    (
        "config-bad-mode",
        {"config": '[smartmemory]\nmode = "cloud"\n'},
        "config",
        FAIL,
        "invalid mode",
    ),
    # mode and why
    ("mode-file", {}, "mode", PASS, "local (from config file)"),
    (
        "mode-env-overrides-file",
        {
            "environ": {"SMARTMEMORY_MODE": "remote"},
            "detect": [FakeVar("SMARTMEMORY_MODE", "~/.zshrc:3", None)],
        },
        "mode",
        WARN,
        "remote (from env SMARTMEMORY_MODE, exported at ~/.zshrc:3), overriding local",
    ),
    (
        "mode-env-decided",
        {
            "environ": {"SMARTMEMORY_MODE": "local"},
            "detect": [FakeVar("SMARTMEMORY_MODE", "~/.zshrc:3", "use")],
        },
        "mode",
        PASS,
        "local (from env SMARTMEMORY_MODE",
    ),
    (
        "mode-env-invalid",
        {"environ": {"SMARTMEMORY_MODE": "cloudy"}},
        "mode",
        FAIL,
        "every sm command stops",
    ),
    (
        "mode-port-invalid",
        {"environ": {"SMARTMEMORY_DAEMON_PORT": "abc"}},
        "mode",
        FAIL,
        "SMARTMEMORY_DAEMON_PORT is invalid",
    ),
    ("mode-unconfigured", {"config": None}, "mode", WARN, "not configured"),
    # inherited SMARTMEMORY_* vars
    ("inherited-none", {}, "inherited", PASS, "none"),
    (
        "inherited-steering-undecided",
        {"detect": [FakeVar("SMARTMEMORY_TEAM_ID", "~/.zshrc:9", None)]},
        "inherited",
        FAIL,
        "SMARTMEMORY_TEAM_ID (~/.zshrc:9)",
    ),
    (
        "inherited-toggle-undecided",
        {"detect": [FakeVar("SMARTMEMORY_NO_UPDATE_CHECK", None, None)]},
        "inherited",
        WARN,
        "SMARTMEMORY_NO_UPDATE_CHECK (source not found)",
    ),
    (
        "inherited-decided",
        {"detect": [FakeVar("SMARTMEMORY_MODE", "~/.zshrc:3", "ignore")]},
        "inherited",
        PASS,
        "SMARTMEMORY_MODE (~/.zshrc:3, ignore)",
    ),
    (
        "inherited-detector-missing",
        {"detect": None, "environ": {"SMARTMEMORY_TEAM_ID": "t"}},
        "inherited",
        WARN,
        "not checked. Present: SMARTMEMORY_CRASH_REPORTS, SMARTMEMORY_TEAM_ID",
    ),
    (
        "inherited-detector-crash",
        {"detect": RuntimeError("x")},
        "inherited",
        WARN,
        "detection failed (RuntimeError)",
    ),
    # API key
    ("credentials-local", {}, "credentials", PASS, "not needed in local mode"),
    (
        "credentials-env",
        {"config": REMOTE_CFG, "environ": KEY},
        "credentials",
        PASS,
        "env SMARTMEMORY_API_KEY",
    ),
    (
        "credentials-keychain",
        {"config": REMOTE_CFG, "keyring": {"api_key": True}},
        "credentials",
        PASS,
        "OS keychain",
    ),
    ("credentials-none", {"config": REMOTE_CFG}, "credentials", FAIL, "none found"),
    (
        "credentials-keychain-unreadable",
        {
            "config": REMOTE_CFG,
            "keyring": {"api_key": CheckUnavailable("OS keychain (KeyringError)")},
        },
        "credentials",
        WARN,
        "not checked: OS keychain (KeyringError) could not be read",
    ),
    # tenant and workspace
    ("tenant-local", {}, "tenant", PASS, "local store, no tenant"),
    (
        "tenant-local-env-workspace",
        {"environ": {"SMARTMEMORY_WORKSPACE_ID": "ws_other"}},
        "tenant",
        WARN,
        "Recall workspace $SMARTMEMORY_WORKSPACE_ID comes from env SMARTMEMORY_WORKSPACE_ID",
    ),
    (
        "tenant-remote-no-key",
        {"config": REMOTE_CFG},
        "tenant",
        WARN,
        "not checked: no API key",
    ),
    (
        "tenant-remote-unreachable",
        {"config": REMOTE_CFG, "environ": KEY},
        "tenant",
        WARN,
        "unreachable (Down)",
    ),
    (
        "tenant-remote-401",
        {"config": REMOTE_CFG, "environ": KEY, "http": {f"{API}/auth/me": (401, {})}},
        "tenant",
        FAIL,
        "rejected the API key",
    ),
    (
        "tenant-remote-500",
        {"config": REMOTE_CFG, "environ": KEY, "http": {f"{API}/auth/me": (500, None)}},
        "tenant",
        WARN,
        "answered HTTP 500",
    ),
    (
        "tenant-remote-default",
        {
            "config": REMOTE_CFG,
            "environ": KEY,
            "http": {f"{API}/auth/me": (200, ME_OK)},
        },
        "tenant",
        PASS,
        "account dev@example.test, tenant tenant_1, workspace ws_default (from account default)",
    ),
    (
        "tenant-remote-team-mismatch",
        {
            "config": REMOTE_CFG,
            "environ": {**KEY, "SMARTMEMORY_TEAM_ID": "ws_other"},
            "http": {f"{API}/auth/me": (200, ME_OK)},
        },
        "tenant",
        WARN,
        "web console opens, is ws_default",
    ),
    # LLM
    ("llm-remote", {"config": REMOTE_CFG}, "llm", PASS, "hosted"),
    ("llm-none-no-key", {}, "llm", WARN, "so sm ask and LLM extraction are off"),
    (
        "llm-none-env-key",
        {"environ": {"GROQ_API_KEY": "g"}},
        "llm",
        PASS,
        "GROQ_API_KEY yes (env)",
    ),
    (
        "llm-groq-missing",
        {"config": LOCAL_CFG + '[local]\nllm_provider = "groq"\n'},
        "llm",
        FAIL,
        "GROQ_API_KEY is missing",
    ),
    (
        "llm-groq-keychain-and-profile",
        {
            "config": LOCAL_CFG + '[local]\nllm_provider = "groq"\n',
            "keyring": {"GROQ_API_KEY": True},
            "profile": ("GROQ_API_KEY",),
        },
        "llm",
        PASS,
        "GROQ_API_KEY yes (OS keychain + shell profile)",
    ),
    (
        "llm-keychain-unreadable",
        {
            "environ": {"GROQ_API_KEY": "g"},
            "keyring": {"GROQ_API_KEY": CheckUnavailable("kc")},
        },
        "llm",
        PASS,
        "Not checked: OS keychain",
    ),
    (
        "llm-ollama-up",
        {
            "config": LOCAL_CFG + '[local]\nllm_provider = "ollama"\n',
            "http": {"http://localhost:11434/v1/models": (200, {})},
        },
        "llm",
        PASS,
        "server answering",
    ),
    (
        "llm-ollama-down",
        {"config": LOCAL_CFG + '[local]\nllm_provider = "ollama"\n'},
        "llm",
        FAIL,
        "not answering at http://localhost:11434/v1",
    ),
    (
        "llm-claude-agent",
        {"config": LOCAL_CFG + '[local]\nllm_provider = "claude-agent"\n'},
        "llm",
        WARN,
        "not checked here",
    ),
    (
        "llm-daemon-without-key",
        {
            "environ": {"GROQ_API_KEY": "g"},
            "daemon": {**OK_DAEMON, "llm_key_present": False},
        },
        "llm",
        WARN,
        "running daemon started without a key",
    ),
    # daemon (local) or hosted service (remote)
    (
        "service-daemon-ok",
        {"daemon": OK_DAEMON},
        "service",
        PASS,
        "running on port 9014 (from default), pid 7",
    ),
    ("service-daemon-stopped", {}, "service", WARN, "Commands still work"),
    (
        "service-daemon-not-answering",
        {"expected": True},
        "service",
        FAIL,
        "should be running",
    ),
    (
        "service-daemon-degraded",
        {"daemon": {**OK_DAEMON, "status": "degraded", "degraded_reason": "db locked"}},
        "service",
        FAIL,
        "degraded on port 9014",
    ),
    (
        "service-daemon-warming",
        {"daemon": {**OK_DAEMON, "status": "warming"}},
        "service",
        WARN,
        "still loading",
    ),
    (
        "service-daemon-wrong-mode",
        {"daemon": {**OK_DAEMON, "mode": "remote"}},
        "service",
        FAIL,
        "runs in remote mode",
    ),
    (
        "service-daemon-env-port",
        {"environ": {"SMARTMEMORY_DAEMON_PORT": "9999"}, "daemon": OK_DAEMON},
        "service",
        PASS,
        "port 9999 (from env SMARTMEMORY_DAEMON_PORT)",
    ),
    (
        "service-daemon-probe-crash",
        {"daemon": TimeoutError("t")},
        "service",
        WARN,
        "not checked: health probe",
    ),
    (
        "service-remote-ready",
        {"config": REMOTE_CFG, "http": {f"{API}/ready": (200, READY_OK)}},
        "service",
        PASS,
        "ready, version 9.9.9",
    ),
    (
        "service-remote-unreachable",
        {"config": REMOTE_CFG},
        "service",
        FAIL,
        "api.example.test unreachable",
    ),
    (
        "service-remote-not-ready",
        {"config": REMOTE_CFG, "http": {f"{API}/ready": (503, READY_GRAPH_DOWN)}},
        "service",
        FAIL,
        "failing: falkordb",
    ),
    # graph backend
    ("graph-local-ok", {"store": "ok"}, "graph", PASS, "SQLite store"),
    ("graph-local-missing", {}, "graph", WARN, "does not exist yet"),
    (
        "graph-local-corrupt",
        {"store": "corrupt"},
        "graph",
        FAIL,
        "cannot be opened (DatabaseError)",
    ),
    (
        "graph-local-env-dir-and-ignored-config",
        {
            "environ": {"SMARTMEMORY_DATA_DIR": "elsewhere"},
            "config": LOCAL_CFG + '[local]\ndata_dir = "/nowhere/store"\n',
            "store": "ok",
        },
        "graph",
        WARN,
        "data_dir (/nowhere/store) is not used by sm commands run from this shell",
    ),
    (
        "graph-remote-ok",
        {"config": REMOTE_CFG, "http": {f"{API}/ready": (200, READY_OK)}},
        "graph",
        PASS,
        "hosted graph store reachable",
    ),
    (
        "graph-remote-down",
        {"config": REMOTE_CFG, "http": {f"{API}/ready": (503, READY_GRAPH_DOWN)}},
        "graph",
        FAIL,
        "hosted graph store is down",
    ),
    (
        "graph-remote-unreachable",
        {"config": REMOTE_CFG},
        "graph",
        WARN,
        "not checked: service unreachable",
    ),
    (
        "graph-remote-unreported",
        {
            "config": REMOTE_CFG,
            "http": {f"{API}/ready": (200, {"status": "ready", "checks": []})},
        },
        "graph",
        WARN,
        "did not report its graph store",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_doctor_row(case, monkeypatch, tmp_path):
    _id, kwargs, key, status, text = case
    result = row(build(monkeypatch, tmp_path, **kwargs), key)
    assert result.status == status, result
    assert text in result.detail, result
    if status != PASS:
        assert result.fix or key == "inherited", result


def test_tour_context_rows(monkeypatch, tmp_path):
    ctx = build(
        monkeypatch,
        tmp_path,
        environ={"SMARTMEMORY_MODE": "remote"},
        detect=[FakeVar("SMARTMEMORY_MODE", "~/.zshrc:3", None)],
    )
    rows = doctor.collect(ctx, "tour")
    assert [r.check for r in rows] == [
        "Python",
        "Config file",
        "Mode",
        "Inherited SMARTMEMORY_* vars",
        "LLM",
    ]
    mode = rows[2]
    assert mode.status == FAIL and "tour runs only in local mode" in mode.detail
    assert "~/.zshrc:3" in mode.detail and mode.fix == "Unset SMARTMEMORY_MODE"


def test_invalid_env_marks_config_rows_not_checked(monkeypatch, tmp_path):
    ctx = build(monkeypatch, tmp_path, environ={"SMARTMEMORY_MODE": "bogus"})
    rows = doctor.collect(ctx, "doctor")
    skipped = [
        r
        for r in rows
        if r.check
        in (
            "API key",
            "Tenant and workspace",
            "LLM",
            "Daemon or service",
            "Graph backend",
        )
    ]
    assert len(skipped) == 5
    assert all(r.status == WARN and "not checked" in r.detail for r in skipped)


def test_crashing_check_is_warn_not_silent(monkeypatch, tmp_path, caplog):
    ctx = build(monkeypatch, tmp_path)

    def boom(ctx):
        raise KeyError("x")

    monkeypatch.setitem(doctor._CHECKS, "graph", boom)
    graph = doctor.collect(ctx, "doctor")[-1]
    assert (
        graph.status == WARN
        and "not checked" in graph.detail
        and "KeyError" in graph.detail
    )
    assert "doctor check graph could not run" in caplog.text


def test_real_detector_names_the_exporting_file(monkeypatch, tmp_path):
    """CLI-ENV-DETECT-1's detector, unfaked: the row FAILs and names ~/.zshrc:N."""
    from smartmemory_app.env_sources import inherited_vars

    ctx = build(
        monkeypatch,
        tmp_path,
        environ={"SHELL": "/bin/zsh", "SMARTMEMORY_MODE": "remote"},
    )
    (ctx.home / ".zshrc").write_text("alias ll=ls\nexport SMARTMEMORY_MODE=remote\n")
    ctx.detect = inherited_vars
    got = doctor.check_inherited(ctx)
    assert got.status == FAIL and "SMARTMEMORY_MODE (~/.zshrc:2)" in got.detail
    assert "sm env" in got.fix


def test_process_env_lets_detector_see_ignored_names(monkeypatch, tmp_path):
    """From the real process the detector gets environ=None, so it reads the names
    the shell passed in, not the ones left after an "ignore" decision removed them."""
    ctx = build(monkeypatch, tmp_path)
    seen = []
    ctx.detect = lambda **kw: seen.append(kw["environ"]) or []
    doctor.check_inherited(ctx)
    ctx.from_process, ctx._cache = True, {}
    doctor.check_inherited(ctx)
    assert seen[0] is not None and seen[1] is None


CANARY = "CANARY-7f3a-SECRET"


# Every free-form SMARTMEMORY_* setting plus every key, set to the canary. Two
# environments: the first keeps the hosted API answering (tenant, service and
# graph rows reach their PASS arms), the second moves the URLs and data dir.
_KEYS = {
    "SMARTMEMORY_API_KEY": CANARY,
    "SMARTMEMORY_SYNC_TOKEN": CANARY,
    "SMARTMEMORY_CRASH_REPORT_KEY": CANARY,
    "SMARTMEMORY_SOME_TOGGLE": CANARY,
    "GROQ_API_KEY": CANARY,
    "OPENAI_API_KEY": CANARY,
    "ANTHROPIC_API_KEY": CANARY,
    "DEEPSEEK_API_KEY": CANARY,
    "GEMINI_API_KEY": CANARY,
    "SMARTMEMORY_TEAM_ID": CANARY,
    "SMARTMEMORY_WORKSPACE_ID": CANARY,
    "SMARTMEMORY_LLM_MODEL": CANARY,
}
CANARY_ENVS = [
    _KEYS,
    {
        **_KEYS,
        "SMARTMEMORY_API_URL": f"https://{CANARY}.example",
        "SMARTMEMORY_DATA_DIR": f"data-{CANARY}",
        "SMARTMEMORY_LLM_PROVIDER": "ollama",
        "SMARTMEMORY_LLM_BASE_URL": f"http://{CANARY}:11434/v1",
    },
    {**_KEYS, "SMARTMEMORY_LLM_PROVIDER": CANARY},
]


@pytest.mark.parametrize("environ", CANARY_ENVS, ids=["api-up", "moved", "provider"])
def test_no_secret_value_is_ever_printed(environ, monkeypatch, tmp_path):
    from smartmemory_app.cli import cli

    detect = [
        FakeVar(name, "~/.zshrc:1", None)
        for name in environ
        if name.startswith("SMARTMEMORY_")
    ]
    for config in (REMOTE_CFG, LOCAL_CFG):
        ctx = build(
            monkeypatch,
            tmp_path,
            environ=environ,
            config=config,
            http={f"{API}/auth/me": (200, ME_OK), f"{API}/ready": (200, READY_OK)},
            daemon=OK_DAEMON,
            detect=detect,
            store="ok",
        )
        for context in ("doctor", "setup", "tour"):
            rows = doctor.collect(ctx, context)
            assert CANARY not in doctor.render(rows)
            assert CANARY not in json.dumps(doctor.to_json(rows))
        monkeypatch.setattr(
            doctor, "run_checks", lambda context="doctor": doctor.collect(ctx, context)
        )
        for args in (["doctor"], ["doctor", "--json"]):
            out = CliRunner().invoke(cli, args)
            leaked = [line for line in out.output.splitlines() if CANARY in line]
            # The older network probe line (support_diagnostics, not owned here)
            # names the effective API URL so support can see a LAN override. It
            # redacts credentials. Nothing else may carry the value.
            allowed = "SMARTMEMORY_API_URL" in environ
            assert [
                line
                for line in leaked
                if not (allowed and line.startswith(f"Network https://{CANARY}"))
            ] == []
            assert "SMARTMEMORY_API_KEY" in out.output  # names are shown


FAIL_ROW = doctor.Row(
    "Mode", FAIL, "remote (from env SMARTMEMORY_MODE)", "Unset SMARTMEMORY_MODE"
)
PASS_ROW = doctor.Row("Python", PASS, "3.12")


def test_doctor_json_and_exit_code(monkeypatch):
    from smartmemory_app.cli import cli

    monkeypatch.setattr(
        doctor, "run_checks", lambda context="doctor": [PASS_ROW, FAIL_ROW]
    )
    out = CliRunner().invoke(cli, ["doctor", "--json"])
    assert out.exit_code == 1, out.output
    payload = json.loads(out.output)
    assert payload["ok"] is False
    assert payload["checks"][1] == {
        "check": "Mode",
        "status": "fail",
        "detail": "remote (from env SMARTMEMORY_MODE)",
        "fix": "Unset SMARTMEMORY_MODE",
    }
    text = CliRunner().invoke(cli, ["doctor"])
    assert text.exit_code == 1
    assert "FAIL  Mode" in text.output and "fix: Unset SMARTMEMORY_MODE" in text.output


def test_setup_runs_doctor_and_stops_on_fail(monkeypatch):
    from smartmemory_app import setup as setup_mod

    monkeypatch.setattr(setup_mod, "_setup_click", lambda *a, **k: None)
    monkeypatch.setattr("smartmemory_app.config.is_configured", lambda: True)
    calls = []

    def fake(context="doctor"):
        calls.append(context)
        return [PASS_ROW, FAIL_ROW]

    monkeypatch.setattr(doctor, "run_checks", fake)
    out = CliRunner().invoke(setup_mod.setup, ["--mode", "local"])
    assert calls == ["setup"]
    assert out.exit_code == 1
    assert "fix: Unset SMARTMEMORY_MODE" in out.output
    assert "1 check(s) failed" in out.output

    monkeypatch.setattr(doctor, "run_checks", lambda context="doctor": [PASS_ROW])
    assert CliRunner().invoke(setup_mod.setup, ["--mode", "local"]).exit_code == 0


def test_cancelled_setup_skips_doctor(monkeypatch):
    from smartmemory_app import setup as setup_mod

    monkeypatch.setattr(setup_mod, "_setup_click", lambda *a, **k: None)
    monkeypatch.setattr("smartmemory_app.config.is_configured", lambda: False)
    monkeypatch.setattr(
        doctor, "run_checks", lambda context="doctor": pytest.fail("ran")
    )
    assert CliRunner().invoke(setup_mod.setup, ["--mode", "local"]).exit_code == 0


def test_tour_stops_on_fail_before_starting(monkeypatch):
    from smartmemory_app import tour
    from smartmemory_app.cli import cli

    started = []
    monkeypatch.setattr(tour, "run_tour", lambda **kw: started.append(kw))
    monkeypatch.setattr(doctor, "run_checks", lambda context="doctor": [FAIL_ROW])
    out = CliRunner().invoke(cli, ["tour"])
    assert out.exit_code == 1 and started == []
    assert (
        "before starting the tour" in out.output
        and "fix: Unset SMARTMEMORY_MODE" in out.output
    )

    seen = []
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda context="doctor": seen.append(context) or [PASS_ROW],
    )
    out = CliRunner().invoke(cli, ["tour"])
    assert seen == ["tour"] and len(started) == 1, out.output
