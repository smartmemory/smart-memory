"""Secret surfaces from the CLI-FU-SWEEP-1a r1 review (H1 to H4), replayed as real tests.

Every value here is synthetic. No test reads a real credential store, runs launchctl
or opens a network connection: the credential store is an in-memory dict and the
launchctl process boundary is replaced.
"""

import contextlib
import io
import json
import os
import plistlib
import re
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from smartmemory_app.config import SmartMemoryConfig

KEY = "sk-test-PROBE123"
# CLI-DAEMON-KEY-PRECEDENCE-1: the loader reads only the selected provider's key; these tests store a Groq key.
_GROQ_CFG = SmartMemoryConfig(mode="local", llm_provider="groq")
MARK = b"PROBE123"

_ENV_NAMES = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_API_KEY",
    "LLM_PROVIDER",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("SMARTMEMORY_", "EXTRACTOR_")) or name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    monkeypatch.setenv("SMARTMEMORY_ENV_CHECK", "0")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    from smartmemory_app import env_pin

    monkeypatch.setattr(env_pin, "ensure_applied", lambda: None)
    return home


@pytest.fixture
def plist_env(monkeypatch, isolate):
    """Run the real plist writer in a throwaway HOME with an in-memory key store."""
    from smartmemory_app import config, daemon, setup

    home = isolate
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(setup, "LAUNCH_AGENTS_DIR", agents)
    cfg = config.SmartMemoryConfig(data_dir=str(home / "data"))
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        daemon,
        "_run_command",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
    )
    store = {}
    monkeypatch.setattr("keyring.get_password", lambda service, name: store.get(name))
    monkeypatch.setattr(
        "keyring.set_password", lambda service, name, value: store.update({name: value})
    )

    def run(caplog):
        captured = io.StringIO()
        with (
            caplog.at_level("WARNING"),
            contextlib.redirect_stdout(captured),
            contextlib.redirect_stderr(captured),
        ):
            assert setup._install_launchd_plist()
        return captured.getvalue()

    return home, agents, store, run


def _home_hits(home, marker=MARK):
    return [
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and marker in p.read_bytes()
    ]


def _plist_env(agents, name):
    return plistlib.loads((agents / name).read_bytes())["EnvironmentVariables"]


# Review row ids: test_plist_paths[fresh|rewrite|proxy]. The proxy row is H1, and the
# rows after it cover the rest of the env-to-plist class (every proxy variable, a
# scheme-less login, a password holding a slash, a login-free proxy that must stay).
_PLIST_ROWS = {
    "fresh": {"GROQ_API_KEY": KEY},
    "rewrite": {"GROQ_API_KEY": KEY},
    "old-plist-only": {},
    "proxy": {
        "GROQ_API_KEY": KEY,
        "HTTPS_PROXY": f"http://probe-user:{KEY}@127.0.0.1:9",
    },
    "proxy-http-upper": {"HTTP_PROXY": f"http://probe-user:{KEY}@127.0.0.1:9"},
    "proxy-all-lower": {"all_proxy": f"socks5://probe-user:{KEY}@127.0.0.1:9"},
    "proxy-no-scheme": {"https_proxy": f"probe-user:{KEY}@127.0.0.1:9"},
    "proxy-slash-password": {"HTTPS_PROXY": f"http://probe-user:pa/{KEY}@127.0.0.1:9"},
    "proxy-user-only": {"HTTPS_PROXY": f"http://{KEY}@127.0.0.1:9"},
}


@pytest.mark.parametrize("case", sorted(_PLIST_ROWS))
def test_plist_paths(case, monkeypatch, plist_env, caplog):
    home, agents, store, run = plist_env
    from smartmemory_app import config, setup

    if case in ("rewrite", "old-plist-only"):
        for name in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME):
            (agents / name).write_bytes(
                plistlib.dumps({"EnvironmentVariables": {"GROQ_API_KEY": KEY}})
            )
    for name, value in _PLIST_ROWS[case].items():
        monkeypatch.setenv(name, value)
    output = run(caplog)
    hits = _home_hits(home)
    assert not hits, f"plaintext secret remains in generated HOME files: {hits}"
    assert KEY not in output, "secret in writer output"
    assert KEY not in caplog.text, "secret in writer log"
    if case in ("fresh", "rewrite", "proxy", "old-plist-only"):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with contextlib.redirect_stdout(io.StringIO()):
            config.load_llm_keys_from_stores(_GROQ_CFG)
        assert os.environ.get("GROQ_API_KEY") == KEY, "key lost when plist is rewritten"


@pytest.mark.parametrize(
    "var",
    [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ],
)
def test_proxy_login_is_dropped_with_a_warning_naming_the_variable(
    var, monkeypatch, plist_env, caplog
):
    """The host survives (downloads behind the proxy still work), the login does not."""
    home, agents, _store, run = plist_env
    from smartmemory_app import setup

    monkeypatch.setenv(var, "http://probe-user:hunter2pw@proxy.example.test:3128")
    output = run(caplog)
    for plist in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME):
        assert _plist_env(agents, plist)[var] == "http://proxy.example.test:3128"
    assert not _home_hits(home, b"hunter2pw") and not _home_hits(home, b"probe-user")
    assert var in caplog.text and var in output
    for text in (caplog.text, output):
        assert "hunter2pw" not in text and "probe-user" not in text


def test_login_free_proxy_and_no_proxy_list_are_copied_unchanged(
    monkeypatch, plist_env, caplog
):
    _home, agents, _store, run = plist_env
    from smartmemory_app import setup

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,*.corp.example.test")
    output = run(caplog)
    env = _plist_env(agents, setup.PLIST_NAME)
    assert env["HTTPS_PROXY"] == "http://proxy.example.test:3128"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,*.corp.example.test"
    assert "holds a login" not in output and "proxy login" not in caplog.text


# Review r2 H1, verbatim: a comma in a scheme-less proxy password.
def test_proxy_password_with_comma(monkeypatch, plist_env, caplog):
    home, agents, store, run = plist_env
    password = "Canary,Comma73"
    monkeypatch.setenv("HTTPS_PROXY", f"probe:{password}@127.0.0.1:9")
    run(caplog)
    leaked = bool(_home_hits(home, password.encode()))
    assert not leaked, "proxy password survives in generated plist files"


_PROXY_HOST = "proxy.example.test:3128"
# id -> (variable, value, expected plist value). The copy rule comes from the variable:
# a *_PROXY / ALL_PROXY value is one URL whose login is dropped, whatever the password
# holds. NO_PROXY and HOME never hold a login and are copied as they are. Every login
# here contains "Canary" or "probe-user", and neither may reach a file.
_PROXY_RULE_ROWS = {
    "with-scheme": (
        "HTTPS_PROXY",
        f"http://probe-user:PwCanary@{_PROXY_HOST}",
        f"http://{_PROXY_HOST}",
    ),
    "no-scheme": ("https_proxy", f"probe-user:PwCanary@{_PROXY_HOST}", _PROXY_HOST),
    "slash": (
        "HTTP_PROXY",
        f"http://probe-user:Pw/Canary@{_PROXY_HOST}",
        f"http://{_PROXY_HOST}",
    ),
    "comma-no-scheme": (
        "http_proxy",
        f"probe-user:Pw,Canary@{_PROXY_HOST}",
        _PROXY_HOST,
    ),
    "comma-scheme": (
        "ALL_PROXY",
        f"socks5h://probe-user:Pw,Canary@{_PROXY_HOST}",
        f"socks5h://{_PROXY_HOST}",
    ),
    "at-sign": (
        "all_proxy",
        f"http://probe-user:Pw@Canary@{_PROXY_HOST}",
        f"http://{_PROXY_HOST}",
    ),
    "colon": (
        "HTTPS_PROXY",
        f"https://probe-user:Pw:Canary@{_PROXY_HOST}",
        f"https://{_PROXY_HOST}",
    ),
    "percent-escaped": (
        "HTTPS_PROXY",
        f"http://probe-user:Pw%2CCanary%40x@{_PROXY_HOST}",
        f"http://{_PROXY_HOST}",
    ),
    "user-only": (
        "HTTPS_PROXY",
        f"http://probe-userCanary@{_PROXY_HOST}",
        f"http://{_PROXY_HOST}",
    ),
    "scheme-inside-login": (
        "https_proxy",
        f"probe-user:pa://Canary@{_PROXY_HOST}",
        _PROXY_HOST,
    ),
    "path-and-query": (
        "HTTP_PROXY",
        f"http://probe-user:PwCanary@{_PROXY_HOST}/p?a=1&b=2",
        f"http://{_PROXY_HOST}/p?a=1&b=2",
    ),
    "no-login": ("HTTPS_PROXY", f"http://{_PROXY_HOST}", f"http://{_PROXY_HOST}"),
    "no-proxy-upper": (
        "NO_PROXY",
        "localhost,127.0.0.1,user@corp.example.test",
        "localhost,127.0.0.1,user@corp.example.test",
    ),
    "no-proxy-lower": (
        "no_proxy",
        "localhost,*.corp.example.test",
        "localhost,*.corp.example.test",
    ),
}


@pytest.mark.parametrize("case", sorted(_PROXY_RULE_ROWS))
def test_plist_env_copy_rule_comes_from_the_variable(
    case, monkeypatch, plist_env, caplog
):
    home, agents, _store, run = plist_env
    from smartmemory_app import setup

    var, value, expected = _PROXY_RULE_ROWS[case]
    monkeypatch.setenv(var, value)
    output = run(caplog)
    for plist in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME):
        assert _plist_env(agents, plist)[var] == expected
    assert not _home_hits(home, b"Canary") and not _home_hits(home, b"probe-user")
    for text in (caplog.text, output):
        assert "Canary" not in text and "probe-user" not in text
    has_login = expected != value
    assert (var in caplog.text) is has_login and (
        "holds a login" in output
    ) is has_login


def test_home_path_is_copied_as_it_is(monkeypatch, plist_env, caplog):
    """HOME is a path: an "@" or "," in it is not a login."""
    home, agents, _store, run = plist_env
    from smartmemory_app import setup

    odd_home = f"{home}/user@corp,x"
    monkeypatch.setenv("HOME", odd_home)
    output = run(caplog)
    assert _plist_env(agents, setup.PLIST_NAME)["HOME"] == odd_home
    assert "holds a login" not in output


def test_a_variable_without_a_plist_copy_rule_is_refused():
    from smartmemory_app.setup import _plist_env_value

    with pytest.raises(ValueError, match="SOME_TOKEN"):
        _plist_env_value("SOME_TOKEN", "value")


def _old_plist(agents, *names, key=KEY):
    for name in names:
        (agents / name).write_bytes(
            plistlib.dumps({"EnvironmentVariables": {"GROQ_API_KEY": key}})
        )


def test_old_key_in_only_the_worker_plist_is_migrated(plist_env, caplog):
    from smartmemory_app import setup

    _home, agents, store, run = plist_env
    _old_plist(agents, setup.WORKER_PLIST_NAME)
    run(caplog)
    assert store["GROQ_API_KEY"] == KEY
    for name in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME):
        assert "GROQ_API_KEY" not in _plist_env(agents, name)


def test_unreadable_old_plist_is_not_a_key_source(plist_env, caplog):
    from smartmemory_app import setup

    _home, agents, store, run = plist_env
    (agents / setup.PLIST_NAME).write_bytes(b"not a plist")
    run(caplog)
    assert "GROQ_API_KEY" not in store


def test_current_env_key_wins_over_the_old_plist_key(monkeypatch, plist_env, caplog):
    from smartmemory_app import setup

    _home, agents, store, run = plist_env
    _old_plist(agents, setup.PLIST_NAME, setup.WORKER_PLIST_NAME, key="old-plist-key-1")
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    run(caplog)
    assert store["GROQ_API_KEY"] == KEY


@pytest.mark.parametrize("cause", ["set-raises", "accepts-but-returns-nothing"])
def test_key_that_cannot_be_migrated_keeps_the_old_plists(
    cause, monkeypatch, plist_env, caplog
):
    """No rewrite when the secure store cannot take the key: the old files still hold the only copy."""
    from smartmemory_app import setup

    _home, agents, store, run = plist_env
    _old_plist(agents, setup.PLIST_NAME, setup.WORKER_PLIST_NAME)
    before = {
        n: (agents / n).read_bytes()
        for n in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME)
    }
    if cause == "set-raises":

        def refuse(service, name, value):
            raise RuntimeError("keychain locked")

        monkeypatch.setattr("keyring.set_password", refuse)
    else:
        monkeypatch.setattr("keyring.set_password", lambda service, name, value: None)
    monkeypatch.setattr(setup, "_read_env_from_profile", lambda name: "")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert setup._install_launchd_plist() is True
    assert {n: (agents / n).read_bytes() for n in before} == before
    assert "left as they are" in output.getvalue()
    assert KEY not in output.getvalue()


# Review r2 H2, verbatim: an env-present key whose storage fails.
def test_env_present_unmigratable_key_keeps_legacy_files(
    monkeypatch,
    plist_env,
    caplog,
):
    from smartmemory_app import config, setup

    home, agents, store, run = plist_env
    names = (setup.PLIST_NAME, setup.WORKER_PLIST_NAME)
    _old_plist(agents, *names)
    before = {n: (agents / n).read_bytes() for n in names}
    monkeypatch.setenv("GROQ_API_KEY", KEY)

    def refuse(*args):
        raise RuntimeError("credential store locked")

    monkeypatch.setattr("keyring.set_password", refuse)
    monkeypatch.setattr(setup, "_read_env_from_profile", lambda name: "")
    run(caplog)
    preserved = all((agents / n).read_bytes() == before[n] for n in names)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with contextlib.redirect_stdout(io.StringIO()):
        config.load_llm_keys_from_stores(_GROQ_CFG)
    reloaded = bool(os.environ.get("GROQ_API_KEY"))
    assert preserved or reloaded, "only persistent copy was replaced without migration"


NEW_KEY = "sk-test-NEWKEY456"
# id -> (key in this shell, key in the old plists, store accepts, key in the profile,
# expected result). "kept" means both plists stay byte-identical. "rewritten" means
# the plists lose the key and the daemon's loader finds the named key afterwards.
_KEY_INVARIANT_ROWS = {
    "env-same-store-fails": (KEY, KEY, False, "", "kept"),
    "env-other-store-fails": (NEW_KEY, KEY, False, "", "kept"),
    "env-empty-legacy-store-fails": ("", KEY, False, "", "kept"),
    "env-same-store-ok": (KEY, KEY, True, "", KEY),
    "env-other-store-ok": (NEW_KEY, KEY, True, "", NEW_KEY),
    "env-empty-legacy-store-ok": ("", KEY, True, "", KEY),
    "env-store-fails-profile-has-it": (KEY, KEY, False, KEY, KEY),
    "env-store-fails-no-old-key": (KEY, "", False, "", ""),
}


def _assert_key_invariant(expect, agents, before, output, monkeypatch):
    from smartmemory_app import config, setup

    assert KEY not in output and NEW_KEY not in output
    if expect == "kept":
        assert {n: (agents / n).read_bytes() for n in before} == before
        assert "left as they are" in output
        return
    for name in (setup.PLIST_NAME, setup.WORKER_PLIST_NAME):
        assert "GROQ_API_KEY" not in _plist_env(agents, name)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with contextlib.redirect_stdout(io.StringIO()):
        config.load_llm_keys_from_stores(_GROQ_CFG)
    assert os.environ.get("GROQ_API_KEY", "") == expect


@pytest.mark.parametrize("case", sorted(_KEY_INVARIANT_ROWS))
def test_plist_rewrite_never_drops_the_only_persistent_key(
    case, monkeypatch, plist_env, caplog
):
    from smartmemory_app import setup

    home, agents, _store, run = plist_env
    env_key, old_key, store_ok, profile_key, expect = _KEY_INVARIANT_ROWS[case]
    names = (setup.PLIST_NAME, setup.WORKER_PLIST_NAME)
    if old_key:
        _old_plist(agents, *names, key=old_key)
    before = {n: (agents / n).read_bytes() for n in names if (agents / n).exists()}
    if env_key:
        monkeypatch.setenv("GROQ_API_KEY", env_key)
    if not store_ok:

        def refuse(*args):
            raise RuntimeError("credential store locked")

        monkeypatch.setattr("keyring.set_password", refuse)
    if profile_key:
        (home / ".zshrc").write_text(f'export GROQ_API_KEY="{profile_key}"\n')
    output = run(caplog)
    _assert_key_invariant(expect, agents, before, output, monkeypatch)
    if case == "env-store-fails-no-old-key":
        assert "set in this shell only" in output


@pytest.mark.parametrize("store_ok", [False, True], ids=["store-fails", "store-ok"])
def test_key_kept_with_enter_in_setup_never_drops_the_old_plist_key(
    store_ok, monkeypatch, plist_env, caplog, tmp_path
):
    """sm setup: the shell has the key, the user presses Enter to keep it, then the
    plists are rewritten. Enter persists nothing, so the rewrite is the check."""
    from unittest.mock import patch

    from smartmemory_app import setup

    _home, agents, _store, run = plist_env
    names = (setup.PLIST_NAME, setup.WORKER_PLIST_NAME)
    _old_plist(agents, *names)
    before = {n: (agents / n).read_bytes() for n in names}
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    if not store_ok:

        def refuse(*args):
            raise RuntimeError("credential store locked")

        monkeypatch.setattr("keyring.set_password", refuse)
    prompts = ["groq", "", "openai", "sm", str(tmp_path / "data")]
    with (
        patch("smartmemory_app.setup._ensure_spacy"),
        patch("smartmemory_app.setup._ensure_lazy_models"),
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.config.save_config"),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("click.confirm", return_value=False),
        patch("click.prompt", side_effect=prompts) as prompt,
    ):
        setup_output = _captured(setup._setup_local)
    assert prompt.call_count == len(prompts)
    output = setup_output + run(caplog)
    _assert_key_invariant(
        KEY if store_ok else "kept", agents, before, output, monkeypatch
    )


_BASE_IDS = {
    "SMARTMEMORY_TEAM_ID": "ReviewTeamCanary73",
    "SMARTMEMORY_LLM_PROVIDER": "ReviewProviderCanary73",
    "SMARTMEMORY_LLM_MODEL": "ReviewModelCanary73",
    "SMARTMEMORY_EMBEDDING_PROVIDER": "ReviewEmbedCanary73",
    "SMARTMEMORY_WORKSPACE_ID": "ReviewWorkspaceCanary73",
    "OPENAI_MODEL": "ReviewOpenaiModelCanary73",
    "GROQ_API_KEY": KEY,
}
# case -> (env overrides, member the value is planted in besides doctor and logs).
# message-key, message-id and numeric-id are the review's failing rows. The rest cover
# the class: a key of any length, a config-only id, escaped and non-ASCII ids, a
# tenant id, a key-named variable the LLM list does not know.
_ZIP_ROWS = {
    "ordinary": {},
    "message-key": {"message": "key"},
    "message-id": {"message": "team"},
    "json-log": {"env": {"SMARTMEMORY_TEAM_ID": 'ReviewTeam"Canary\\73'}, "json": True},
    "numeric-id": {"env": {"SMARTMEMORY_TEAM_ID": "8319472056"}},
    "numeric-id-short": {"env": {"SMARTMEMORY_TEAM_ID": "4412"}, "message": "team"},
    "short-key": {"env": {"GROQ_API_KEY": "k9Zq"}, "message": "key"},
    "three-char-key": {"env": {"GROQ_API_KEY": "k9Z"}, "message": "key"},
    "config-only-id": {
        "env": {"SMARTMEMORY_TEAM_ID": ""},
        "cfg_team": "CfgTeamCanary73",
    },
    "non-ascii-json-log": {
        "env": {"SMARTMEMORY_TEAM_ID": "\u00c9quipeCanary73"},
        "json": True,
    },
    "tenant-id": {"env": {"TENANT_ID": "ReviewTenantCanary73"}, "message": "tenant"},
    "other-key-name": {
        "env": {"VENDOR_API_TOKEN": "vendor-token-Canary73"},
        "message": "vendor",
    },
}


def _decoded_strings(text):
    found = []
    for line in text.splitlines():
        if "INFO {" in line:
            try:
                found += [
                    str(v) for v in json.loads(line.split("INFO ", 1)[1]).values()
                ]
            except ValueError:
                pass
    return found


@pytest.mark.parametrize("case", sorted(_ZIP_ROWS))
def test_zip_paths(case, monkeypatch, isolate):
    from smartmemory_app import bug_report, config
    from smartmemory_app import support_diagnostics as support

    home, row = isolate, _ZIP_ROWS[case]
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: home / "cli-debug.log")
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: SimpleNamespace(browser_info="probe environment"),
    )
    vals = {**_BASE_IDS, **row.get("env", {})}
    for name, value in vals.items():
        monkeypatch.setenv(name, value)
    team = row.get("cfg_team") or vals["SMARTMEMORY_TEAM_ID"]
    cfg = config.SmartMemoryConfig(
        team_id=team,
        llm_model=vals["SMARTMEMORY_LLM_MODEL"],
        llm_provider=vals["SMARTMEMORY_LLM_PROVIDER"],
        embedding_provider=vals["SMARTMEMORY_EMBEDDING_PROVIDER"],
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    planted = {n: v for n, v in vals.items() if v}
    if row.get("cfg_team"):
        planted["config team_id"] = team
    payload = {"team_id": team, "api": vals["GROQ_API_KEY"]}
    log = "2026-10-17 12:00:00 INFO " + (
        json.dumps(payload) if row.get("json") else " ".join(planted.values())
    )
    for name in ("cli-debug.log", "daemon.log"):
        (home / name).write_text(log)
    message = {
        "key": vals["GROQ_API_KEY"],
        "team": team,
        "tenant": vals.get("TENANT_ID", ""),
        "vendor": vals.get("VENDOR_API_TOKEN", ""),
    }.get(row.get("message"), "probe message")
    path = support.write_support_bundle(
        home / "support.zip", "probe " + " ".join(planted.values()), message=message
    )
    extracted = home / "unzipped"
    with zipfile.ZipFile(path) as z:
        z.extractall(extracted)
    assert {p.name for p in extracted.iterdir()} >= {
        "doctor.txt",
        "config.txt",
        "daemon.log",
    }
    leaks = []
    for member in extracted.iterdir():
        text = member.read_text()
        decoded = _decoded_strings(text)
        for name, value in planted.items():
            if name in ("SMARTMEMORY_LLM_PROVIDER",) and value.lower() in ("none",):
                continue
            escaped = json.dumps(value)[1:-1]
            if value in text or escaped in text or any(value in d for d in decoded):
                leaks.append((member.name, name))
    assert not leaks, f"raw identifier or secret in extracted zip: {leaks}"
    if row.get("message"):
        assert (extracted / "message.txt").read_text() != message


# Review r2 H3, verbatim: a bare short alphabetic team id as the whole message.
def test_bare_short_team_id_is_not_in_message_member(monkeypatch, isolate):
    from smartmemory_app import bug_report, config
    from smartmemory_app import support_diagnostics as support

    identifier = "abc"
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", identifier)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.SmartMemoryConfig(
            team_id=identifier,
            data_dir=str(isolate / "data"),
        ),
    )
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: isolate / "cli-debug.log")
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: SimpleNamespace(
            browser_info="probe environment",
        ),
    )
    path = support.write_support_bundle(
        isolate / "support.zip", "probe", message=identifier
    )
    with zipfile.ZipFile(path) as archive:
        leaked = identifier.encode() in archive.read("message.txt")
    assert not leaked, "supplied team identifier survives in message.txt"


# Review r3 H3, verbatim: a JSON escape right before a short supplied id. The
# serialized text holds a backslash and an ``n``, the decoded text a newline.
def test_json_escaped_boundary_hides_short_supplied_id(monkeypatch, isolate):
    from smartmemory_app import bug_report, config
    from smartmemory_app import support_diagnostics as support

    identifier = "abc"  # same synthetic identifier as r2 H3
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", identifier)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.SmartMemoryConfig(
            team_id=identifier,
            data_dir=str(isolate / "data"),
        ),
    )
    monkeypatch.setattr(
        bug_report,
        "debug_log_path",
        lambda: isolate / "cli-debug.log",
    )
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: SimpleNamespace(browser_info="probe environment"),
    )
    message = json.dumps({"note": "\n" + identifier})
    path = support.write_support_bundle(
        isolate / "support.zip",
        "probe",
        message=message,
    )
    with zipfile.ZipFile(path) as archive:
        body = archive.read("message.txt").decode()
    decoded = json.loads(body)["note"]
    leaked = identifier in body and bool(
        re.search(rf"(?<!\w){re.escape(identifier)}(?!\w)", decoded)
    )
    assert not leaked, "supplied identifier survives the JSON archive boundary"


# id -> (where the value is supplied, the value, the text it is planted in). Every
# length (1, 2, 3, 4+), character class (letters, digits, mixed, a symbol) and
# placement (the whole text, prose, quoted, a JSON record) appears at least once.
# The exact-value pass masks a supplied value at any length and any class.
_EXACT_ROWS = {
    "len1-whole": ("SMARTMEMORY_TEAM_ID", "x", "{v}"),
    "len2-prose": ("SMARTMEMORY_TEAM_ID", "ab", "my team {v} cannot sync"),
    "len3-alpha-quoted": ("SMARTMEMORY_TEAM_ID", "abc", "team_id='{v}' failed"),
    "len3-numeric-json": ("SMARTMEMORY_TEAM_ID", "417", '{{"team_id": "{v}"}}'),
    "len3-symbol-json-escaped": ("SMARTMEMORY_TEAM_ID", 'q"z', "{j}"),
    "len4-alpha-prose": ("SMARTMEMORY_TEAM_ID", "abcd", "team {v}, again"),
    "mixed-short-whole": ("SMARTMEMORY_TEAM_ID", "a1", "{v}"),
    "numeric-long-quoted": ("SMARTMEMORY_TEAM_ID", "8319472056", 'team "{v}"'),
    "upper-case-prose": ("SMARTMEMORY_TEAM_ID", "Qx", "team QX and {v}"),
    "workspace-len2-prose": ("SMARTMEMORY_WORKSPACE_ID", "wq", "workspace {v} is gone"),
    "tenant-len1-quoted": ("TENANT_ID", "t", 'tenant "{v}"'),
    "config-only-len1": ("config team_id", "z", "{v}"),
    "key-alpha-len2-prose": ("GROQ_API_KEY", "kq", "my key {v} was refused"),
    "key-numeric-whole": ("GROQ_API_KEY", "73", "{v}"),
}
_LABEL = re.compile(r"\$[A-Z_]+ \(env\)|<[a-z_]+>")


def _token_hit(value, text):
    """``value`` as a whole token in ``text`` once every mask label is removed."""
    bare = _LABEL.sub(" ", text)
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", bare, re.IGNORECASE)


@pytest.mark.parametrize("case", sorted(_EXACT_ROWS))
def test_every_supplied_value_is_masked_at_any_length(case, monkeypatch, isolate):
    from smartmemory_app import bug_report, config
    from smartmemory_app import support_diagnostics as support

    where, value, template = _EXACT_ROWS[case]
    text = template.format(v=value, j=json.dumps({"team_id": value}))
    team = value if where in ("SMARTMEMORY_TEAM_ID", "config team_id") else ""
    if where != "config team_id":
        monkeypatch.setenv(where, value)
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.SmartMemoryConfig(team_id=team, data_dir=str(isolate / "data")),
    )
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: isolate / "cli-debug.log")
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: SimpleNamespace(browser_info=f"probe environment {text}"),
    )
    for name in ("cli-debug.log", "daemon.log"):
        (isolate / name).write_text(f"2026-10-17 12:00:00 INFO {text}\n")
    path = support.write_support_bundle(isolate / "support.zip", text, message=text)
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name).decode() for name in archive.namelist()}
    assert members["message.txt"] != text
    escaped = json.dumps(value)[1:-1]
    leaks = [
        name
        for name, body in members.items()
        for form in {value, escaped}
        if _token_hit(form, body)
        or any(_token_hit(value, d) for d in _decoded_strings(body))
    ]
    assert not leaks, f"supplied value survives in zip members: {sorted(set(leaks))}"


def test_short_value_is_masked_only_as_a_whole_token():
    from smartmemory_app.doctor import mask_exact_values

    out = mask_exact_values("abc abcdef xabc 'abc' ABC", [("abc", "<id>")])
    assert out == "<id> abcdef xabc '<id>' <id>"


def test_overlapping_supplied_values_are_masked_whole():
    """One label never leaves part of an overlapping value behind."""
    from smartmemory_app.doctor import mask_exact_values

    out = mask_exact_values("xxabcdefyy", [("abcdef", "<a>"), ("xxabc", "<b>")])
    assert "abc" not in out and "def" not in out and "xx" not in out


# id -> (the JSON string content as the archive member holds it, the supplied value,
# where it is supplied, masked or kept). One row per escape before and one after the
# value: the six JSON escapes, an escaped non-ASCII letter (``\u00e9``, an e-acute as
# json.dumps writes it) and a ``\u`` escape of a newline. Lengths 1, 3 and 8, letters
# and digits, from the env, the config or both. The ``kept`` rows pin that a short
# id still masks only as a whole token: ``abc`` stays in ``xabcx`` and ``abcdef``,
# after an escape too.
_ESCAPE_ROWS = {
    "before-n-len1-alpha": (r"\n{v}", "q", "env", "masked"),
    "before-t-len3-numeric": (r"\t{v}", "417", "config", "masked"),
    "before-r-len1-numeric": (r"\r{v}", "7", "env", "masked"),
    "before-quote-len3-alpha": (r"\"{v}", "abc", "env", "masked"),
    "before-backslash-len8-alpha": (r"\\{v}", "qzxwvuts", "config", "masked"),
    "before-slash-len3-alpha": (r"\/{v}", "abc", "config", "masked"),
    "before-e-acute-len3-alpha": (r"\u00e9{v}", "abc", "env", "masked"),
    "before-u000a-len3-alpha": (r"\u000a{v}", "abc", "both", "masked"),
    "after-n-len8-numeric": (r"{v}\n", "83194726", "env", "masked"),
    "after-t-len1-alpha": (r"{v}\t", "q", "config", "masked"),
    "after-r-len3-alpha": (r"{v}\r", "abc", "both", "masked"),
    "after-quote-len1-numeric": (r"{v}\"", "7", "env", "masked"),
    "after-backslash-len3-numeric": (r"{v}\\", "417", "config", "masked"),
    "after-slash-len8-alpha": (r"{v}\/", "qzxwvuts", "both", "masked"),
    "after-e-acute-len3-alpha": (r"{v}\u00e9", "abc", "env", "masked"),
    "after-u000a-len1-numeric": (r"{v}\u000a", "7", "config", "masked"),
    "kept-inside-word": ("x{v}x", "abc", "both", "kept"),
    "kept-prefix-of-word": ("{v}def", "abc", "both", "kept"),
    "kept-escape-then-longer-word": (r"\n{v}def", "abc", "both", "kept"),
}


@pytest.mark.parametrize("case", sorted(_ESCAPE_ROWS))
def test_supplied_value_next_to_an_escape_is_masked(case, monkeypatch, isolate):
    from smartmemory_app import bug_report, config
    from smartmemory_app import support_diagnostics as support

    content, value, source, expect = _ESCAPE_ROWS[case]
    body = '{"note": "' + content.format(v=value) + '"}'
    if source in ("env", "both"):
        monkeypatch.setenv("SMARTMEMORY_TEAM_ID", value)
    team = value if source in ("config", "both") else ""
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: config.SmartMemoryConfig(team_id=team, data_dir=str(isolate / "data")),
    )
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: isolate / "cli-debug.log")
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: SimpleNamespace(browser_info="probe environment"),
    )
    path = support.write_support_bundle(isolate / "support.zip", body, message=body)
    with zipfile.ZipFile(path) as archive:
        members = {
            name: archive.read(name).decode() for name in ("message.txt", "doctor.txt")
        }
    for name, member in members.items():
        note = json.loads(member)["note"]
        if expect == "kept":
            assert member == body, f"{name}: a longer word holding the id was masked"
            continue
        assert value.lower() not in _LABEL.sub(" ", member).lower(), name
        assert not _token_hit(value, note), f"{name}: id is a token once decoded"


@pytest.mark.parametrize(
    "name,value,masked",
    [
        ("GROQ_API_KEY", "4096", True),
        ("VENDOR_SECRET", "123456", True),
        ("SERVICE_PASSWORD", "2468", True),
        ("MAX_TOKEN", "4096", False),  # a token budget is a setting
        ("VENDOR_TOKEN", "tok-abc", True),
    ],
)
def test_numeric_values_under_secret_names(name, value, masked, monkeypatch):
    from smartmemory_app import config
    from smartmemory_app.support_diagnostics import supplied_values

    monkeypatch.setattr(config, "load_config", lambda: config.SmartMemoryConfig())
    found = [v for v, _label in supplied_values({name: value})]
    assert (value in found) is masked


def _captured(callable_, *args):
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        callable_(*args)
    return captured.getvalue()


@pytest.mark.parametrize(
    "shell,profile",
    [("/bin/zsh", ".zshrc"), ("/bin/bash", ".bashrc"), ("/bin/sh", ".profile")],
)
def test_profile_write_failure_does_not_echo_key(shell, profile, monkeypatch, isolate):
    from smartmemory_app.setup import _persist_env_var

    monkeypatch.setenv("SHELL", shell)
    (isolate / profile).mkdir()  # a directory where the profile file should be
    output = _captured(_persist_env_var, "GROQ_API_KEY", KEY)
    assert KEY not in output, "setup failure echoes full provider key"
    assert 'export GROQ_API_KEY="<your key>"' in output


def test_keyring_exception_does_not_log_key(monkeypatch, caplog):
    from smartmemory_app.setup import _daemon_can_read_key

    monkeypatch.setattr("keyring.get_password", lambda *a: None)

    def broken(*args):
        raise RuntimeError(KEY)

    monkeypatch.setattr("keyring.set_password", broken)
    assert not _daemon_can_read_key("GROQ_API_KEY", KEY)
    assert caplog.text and KEY not in caplog.text, (
        "keychain exception leaks provider key"
    )


def test_windows_credential_failure_does_not_log_key(monkeypatch, caplog):
    import sys

    from smartmemory_app.setup import _persist_env_var

    def broken(*args):
        raise RuntimeError(f"cannot store {KEY}")

    monkeypatch.setattr("keyring.set_password", broken)
    monkeypatch.setattr(sys, "platform", "win32")
    output = _captured(_persist_env_var, "GROQ_API_KEY", KEY)
    assert caplog.text and KEY not in caplog.text + output


def test_launchctl_stderr_carrying_the_key_is_not_printed(
    monkeypatch, plist_env, caplog
):
    from smartmemory_app import daemon

    _home, _agents, _store, run = plist_env
    monkeypatch.setenv("GROQ_API_KEY", KEY)

    def failing(cmd, **kw):
        code = 1 if cmd[1] == "load" else 0
        return subprocess.CompletedProcess(cmd, code, "", f"load failed for {KEY}")

    monkeypatch.setattr(daemon, "_run_command", failing)
    from smartmemory_app import setup

    output = _captured(setup._install_launchd_plist)
    assert "load failed" in output and KEY not in output


def test_remote_auth_failure_does_not_echo_key(monkeypatch):
    import click

    from smartmemory_app.setup import _setup_remote

    def refuse(*args, **kwargs):
        raise RuntimeError(f"rejected {KEY}")

    monkeypatch.setattr("httpx.get", refuse)
    with pytest.raises(click.ClickException) as raised:
        _setup_remote(KEY, "https://api.example.test")
    assert "Remote authentication failed" in raised.value.message
    assert KEY not in raised.value.message


@pytest.mark.parametrize(
    "key,expect",
    [
        ("sk-test-PROBE123", "...E123"),  # a prefix is never shown
        ("gsk_0123456789abcdefghij", "...ghij"),
        ("short-key", "***"),
    ],
)
def test_key_prompt_hint_shows_at_most_the_last_four_characters(key, expect):
    from smartmemory_app.setup import _masked_key_hint

    hint = _masked_key_hint(key)
    assert hint.endswith(expect) or hint == expect
    assert key[:8] not in hint and len(hint) <= 7


# redact_credentials with supplied secrets: every printed form, any length.
_SECRET_ROWS = [
    ("plain", "boom: k9Zq refused", "k9Zq"),
    ("one-char", "boom: z refused", "z"),
    ("no-known-prefix", "rejected abc123def456ghi789jkl", "abc123def456ghi789jkl"),
    ("json-escaped", 'detail {"k": "a\\"b\\\\c9"}', 'a"b\\c9'),
    ("url-quoted", "GET /x?k=p%40ss%2Fword9", "p@ss/word9"),
    ("with-spaces-trimmed", "key was  spaced9Key  here", " spaced9Key "),
]


@pytest.mark.parametrize(
    "label,message,secret", _SECRET_ROWS, ids=[r[0] for r in _SECRET_ROWS]
)
def test_redact_credentials_masks_the_exact_supplied_secret(label, message, secret):
    from smartmemory_app.diagnostics import redact_credentials

    out = redact_credentials(message, (secret,))
    assert secret.strip() not in out and "<redacted>" in out


def test_redact_credentials_without_secrets_is_unchanged():
    from smartmemory_app.diagnostics import redact_credentials

    assert redact_credentials("plain error text", ()) == "plain error text"
    assert redact_credentials("plain error text") == "plain error text"


# ── CLI-POSTLAND-FU-1 H1: a typed credential is never echoed ────────────────

_TYPED = "postland-synthetic-not-a-real-credential-112233"
_STORED = "postland-stored-not-a-real-credential-445566"
_KEY_ENVVARS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
# (id, provider, a key is already stored)
PROMPT_ROWS = [
    (f"{provider}-{mode}", provider, mode == "replace")
    for provider in _KEY_ENVVARS
    for mode in ("fresh", "replace")
]


@pytest.mark.parametrize(
    "provider, stored",
    [row[1:] for row in PROMPT_ROWS],
    ids=[row[0] for row in PROMPT_ROWS],
)
def test_provider_key_prompt_does_not_echo_the_typed_key(
    provider, stored, tmp_path, monkeypatch
):
    import click
    from click.testing import CliRunner

    from smartmemory_app import setup

    home = tmp_path / "prompt-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for name in _KEY_ENVVARS.values():
        monkeypatch.delenv(name, raising=False)
    if stored:
        monkeypatch.setenv(_KEY_ENVVARS[provider], _STORED)
    vault: dict = {}
    keyring = pytest.importorskip("keyring")
    monkeypatch.setattr(keyring, "get_password", lambda *a: vault.get(a))
    monkeypatch.setattr(keyring, "set_password", lambda *a: vault.update({a[:2]: a[2]}))

    command = click.command()(setup._ask_local_choices)
    result = CliRunner().invoke(
        command, input=f"n\n{provider}\n{_TYPED}\nlocal\nsm\n\n"
    )

    assert result.exit_code == 0, result.output
    assert _TYPED not in result.output
    assert _STORED not in result.output


def test_every_credential_prompt_in_the_app_hides_its_input():
    """The class: any ``click.prompt`` whose text asks for a key, token, password or
    secret passes ``hide_input=True``."""
    import ast
    from pathlib import Path

    import smartmemory_app

    asks = ("api key", "api_key", "token", "password", "secret")
    bad = []
    for path in sorted(Path(smartmemory_app.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            is_prompt = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "prompt"
            )
            if not is_prompt:
                continue
            text = " ".join(
                c.value
                for arg in node.args[:1]
                for c in ast.walk(arg)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ).lower()
            hidden = any(
                k.arg == "hide_input"
                and isinstance(k.value, ast.Constant)
                and k.value.value is True
                for k in node.keywords
            )
            if any(a in text for a in asks) and not hidden:
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, bad
