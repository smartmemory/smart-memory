"""Secret surfaces from the CLI-FU-SWEEP-1a r1 review (H1 to H4), replayed as real tests.

Every value here is synthetic. No test reads a real credential store, runs launchctl
or opens a network connection: the credential store is an in-memory dict and the
launchctl process boundary is replaced.
"""

import contextlib
import io
import os
import plistlib
import subprocess

import pytest

KEY = "sk-test-PROBE123"
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

    if case == "rewrite":
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
    if "GROQ_API_KEY" in _PLIST_ROWS[case]:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with contextlib.redirect_stdout(io.StringIO()):
            config.load_llm_keys_from_stores()
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
