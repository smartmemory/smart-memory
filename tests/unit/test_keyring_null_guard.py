"""CLI-KEYRING-NULL-GUARD-1: setup under a scratch HOME never reaches the OS keychain."""

import subprocess
import sys
from pathlib import Path

import pytest

from smartmemory_app import config, keyring_guard, setup

NULL = "keyring.backends.null.Keyring"
KEY = "test_keyring_guard_secret_value"


@pytest.fixture
def os_store_trap(monkeypatch, tmp_path):
    """Stand in for the OS backend: any call to it is a test failure."""
    import keyring

    calls = []

    def trap(*args):
        calls.append(args)
        raise AssertionError(f"keyring reached the OS backend: {args[:2]}")

    monkeypatch.setattr(keyring, "set_password", trap)
    monkeypatch.setattr(keyring, "get_password", trap)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    return calls


def _wizard_key_step(monkeypatch):
    """The real `sm setup` key prompt under a scratch HOME (posix shell-profile path)."""
    import click
    from click.testing import CliRunner

    monkeypatch.setattr(sys, "platform", "linux")
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        # setenv before delenv: delenv on an absent name records nothing to restore, so the
        # wizard's own os.environ write would leak out of the test (provider env leak guard).
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    command = click.command()(setup._ask_local_choices)
    result = CliRunner().invoke(command, input=f"n\ngroq\n{KEY}\nlocal\nsm\n\n")
    assert result.exit_code == 0, result.output
    assert KEY not in result.output


def _daemon_can_read(monkeypatch):
    assert setup._daemon_can_read_key("GROQ_API_KEY", KEY) is False


def _set_api_key(monkeypatch):
    with pytest.warns(UserWarning, match="OS keychain unavailable"):
        config.set_api_key(KEY)


# One row per write site (setup.py wizard, setup.py daemon read-back, config.py).
SITES = {
    "wizard_key_step": _wizard_key_step,
    "daemon_can_read_key": _daemon_can_read,
    "config_set_api_key": _set_api_key,
}
MODES = {
    "null_backend": {"PYTHON_KEYRING_BACKEND": NULL},
    "no_keyring_flag": {"SMARTMEMORY_NO_KEYRING": "1"},
}


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("mode", sorted(MODES))
def test_disabled_keyring_skips_every_write_site(
    site, mode, monkeypatch, os_store_trap, caplog
):
    # 3 write sites x 2 disable modes = 6 rows, each site once per mode.
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)
    monkeypatch.delenv("SMARTMEMORY_NO_KEYRING", raising=False)
    for name, value in MODES[mode].items():
        monkeypatch.setenv(name, value)
    with caplog.at_level("WARNING"):
        SITES[site](monkeypatch)
    assert os_store_trap == []
    assert any("keychain" in r.getMessage().lower() for r in caplog.records)
    assert KEY not in caplog.text


def test_set_secret_skip_is_loud_and_returns_false(monkeypatch, os_store_trap, caplog):
    monkeypatch.setenv("SMARTMEMORY_NO_KEYRING", "1")
    with caplog.at_level("WARNING"):
        assert keyring_guard.set_secret("GROQ_API_KEY", KEY) is False
    warning = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning) == 1
    assert "GROQ_API_KEY" in warning[0].getMessage()
    assert KEY not in caplog.text


def test_enabled_keyring_still_writes(monkeypatch, keyring_writes_enabled):
    import keyring

    written = []
    monkeypatch.setattr(keyring, "set_password", lambda *a: written.append(a))
    assert keyring_guard.set_secret("GROQ_API_KEY", KEY) is True
    assert written == [("smartmemory", "GROQ_API_KEY", KEY)]


@pytest.mark.parametrize(
    "env",
    [{"PYTHON_KEYRING_BACKEND": NULL}, {"SMARTMEMORY_NO_KEYRING": "1"}],
    ids=["null_backend", "no_keyring_flag"],
)
def test_fresh_process_under_scratch_home_never_loads_an_os_backend(env, tmp_path):
    """No in-process patching: a real process, scratch HOME, real keyring package."""
    home = tmp_path / "scratch-home"
    home.mkdir()
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from smartmemory_app import keyring_guard, setup\n"
        "assert keyring_guard.set_secret('GROQ_API_KEY', 'k') is False\n"
        "assert setup._daemon_can_read_key('GROQ_API_KEY', 'k') is False\n"
        "backends = [m for m in sys.modules if m.startswith('keyring.backends')]\n"
        "print('OS_BACKENDS', [m for m in backends if m != 'keyring.backends.null'])\n"
    )
    run_env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/zsh",
        **env,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert "OS_BACKENDS []" in result.stdout
