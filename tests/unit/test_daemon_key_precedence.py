"""CLI-DAEMON-KEY-PRECEDENCE-1: the daemon loads only the selected provider's key.

Tester repro (smartmemory 1.5.28/1.5.29): llm_provider groq, GROQ_API_KEY exported in the
shell, a stale OPENAI_API_KEY left in the keychain. The daemon logged
"Loaded OPENAI_API_KEY from keychain", then "no usable provider configured" while
``sm status`` showed groq. Precedence is: explicit provider > exported env > keychain >
shell profile, and a credential is never loaded for a provider that is not selected.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os

import pytest

from smartmemory_app import config, setup
from smartmemory_app.config import (
    LLM_KEY_ENV_VARS,
    PROVIDER_KEY_ENV,
    SmartMemoryConfig,
)

G, OA, A, D, M = (
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # setenv then delenv: monkeypatch records the name, so a key the loader sets is removed at teardown
    for name in (*LLM_KEY_ENV_VARS, "LLM_API_KEY", "SMARTMEMORY_LLM_PROVIDER"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _load(monkeypatch, provider, *, env=None, keychain=None, profile=None, model=""):
    """Run the daemon's key loader against fake stores. Returns (loaded names, stdout)."""
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "keyring.get_password", lambda service, key: (keychain or {}).get(key)
    )
    monkeypatch.setattr(
        setup, "_read_env_from_profile", lambda n: (profile or {}).get(n, "")
    )
    cfg = SmartMemoryConfig(mode="local", llm_provider=provider, llm_model=model)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        loaded = config.load_llm_keys_from_stores(cfg)
    return loaded, out.getvalue()


def _present(*names):
    return {n for n in LLM_KEY_ENV_VARS if os.environ.get(n)} == set(names)


# id -> (provider, env, keychain, profile, expected env names afterwards, expected values)
# One row per class member: every provider, every store, and each precedence pair.
_ROWS = {
    # the tester's repro: stale openai in keychain, groq selected, groq not in the daemon's env
    "groq-stale-openai-keychain": ("groq", {}, {OA: "sk-stale"}, {}, (), {}),
    # exported groq plus stale openai: env wins, openai never loaded
    "groq-env-and-stale-openai": (
        "groq",
        {G: "gsk-env"},
        {OA: "sk-stale"},
        {},
        (G,),
        {G: "gsk-env"},
    ),
    "groq-env-beats-keychain": (
        "groq",
        {G: "gsk-env"},
        {G: "gsk-kc"},
        {},
        (G,),
        {G: "gsk-env"},
    ),
    "groq-keychain-beats-profile": (
        "groq",
        {},
        {G: "gsk-kc"},
        {G: "gsk-pf"},
        (G,),
        {G: "gsk-kc"},
    ),
    "groq-profile-only": (
        "groq",
        {},
        {},
        {G: "gsk-pf", OA: "sk-pf"},
        (G,),
        {G: "gsk-pf"},
    ),
    "groq-keychain-has-all": (
        "groq",
        {},
        {G: "g", OA: "o", A: "a", D: "d", M: "m"},
        {},
        (G,),
        {G: "g"},
    ),
    "openai-ignores-groq": (
        "openai",
        {},
        {OA: "sk-kc", G: "gsk-kc"},
        {},
        (OA,),
        {OA: "sk-kc"},
    ),
    "anthropic-own-key-only": ("anthropic", {}, {A: "a", OA: "o"}, {}, (A,), {A: "a"}),
    "deepseek-own-key-only": ("deepseek", {}, {D: "d", OA: "o"}, {}, (D,), {D: "d"}),
    "gemini-own-key-only": ("gemini", {}, {M: "m", OA: "o"}, {}, (M,), {M: "m"}),
    "provider-case-insensitive": ("GROQ", {}, {G: "g", OA: "o"}, {}, (G,), {G: "g"}),
    # providers that use no cloud key load none: provider none means no LLM at all
    "none-loads-nothing": ("none", {}, {OA: "o", G: "g"}, {OA: "o"}, (), {}),
    "ollama-loads-nothing": ("ollama", {}, {OA: "o", G: "g"}, {}, (), {}),
    "lmstudio-loads-nothing": ("lmstudio", {}, {OA: "o"}, {}, (), {}),
    "claude-agent-loads-nothing": ("claude-agent", {}, {OA: "o", A: "a"}, {}, (), {}),
    # a provider name this code does not know keeps the old load-everything behaviour
    "unknown-provider-keeps-legacy": (
        "weird",
        {},
        {OA: "o", G: "g"},
        {},
        (OA, G),
        {OA: "o", G: "g"},
    ),
}


@pytest.mark.parametrize("case", sorted(_ROWS))
def test_loader_scopes_keys_to_the_selected_provider(case, monkeypatch):
    provider, env, keychain, profile, expected, values = _ROWS[case]
    # env rows put the exported key in the process env the daemon inherited
    _, out = _load(monkeypatch, provider, env=env, keychain=keychain, profile=profile)
    assert _present(*expected), (
        case,
        {n for n in LLM_KEY_ENV_VARS if os.environ.get(n)},
    )
    for name, value in values.items():
        assert os.environ[name] == value
    for secret in ("sk-stale", "gsk-env", "gsk-kc", "gsk-pf"):
        assert secret not in out  # names only, never values


def test_provider_key_table_is_the_one_source():
    """preflight's provider -> key map is the config one, not a second copy."""
    from smartmemory_app import preflight

    assert preflight._PROVIDER_KEY_ENV is PROVIDER_KEY_ENV
    assert set(PROVIDER_KEY_ENV.values()) <= set(LLM_KEY_ENV_VARS)


def test_missing_selected_key_warns_and_names_the_provider(monkeypatch, caplog):
    """Selected provider has no key in any store: WARNING says what was lost (no-silent-degradation)."""
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.config"):
        loaded, _ = _load(monkeypatch, "groq", keychain={OA: "sk-stale"})
    assert loaded == []
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "groq" in text and G in text and "keychain" in text
    assert "sk-stale" not in text


def test_selected_key_found_logs_no_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.config"):
        loaded, _ = _load(monkeypatch, "groq", keychain={G: "gsk-kc"})
    assert loaded == [G]
    assert not caplog.records


def test_providers_that_need_no_key_do_not_warn(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.config"):
        for provider in ("none", "ollama", "claude-agent"):
            _load(monkeypatch, provider, keychain={OA: "o"})
    assert not caplog.records


# llm_key_present() is the daemon's health answer: it counts the selected provider's key only.
_PRESENT_ROWS = {
    "groq-with-only-openai-in-env": ("groq", {OA: "o"}, False),
    "groq-with-groq": ("groq", {G: "g"}, True),
    "groq-with-both": ("groq", {G: "g", OA: "o"}, True),
    "openai-with-only-groq": ("openai", {G: "g"}, False),
    "none-with-key": ("none", {OA: "o"}, False),
    "unknown-provider-any-key": ("weird", {OA: "o"}, True),
    # a local server's placeholder OPENAI_API_KEY is what flips Tier-2 on (config._apply_local_llm_env)
    "ollama-placeholder": ("ollama", {OA: "local-llm-no-key"}, True),
}


@pytest.mark.parametrize("case", sorted(_PRESENT_ROWS))
def test_llm_key_present_counts_only_the_selected_provider(case, monkeypatch):
    provider, env, expected = _PRESENT_ROWS[case]
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        config,
        "load_config",
        # a local provider is "on" (not llm_off) only once setup picked a model
        lambda *a, **k: SmartMemoryConfig(
            mode="local",
            llm_provider=provider,
            llm_model="m" if provider == "ollama" else "",
        ),
    )
    assert config.llm_key_present() is expected


def test_tester_repro_stale_openai_never_reaches_the_route(monkeypatch, caplog):
    """The exact tester sequence: daemon boot with groq selected and a stale OpenAI keychain entry.

    Before the fix the daemon printed "Loaded OPENAI_API_KEY from keychain". Now OPENAI_API_KEY
    stays out of the process whatever the groq key's source, so the OpenAI route is unreachable.
    """
    monkeypatch.setattr(
        "keyring.get_password", lambda s, k: "sk-stale" if k == OA else None
    )
    monkeypatch.setattr(setup, "_read_env_from_profile", lambda n: "")
    monkeypatch.setattr(
        config,
        "load_config",
        lambda *a, **k: SmartMemoryConfig(mode="local", llm_provider="groq"),
    )
    out = io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        caplog.at_level(logging.WARNING, logger="smartmemory_app.config"),
    ):
        config.load_llm_keys_from_stores()
    assert "Loaded OPENAI_API_KEY" not in out.getvalue()
    assert not os.environ.get(OA)
    assert (
        config.llm_key_present() is False
    )  # truthful: groq is selected and has no key here
    assert any(G in r.getMessage() for r in caplog.records)
