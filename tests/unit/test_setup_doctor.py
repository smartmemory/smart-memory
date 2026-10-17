"""SETUP-DOCTOR-1: one row per doctor row x state (ok / fail / cannot-check).

Env, config file, keychain, shell profile, daemon health, hosted API and the
CLI-ENV-DETECT-1 detector are all injected. Nothing touches the real HOME,
keychain, daemon or network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
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
    "default_team_id": "ws_default",
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
        "The CLI sends workspace ws_default (from account default_team_id from /auth/me), "
        "the same workspace the web console opens",
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
        "but the web console opens workspace ws_default",
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
            "keyring": {"GROQ_API_KEY": CheckUnavailable("OS keychain (KeyringError)")},
        },
        "llm",
        WARN,
        "Not checked: OS keychain (KeyringError)",
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
        "endpoint http://localhost:11434/v1 (from provider default for llm_provider ollama), not answering",
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
            assert CANARY not in out.output, out.output
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


# ── Fix round 1 probes (review-r1 H1-H5) ──────────────────────────────────────
# One row per probe. Each row runs the CLI's real resolver and sm doctor on the
# same inputs and asserts they agree. No live network: H1 uses a local HTTP fake,
# the LLM rows capture what core call_llm would send instead of sending it.


class _FakeService:
    """A local /auth/me and /memory/search that records the X-Workspace-Id it gets."""

    def __init__(self, me):
        import http.server
        import threading

        seen = self.headers = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer(me if self.path == "/auth/me" else {})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(self.headers.get("X-Workspace-Id"))
                self._answer({"results": []})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _core_route(monkeypatch, ctx):
    """What core call_llm would send for a Tier-2 extraction, captured, never sent."""
    from smartmemory.plugins.extractors.llm_single import LLMSingleExtractor
    from smartmemory.utils import llm as core_llm

    from smartmemory_app.config import _apply_local_llm_env
    from smartmemory_app.storage import pin_llm_model

    sent = {}

    class Sent(Exception):
        pass

    def capture(endpoint):
        def fake(*args, **kwargs):
            sent.update(model=kwargs.get("model"), endpoint=endpoint(kwargs))
            raise Sent

        return fake

    monkeypatch.setattr(
        core_llm,
        "call_openai_chat",
        capture(
            lambda kw: kw.get("api_base")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
    )
    monkeypatch.setattr(core_llm, "_call_claude_agent", capture(lambda kw: ""))
    monkeypatch.setattr(
        core_llm, "_call_bedrock_converse", capture(lambda kw: "AWS Bedrock")
    )
    env = doctor._shell_env(ctx)
    cfg = doctor._config(ctx)
    _apply_local_llm_env(cfg, env)
    pin_llm_model(cfg, env)
    with doctor._patched_environ(env):
        try:
            core_llm.call_llm(
                user_content="x", api_base=LLMSingleExtractor()._resolve_api_base()
            )
        except Sent:
            pass
    return sent


def _probe_h1(monkeypatch, tmp_path):
    from smartmemory_app import storage
    from smartmemory_app.cli import cli

    service = _FakeService(
        {
            **ME_OK,
            "default_team_id": "actual-team",
            "default_workspace_id": "console-workspace",
            "active_workspace_id": "console-workspace",
        }
    )
    try:
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        ctx = build(
            monkeypatch,
            tmp_path,
            environ=KEY,
            config=f'[smartmemory]\nmode = "remote"\n[remote]\napi_url = "{service.url}"\n',
        )
        ctx.http_get = doctor._default_http_get
        got = doctor.check_tenant(ctx)
        monkeypatch.setattr(storage, "_remote_memory", None)
        CliRunner().invoke(cli, ["search", "anything"])
    finally:
        service.close()
    assert got.status == WARN, got
    assert "The CLI sends workspace actual-team" in got.detail, got
    assert "web console opens workspace console-workspace" in got.detail, got
    assert service.headers == ["actual-team"], service.headers  # what sm search sent
    return got


def _probe_h2(decision):
    def probe(monkeypatch, tmp_path):
        from smartmemory_app import env_pin
        from smartmemory_app.env_sources import inherited_vars
        from smartmemory_app.recall_format import derive_workspace_id

        ctx = build(
            monkeypatch,
            tmp_path,
            environ={"SMARTMEMORY_WORKSPACE_ID": "ws_shellvalue1"},
        )
        monkeypatch.chdir(tmp_path)
        value = "ws_override77" if decision == "override" else None
        env_pin.set_decision(
            "SMARTMEMORY_WORKSPACE_ID", decision, value, project_dir=tmp_path
        )
        monkeypatch.setattr(env_pin, "_applied", False)
        env_pin.ensure_applied()  # what run_checks and every sm command do first
        import os

        ctx.environ, ctx.from_process, ctx.detect = (
            dict(os.environ),
            True,
            inherited_vars,
        )
        real = derive_workspace_id(str(tmp_path))
        got = doctor.check_tenant(ctx)
        assert got.status == PASS, got
        assert "ws_shellvalue1" not in got.detail and "ws_override77" not in got.detail
        expected = {
            "use": "SMARTMEMORY_WORKSPACE_ID (env, accepted for this project)",
            "override": "the project override of SMARTMEMORY_WORKSPACE_ID (sm env)",
            "ignore": f"Recall workspace {real} comes from the current project folder",
        }[decision]
        assert expected in got.detail, got
        assert real == {
            "use": "ws_shellvalue1",
            "override": "ws_override77",
        }.get(decision, real)
        assert (
            decision != "ignore" or real.startswith("ws_") and real != "ws_shellvalue1"
        )
        return got

    return probe


def _probe_llm(kwargs, provider, model, model_src, endpoint_src, probe_url=None):
    """Doctor's route equals core call_llm's route, and doctor names the winner."""

    def probe(monkeypatch, tmp_path):
        from smartmemory.utils import llm as core_llm

        monkeypatch.setattr(core_llm, "_claude_agent_sdk_available", lambda: False)
        monkeypatch.chdir(tmp_path)  # no ./config.json for core get_config
        seen = []
        ctx = build(monkeypatch, tmp_path, **kwargs)
        inner = ctx.http_get
        ctx.http_get = lambda url, **kw: seen.append(url) or inner(url, **kw)
        route = doctor._llm_route(ctx, doctor._config(ctx))
        core = _core_route(monkeypatch, ctx)
        assert route.provider == provider, route
        assert core["endpoint"] == route.probe_url, (core, route)
        assert core["model"] in (route.model_value, route.model_value.split("/")[-1])
        assert (route.model_value, route.model_src) == model, route
        assert route.model_src == model_src and route.endpoint_src == endpoint_src
        got = doctor.check_llm(ctx)
        if probe_url:
            assert seen == [f"{probe_url}/models"], seen
        return got

    return probe


_CANARY_SKIP = {
    "SMARTMEMORY_MODE": "set per arm (local, remote) or to the canary in the invalid arm",
    "SMARTMEMORY_DAEMON_PORT": "a non-number blocks every config row, so only the invalid arm sets it",
    "SMARTMEMORY_CRASH_REPORTS": "conftest keeps crash reports off",
}


def _canary_env():
    import re
    from pathlib import Path

    import smartmemory_app

    names = set()
    for path in Path(smartmemory_app.__file__).parent.rglob("*.py"):
        names |= set(re.findall(r"SMARTMEMORY_[A-Z0-9_]+", path.read_text()))
    names |= {
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
    }
    return {n: f"DR1CANARY_{n}" for n in sorted(names - set(_CANARY_SKIP))}


def _probe_h4(monkeypatch, tmp_path):
    from smartmemory_app import support_diagnostics
    from smartmemory_app.cli import cli

    from smartmemory_app import bug_report

    canaries = _canary_env()
    url = "https://dr1canary-smartmemory-api-url.example"
    # conftest pins the debug log to tmp_path. Restore the real data_dir lookup
    # so the Storage line prints SMARTMEMORY_DATA_DIR (a tmp path here).
    monkeypatch.setattr(
        bug_report,
        "debug_log_path",
        lambda: Path(os.environ["SMARTMEMORY_DATA_DIR"]) / "cli-debug.log",
    )
    monkeypatch.setattr(
        support_diagnostics, "_daemon_status", lambda: "Daemon: stopped (fake)"
    )
    arms = []
    for mode in ("local", "remote", "invalid"):
        environ = {
            **canaries,
            "SMARTMEMORY_API_URL": url,
            "SMARTMEMORY_DATA_DIR": f"DR1CANARY_SMARTMEMORY_DATA_DIR-{mode}",
            "SMARTMEMORY_MODE": "DR1CANARY_SMARTMEMORY_MODE"
            if mode == "invalid"
            else mode,
        }
        if mode == "invalid":
            environ["SMARTMEMORY_DAEMON_PORT"] = "DR1CANARY_SMARTMEMORY_DAEMON_PORT"
        ctx = build(
            monkeypatch,
            tmp_path / mode,
            environ={**environ, "SHELL": "/bin/zsh"},
            config=LOCAL_CFG + '[local]\nllm_provider = "ollama"\n',
            keyring={"GROQ_API_KEY": True, "api_key": True},
            http={
                f"{url}/auth/me": (200, ME_OK),
                f"{url}/ready": (200, READY_OK),
            },
            daemon=OK_DAEMON,
            detect=[
                FakeVar(n, "~/.zshrc:1", None) for n in environ if "SMARTMEMORY_" in n
            ],
            store="ok",
        )
        monkeypatch.setenv("HOME", str(ctx.home))
        (ctx.home / ".zshrc").write_text(
            "".join(f"export {n}=DR1CANARY_PROFILE_{n}\n" for n in canaries)
        )
        ctx.profile_has = doctor._default_profile_has
        ctx.api_key = lambda: "DR1CANARY_KEYCHAIN_API_KEY"
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda context="doctor", ctx=ctx: doctor.collect(ctx, context),
        )
        for args in (["doctor"], ["doctor", "--json"]):
            out = CliRunner().invoke(cli, args)
            text = out.output + (getattr(out, "stderr", "") or "")
            assert "DR1CANARY_" not in text, (mode, args, text)
            assert "SMARTMEMORY_" in text, text  # names, never values
            if mode != "invalid" and "--json" not in args:  # masked lines really render
                assert "Data directory: $SMARTMEMORY_DATA_DIR (env)" in text, text
            if mode == "remote" and "--json" not in args:
                assert "Network $SMARTMEMORY_API_URL (env)" in text, text
            arms.append(f"{mode} {' '.join(args)}: exit {out.exit_code}, 0 canaries")
    # F1: the older canary test may not exempt any line again (2596b91 let the
    # Network line through). The needle is split so this file does not hold it.
    exemption = "line.startswith(f" + '"Network https://{CANARY}")'
    assert exemption not in Path(__file__).read_text(), "leak check has an exemption"
    return "\n".join(arms)


def _probe_h5(kind):
    def probe(monkeypatch, tmp_path):
        ctx = build(
            monkeypatch,
            tmp_path,
            environ={"GROQ_API_KEY": "g", "SHELL": "/bin/zsh"},
            config=LOCAL_CFG + '[local]\nllm_provider = "groq"\n',
            keyring={
                "GROQ_API_KEY": CheckUnavailable("OS keychain (KeyringError)")
                if kind == "keychain"
                else False
            },
        )
        if kind == "profile":
            monkeypatch.setenv("HOME", str(ctx.home))
            (ctx.home / ".zshrc").mkdir()
            ctx.profile_has = doctor._default_profile_has
        got = doctor.check_llm(ctx)
        named = {
            "keychain": "OS keychain (KeyringError)",
            "profile": "shell profile ~/.zshrc (IsADirectoryError)",
        }[kind]
        assert got.status == WARN, got
        assert f"Not checked: {named}" in got.detail and named in got.fix, got
        return got

    return probe


def _cli_text(monkeypatch, ctx, args):
    """Real `sm doctor` output plus every log line any handler printed meanwhile."""
    import io
    import logging

    from smartmemory_app.cli import cli

    monkeypatch.setattr(
        doctor, "run_checks", lambda context="doctor": doctor.collect(ctx, context)
    )
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    try:
        out = CliRunner().invoke(cli, args)
    finally:
        logging.getLogger().removeHandler(handler)
    return out.output + (getattr(out, "stderr", "") or "") + buf.getvalue()


def _probe_r2_h1a(monkeypatch, tmp_path):
    canary = "R2_UNKNOWN_PROVIDER_CANARY"
    monkeypatch.chdir(tmp_path)
    ctx = build(
        monkeypatch,
        tmp_path,
        environ={"LLM_PROVIDER": canary, "OPENAI_API_KEY": "sk-synthetic"},
        config=LOCAL_CFG + '[local]\nllm_provider = "none"\n',
    )
    seen = []
    for args in (["doctor"], ["doctor", "--json"]):
        text = _cli_text(monkeypatch, ctx, args)
        assert canary.lower() not in text.lower(), text
        assert "is not a recognized provider" in text, (
            text
        )  # the core record was printed
        seen.append(f"{' '.join(args)}: 0 canaries, core warning printed masked")
    return "; ".join(seen)


def _probe_r2_h1b(monkeypatch, tmp_path):
    ctx = build(monkeypatch, tmp_path, environ={"SMARTMEMORY_MODE": "z9x"})
    text = _cli_text(monkeypatch, ctx, ["doctor"])
    assert "z9x" not in text.lower(), text
    mode = row(ctx, "mode")
    assert mode.status == FAIL and "env SMARTMEMORY_MODE is invalid" in mode.detail
    assert "Invalid SMARTMEMORY_MODE='$SMARTMEMORY_MODE (env)'" in text, text
    # Short plain words are masked only quoted or after =, never inside prose.
    assert doctor.mask_env_values("an abc of 'abc'", {"SMARTMEMORY_X": "abc"}) == (
        "an abc of '$SMARTMEMORY_X (env)'"
    )
    return mode


def _probe_r2_h2a(monkeypatch, tmp_path):
    from smartmemory.utils import llm as core_llm

    monkeypatch.setattr(core_llm, "_claude_agent_sdk_available", lambda: False)
    monkeypatch.chdir(tmp_path)
    ctx = build(
        monkeypatch,
        tmp_path,
        environ={"ANTHROPIC_API_KEY": "sk-ant-synthetic"},
        config=LOCAL_CFG
        + '[local]\nllm_provider = "anthropic"\nllm_model = "sonnet"\n',
    )
    route = doctor._llm_route(ctx, doctor._config(ctx))
    core = _core_route(monkeypatch, ctx)
    assert core["model"] == route.model_value == "claude-sonnet-4-6-20250627", (
        core,
        route,
    )
    assert route.model == "sonnet, sent as claude-sonnet-4-6-20250627", route
    assert route.model_src == "config file llm_model", route
    assert core["endpoint"] == route.probe_url, (core, route)
    return route.text()


def _probe_r2_h2b(monkeypatch, tmp_path):
    canary = "R2_LEAF_MODEL_CANARY"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"extractor": {"llm": {"model_name": "file-model"}}})
    )
    ctx = build(
        monkeypatch,
        tmp_path,
        environ={"EXTRACTOR_LLM_MODEL_NAME": canary, "OPENAI_API_KEY": "sk-synthetic"},
        config=LOCAL_CFG,
    )
    route = doctor._llm_route(ctx, doctor._config(ctx))
    core = _core_route(monkeypatch, ctx)
    assert core["model"] == route.model_value == canary, (core, route)
    assert route.model_src == "env EXTRACTOR_LLM_MODEL_NAME", route
    rows = doctor.collect(ctx)
    assert canary.lower() not in doctor.render(rows).lower()
    assert canary.lower() not in json.dumps(doctor.to_json(rows)).lower()
    for args in (["doctor"], ["doctor", "--json"]):
        assert canary.lower() not in _cli_text(monkeypatch, ctx, args).lower()
    return doctor.check_llm(ctx)


def _probe_r2_h3(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").mkdir()
    ctx = build(monkeypatch, tmp_path, environ={"OPENAI_API_KEY": "sk-synthetic"})
    got = doctor.check_llm(ctx)
    named = f"core config {tmp_path / 'config.json'}"
    assert got.status == WARN, got
    assert f"Not checked: {named}" in got.detail and named in got.fix, got
    text = _cli_text(monkeypatch, ctx, ["doctor"])
    assert "Some local checks were not completed" in text or "warnings above" in text
    return got


def _probe_api_env(monkeypatch, tmp_path):
    """Review r2's own row: env API URL over a different config URL, remote mode."""
    env_url = "https://r2-api-env-canary.example"
    seen = []
    ctx = build(
        monkeypatch,
        tmp_path,
        environ={**KEY, "SMARTMEMORY_API_URL": env_url},
        config=REMOTE_CFG,
        http={f"{env_url}/auth/me": (200, ME_OK), f"{env_url}/ready": (200, READY_OK)},
    )
    inner = ctx.http_get
    ctx.http_get = lambda url, **kw: seen.append(url) or inner(url, **kw)
    assert doctor._config(ctx).api_url.rstrip("/") == env_url  # what the CLI sends to
    got = row(ctx, "service")
    assert got.status == PASS and "$SMARTMEMORY_API_URL" in got.detail, got
    assert seen and all(url.startswith(env_url) for url in seen), seen
    for args in (["doctor"], ["doctor", "--json"]):
        assert "r2-api-env-canary" not in _cli_text(monkeypatch, ctx, args)
    return got


_OLLAMA = LOCAL_CFG + '[local]\nllm_provider = "ollama"\n'
PROBES = [
    ("H1", _probe_h1),
    ("H2-use", _probe_h2("use")),
    ("H2-override", _probe_h2("override")),
    ("H2-ignore", _probe_h2("ignore")),
    (
        "H3-url",
        _probe_llm(
            {
                "config": _OLLAMA + 'llm_base_url = "http://configured.test:1/v1"\n',
                "environ": {"OPENAI_BASE_URL": "http://different.test:2/v1"},
                "http": {"http://different.test:2/v1/models": (200, {})},
            },
            "ollama",
            ("gpt-4o-mini", "core default"),
            "core default",
            "env OPENAI_BASE_URL",
            probe_url="http://different.test:2/v1",
        ),
    ),
    (
        "H3-core",
        _probe_llm(
            {"environ": {"LLM_PROVIDER": "gemini", "LLM_API_KEY": "dummy"}},
            "gemini",
            ("gemini-2.0-flash", "default for LLM_PROVIDER gemini"),
            "default for LLM_PROVIDER gemini",
            "LLM_PROVIDER gemini",
        ),
    ),
    (
        "H3-model-env",
        _probe_llm(
            {
                "config": _OLLAMA + 'llm_model = "file-model"\n',
                "environ": {"SMARTMEMORY_LLM_MODEL": "env-model"},
            },
            "ollama",
            ("env-model", "env SMARTMEMORY_LLM_MODEL"),
            "env SMARTMEMORY_LLM_MODEL",
            "provider default for llm_provider ollama",
        ),
    ),
    (
        "H3-model-file",
        _probe_llm(
            {"config": _OLLAMA + 'llm_model = "file-model"\n'},
            "ollama",
            ("file-model", "config file llm_model"),
            "config file llm_model",
            "provider default for llm_provider ollama",
        ),
    ),
    ("H4", _probe_h4),
    ("H5-keychain", _probe_h5("keychain")),
    ("H5-profile", _probe_h5("profile")),
    ("R2-H1a", _probe_r2_h1a),
    ("R2-H1b", _probe_r2_h1b),
    ("R2-H2a", _probe_r2_h2a),
    ("R2-H2b", _probe_r2_h2b),
    ("R2-H3", _probe_r2_h3),
    ("API-env", _probe_api_env),
]


@pytest.mark.parametrize("probe", PROBES, ids=[p[0] for p in PROBES])
def test_fix_r1_probe(probe, monkeypatch, tmp_path, capsys):
    _id, run = probe
    got = run(monkeypatch, tmp_path)
    with capsys.disabled():
        print(f"\nPROBE {_id}: {got}")
