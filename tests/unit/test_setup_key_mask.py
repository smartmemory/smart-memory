"""sm setup never prints or echoes the provider key (WRAPPER-2.0-SETUP-KEY-MASK-PORT-1).

Ported from main d12e6cec (key-masking hunks) and 86701dbb (hide_input hunk).
Every value is synthetic. No real profile, keychain or network is touched.
"""

from pathlib import Path

import click
import pytest

from smartmemory_app import setup
from smartmemory_app.diagnostics import redact_credentials

KEY = "zz-probe-key-NOT-a-known-shape-7f3a9c"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_profile_write_failure_never_echoes_key(monkeypatch, capsys):
    def boom(self, *a, **kw):
        raise OSError(f"disk refused export GROQ_API_KEY={KEY}")

    monkeypatch.setattr(Path, "write_text", boom)
    monkeypatch.setattr(
        "builtins.open", lambda *a, **kw: (_ for _ in ()).throw(OSError(f"nope {KEY}"))
    )
    setup._persist_env_var("GROQ_API_KEY", KEY)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "Add this line to" in out.out


def test_redact_masks_exact_secret_in_every_form():
    from urllib.parse import quote

    secret = 'a"b c/d'
    for form in (secret, quote(secret, safe=""), 'a\\"b c/d'):
        assert secret not in redact_credentials(f"x {form} y", (secret,))
        assert form not in redact_credentials(f"x {form} y", (secret,))


def test_key_hint_shows_suffix_only():
    assert setup._key_hint(KEY) == "..." + KEY[-4:]
    assert KEY[:8] not in setup._key_hint(KEY)
    assert setup._key_hint("short") == "***"


@pytest.mark.parametrize("has_existing", [True, False])
def test_key_prompt_hides_input_and_hint_has_no_prefix(
    monkeypatch, capsys, has_existing
):
    calls = []

    def fake_prompt(text, **kw):
        calls.append((text, kw))
        if "LLM provider" in text:
            return "groq"
        return kw.get("default", "")

    monkeypatch.setattr(click, "prompt", fake_prompt)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)
    monkeypatch.setattr(setup, "_persist_env_var", lambda n, v: None)
    if has_existing:
        monkeypatch.setenv("GROQ_API_KEY", KEY)
    try:
        setup._setup_local()
    except Exception:
        pass  # only the prompts matter, later setup steps are out of scope
    key_calls = [(t, kw) for t, kw in calls if "GROQ_API_KEY" in t]
    assert key_calls, calls
    assert all(kw.get("hide_input") is True for _, kw in key_calls)
    assert all(KEY[:8] not in t and KEY not in t for t, _ in key_calls)


def _row_keyring_warning(monkeypatch, caplog, tmp_path, capsys):
    def fake_prompt(text, **kw):
        if "LLM provider" in text:
            return "groq"
        return KEY if kw.get("hide_input") else kw.get("default", "")

    def refuse(*args):
        raise RuntimeError(f"keychain refused {KEY}")

    monkeypatch.setattr(click, "prompt", fake_prompt)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)
    monkeypatch.setattr(setup, "_persist_env_var", lambda n, v: None)
    monkeypatch.setattr("keyring.set_password", refuse)
    try:
        setup._setup_local()
    except Exception:
        pass  # only the keyring warning matters, later steps are out of scope
    assert "OS credential store unavailable" in caplog.text
    return caplog.text


def _row_remote_auth_failure(monkeypatch, caplog, tmp_path, capsys):
    def refuse(*args, **kwargs):
        raise RuntimeError(f"rejected {KEY}")

    monkeypatch.setattr("httpx.get", refuse)
    with pytest.raises(click.ClickException) as raised:
        setup._setup_remote(KEY, "https://api.example.test")
    assert "Remote authentication failed" in raised.value.message
    return raised.value.message


def _row_launchctl_stderr(monkeypatch, caplog, tmp_path, capsys):
    import subprocess

    from smartmemory_app import config, daemon

    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(setup, "LAUNCH_AGENTS_DIR", agents)
    cfg = config.SmartMemoryConfig(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setenv("GROQ_API_KEY", KEY)

    def failing(cmd, **kw):
        code = 1 if cmd[1] == "load" else 0
        return subprocess.CompletedProcess(cmd, code, "", f"load failed for {KEY}")

    monkeypatch.setattr(daemon, "_run_command", failing)
    setup._install_launchd_plist()
    out = capsys.readouterr()
    assert "load failed" in out.out
    return out.out + out.err


_ROWS = {
    "keyring-warning": _row_keyring_warning,
    "remote-auth-failure": _row_remote_auth_failure,
    "launchctl-stderr": _row_launchctl_stderr,
}


@pytest.mark.parametrize("name", sorted(_ROWS))
def test_error_text_carrying_the_key_is_never_printed(
    name, monkeypatch, caplog, tmp_path, capsys
):
    shown = _ROWS[name](monkeypatch, caplog, tmp_path, capsys)
    assert KEY not in shown, f"{name} leaks the provider key"
