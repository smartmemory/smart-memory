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
