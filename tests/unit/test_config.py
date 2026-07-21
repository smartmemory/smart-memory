"""Unit tests for smartmemory_app.config — DIST-LITE-5."""
import os
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_app.config import (
    SmartMemoryConfig,
    UnconfiguredError,
    _detect_and_migrate,
    config_path,
    get_api_key,
    is_configured,
    load_config,
    save_config,
    set_api_key,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect config path to a temp dir and clear all relevant env vars.

    Only create the XDG_CONFIG_HOME parent, not the smartmemory/ subdir —
    save_config() must create that with mode=0o700, so it must not pre-exist.
    """
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Clear all env vars that affect config
    for var in (
        "SMARTMEMORY_MODE",
        "SMARTMEMORY_API_KEY",
        "SMARTMEMORY_API_URL",
        "SMARTMEMORY_TEAM_ID",
        "SMARTMEMORY_DATA_DIR",
        "SMARTMEMORY_LLM_PROVIDER",
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path


# ── is_configured ─────────────────────────────────────────────────────────────


def test_is_configured_false_no_file_no_env():
    assert is_configured() is False


def test_is_configured_true_via_env(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    assert is_configured() is True


def test_is_configured_true_via_config_file():
    save_config(SmartMemoryConfig(mode="local"))
    assert is_configured() is True


# ── load_config ───────────────────────────────────────────────────────────────


def test_load_config_missing_file_returns_unconfigured():
    cfg = load_config()
    assert cfg.mode is None


def test_load_config_reads_mode():
    save_config(SmartMemoryConfig(mode="remote", api_key_set=True, team_id="t1"))
    cfg = load_config()
    assert cfg.mode == "remote"
    assert cfg.api_key_set is True
    assert cfg.team_id == "t1"


def test_load_config_reads_local_fields():
    save_config(SmartMemoryConfig(
        mode="local",
        coreference=True,
        llm_provider="groq",
        llm_model="llama3",
        data_dir="~/custom",
    ))
    cfg = load_config()
    assert cfg.coreference is True
    assert cfg.llm_provider == "groq"
    assert cfg.llm_model == "llama3"
    assert cfg.data_dir == "~/custom"


def test_load_config_invalid_toml_warns_and_returns_unconfigured():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not [ valid toml !!!")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = load_config()
    assert cfg.mode is None
    assert any("Failed to parse config" in str(warning.message) for warning in w)


def test_load_config_invalid_mode_in_file_warns_and_returns_unconfigured():
    """A typo in mode= inside the config file warns and returns mode=None."""
    import tomli_w
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({"smartmemory": {"mode": "remtoe"}}), encoding="utf-8")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = load_config()
    assert cfg.mode is None
    assert any("Invalid mode" in str(warning.message) for warning in w)


def test_load_config_invalid_mode_env_var_raises(monkeypatch):
    """SMARTMEMORY_MODE with an invalid value raises ValueError immediately."""
    monkeypatch.setenv("SMARTMEMORY_MODE", "remtoe")
    with pytest.raises(ValueError, match="SMARTMEMORY_MODE"):
        load_config()


def test_load_config_env_var_overrides_file(monkeypatch):
    save_config(SmartMemoryConfig(mode="local"))
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    cfg = load_config()
    assert cfg.mode == "remote"


def test_load_config_api_key_env_sets_api_key_set(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test")
    cfg = load_config()
    assert cfg.api_key_set is True


def test_load_config_data_dir_env_overrides_file(monkeypatch):
    save_config(SmartMemoryConfig(mode="local", data_dir="~/.smartmemory"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", "/tmp/mydata")
    cfg = load_config()
    assert cfg.data_dir == "/tmp/mydata"


# ── save_config ───────────────────────────────────────────────────────────────


def test_save_config_creates_directory_with_restricted_perms():
    cfg = SmartMemoryConfig(mode="local")
    save_config(cfg)
    path = config_path()
    assert path.exists()
    # Directory should be owner-accessible only (0o700)
    import stat
    mode = path.parent.stat().st_mode & 0o777
    assert mode == 0o700


def test_save_config_omits_smartmemory_table_when_unconfigured():
    save_config(SmartMemoryConfig(mode=None))
    import tomllib
    raw = tomllib.loads(config_path().read_text())
    assert "smartmemory" not in raw


# ── _detect_and_migrate ───────────────────────────────────────────────────────


def test_detect_and_migrate_always_writes_local_config():
    """Local deps are now hard dependencies, so migration always succeeds."""
    result = _detect_and_migrate()
    assert result is True
    cfg = load_config()
    assert cfg.mode == "local"


# ── get_api_key ───────────────────────────────────────────────────────────────


def test_get_api_key_returns_env_var(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_from_env")
    key = get_api_key()
    assert key == "sk_from_env"


def test_get_api_key_warns_when_neither_env_nor_keychain(monkeypatch):
    with (
        patch("keyring.get_password", return_value=None),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        key = get_api_key()
    assert key == ""
    assert any("No API key found" in str(warning.message) for warning in w)


def test_get_api_key_warns_when_keyring_raises(monkeypatch):
    with (
        patch("keyring.get_password", side_effect=Exception("no keychain")),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        key = get_api_key()
    assert key == ""
    assert any("No API key found" in str(warning.message) for warning in w)


def test_get_api_key_env_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_env")
    with patch("keyring.get_password", return_value="sk_keychain") as mock_kr:
        key = get_api_key()
    assert key == "sk_env"
    mock_kr.assert_not_called()  # keychain never consulted when env var is set


# ── set_api_key ───────────────────────────────────────────────────────────────


def test_set_api_key_warns_does_not_raise_when_keychain_unavailable():
    with (
        patch("keyring.set_password", side_effect=Exception("no keychain")),
        warnings.catch_warnings(record=True) as w,
    ):
        warnings.simplefilter("always")
        set_api_key("sk_test")  # must not raise
    assert any("OS keychain unavailable" in str(warning.message) for warning in w)


# ── local OpenAI-compatible provider translation (ollama/lmstudio/localai) ──────
# An OpenAI-compatible URL IS the OpenAI path; picking such a provider must set the
# env vars the core extraction path already reads, with no provider-specific code.


def test_ollama_provider_sets_openai_compatible_env(monkeypatch):
    """llm_provider=ollama → OPENAI_BASE_URL (localhost default), a placeholder
    OPENAI_API_KEY (so llm_key_present() flips true and the daemon runs Tier-2),
    and SMARTMEMORY_LLM_MODEL from llm_model — all consumed by the OpenAI path."""
    from smartmemory_app.config import llm_key_present
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SMARTMEMORY_LLM_MODEL", "dolphin-phi:latest")
    with patch.dict(os.environ, {}, clear=False):  # roll back load_config's setdefault
        for v in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "SMARTMEMORY_LLM_MODEL"):
            os.environ.pop(v, None)
        os.environ["SMARTMEMORY_LLM_MODEL"] = "dolphin-phi:latest"
        load_config()
        assert os.environ["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
        assert os.environ["OPENAI_API_KEY"]  # non-empty placeholder
        assert os.environ["SMARTMEMORY_LLM_MODEL"] == "dolphin-phi:latest"
        assert llm_key_present() is True  # daemon will run Tier-2, not keyless Tier-1


def test_ollama_never_clobbers_user_env(monkeypatch):
    """A real OPENAI_API_KEY / OPENAI_BASE_URL the user set must survive (setdefault)."""
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "ollama")
    with patch.dict(os.environ, {}, clear=False):
        os.environ["OPENAI_API_KEY"] = "sk-real-user-key"
        os.environ["OPENAI_BASE_URL"] = "https://my-proxy.example/v1"
        load_config()
        assert os.environ["OPENAI_API_KEY"] == "sk-real-user-key"
        assert os.environ["OPENAI_BASE_URL"] == "https://my-proxy.example/v1"


def test_llm_base_url_override_wins_over_default(monkeypatch):
    """A configured llm_base_url (e.g. remote ollama host) overrides the localhost default."""
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SMARTMEMORY_LLM_BASE_URL", "http://gpu-box.lan:11434/v1")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_BASE_URL", None)
        load_config()
        assert os.environ["OPENAI_BASE_URL"] == "http://gpu-box.lan:11434/v1"


def test_cloud_provider_is_noop(monkeypatch):
    """Cloud providers / 'none' must NOT get a placeholder key or a base_url — those
    users supply real provider keys. (Guards against hijacking real OpenAI usage.)"""
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "groq")
    with patch.dict(os.environ, {}, clear=False):
        for v in ("OPENAI_BASE_URL", "OPENAI_API_KEY"):
            os.environ.pop(v, None)
        load_config()
        assert "OPENAI_BASE_URL" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ


def test_llm_base_url_round_trips_through_save(monkeypatch):
    """llm_base_url persists via save_config → load_config."""
    save_config(SmartMemoryConfig(mode="local", llm_provider="ollama",
                                  llm_model="qwen2.5:7b", llm_base_url="http://h:11434/v1"))
    with patch.dict(os.environ, {}, clear=False):
        cfg = load_config()
        assert cfg.llm_base_url == "http://h:11434/v1"
        assert cfg.llm_model == "qwen2.5:7b"
