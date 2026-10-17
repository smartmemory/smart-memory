"""CLI-ENV-DETECT-1 fix r1: a config that exists but cannot be read is never rewritten."""

import os

import pytest
from click.testing import CliRunner

from smartmemory_app import env_pin, env_sources
from smartmemory_app.config import SmartMemoryConfig, config_path, save_config
from smartmemory_app.env_sources import ConfigUnreadableError

GOOD = (
    '[smartmemory]\nmode = "remote"\n\n'
    '[remote]\napi_url = "https://example.invalid"\nteam_id = "team_keepme"\n'
)
BROKEN = GOOD + "\n[local\n"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("SMARTMEMORY_"):
            monkeypatch.delenv(name)
    (tmp_path / "xdg").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(env_pin, "_applied", False)
    monkeypatch.setattr(env_sources, "_pre_pin_names", None)
    monkeypatch.setattr(env_pin, "_stdin_is_tty", lambda: False)
    return tmp_path


def _put(kind):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "toml":
        path.write_text(BROKEN, encoding="utf-8")
    elif kind == "utf8":
        path.write_bytes(GOOD.encode() + b"\xff\xfe\n")
    elif kind == "chmod":
        path.write_text(GOOD, encoding="utf-8")
        path.chmod(0)
    return path


def _cli(args, env=None, tty=False):
    from smartmemory_app import cli as cli_module

    env_pin._applied = False
    env_sources._pre_pin_names = None
    env_pin._stdin_is_tty = lambda: tty
    return CliRunner().invoke(cli_module.cli, args, env=env, input="ignore\n")


def _raises(fn):
    def run():
        with pytest.raises(ConfigUnreadableError) as caught:
            fn()
        return caught.value.format_message()

    return run


def _cli_refuses(args):
    def run():
        result = _cli(args)
        assert result.exit_code != 0
        return result.output

    return run


def _prompt_continues():
    # A terminal, an inherited SMARTMEMORY_MODE, the answer typed: the command still runs.
    result = _cli(["config"], env={"SMARTMEMORY_MODE": "remote"}, tty=True)
    assert result.exit_code == 0, result.output
    assert "Config:" in result.output  # the user's own command ran
    assert "Choice" not in result.output  # asked nothing, saved nothing
    return result.output


# (row, file kind, action returning the text the user saw)
REFUSE_ROWS = [
    (
        "set_decision",
        "toml",
        _raises(lambda: env_pin.set_decision("SMARTMEMORY_MODE", "ignore")),
    ),
    ("env set", "toml", _cli_refuses(["env", "set", "SMARTMEMORY_MODE", "ignore"])),
    ("env reset", "toml", _cli_refuses(["env", "reset"])),
    ("clear_decisions", "toml", _raises(lambda: env_pin.clear_decisions())),
    (
        "carry_over",
        "toml",
        _raises(lambda: env_pin.carry_over_tables(config_path(), {})),
    ),
    (
        "save_config",
        "toml",
        _raises(lambda: save_config(SmartMemoryConfig(mode="local"))),
    ),
    ("first-command prompt", "toml", _prompt_continues),
    (
        "bad utf-8",
        "utf8",
        _raises(lambda: env_pin.set_decision("SMARTMEMORY_MODE", "ignore")),
    ),
    (
        "OSError",
        "chmod",
        _raises(lambda: env_pin.set_decision("SMARTMEMORY_MODE", "ignore")),
    ),
    (
        "OSError save_config",
        "chmod",
        _raises(lambda: save_config(SmartMemoryConfig(mode="local"))),
    ),
]


@pytest.mark.parametrize(
    "kind,action", [(k, a) for _, k, a in REFUSE_ROWS], ids=[r[0] for r in REFUSE_ROWS]
)
def test_unreadable_config_is_never_rewritten(kind, action):
    if kind == "chmod" and os.geteuid() == 0:
        pytest.skip("root reads mode 000 files")
    path = _put(kind)
    try:
        before = path.read_bytes() if kind != "chmod" else None
        seen = action()
        assert str(path) in seen  # one line naming the file to fix
        assert "Fix or remove it" in seen
        if kind == "toml":
            assert "line" in seen
        path.chmod(0o600)
        after = path.read_bytes()
        assert after == (before if before is not None else GOOD.encode())
        assert not list(path.parent.glob(".config-*"))  # no temp file left
    finally:
        path.chmod(0o600)


def test_missing_file_is_created_as_before():
    path = config_path()
    assert not path.exists()
    env_pin.set_decision("SMARTMEMORY_MODE", "ignore")
    assert "SMARTMEMORY_MODE" in path.read_text(encoding="utf-8")


def test_read_only_callers_keep_reading_empty(caplog):
    """The lenient reader is for readers that never write the file back."""
    path = _put("toml")
    assert env_sources.read_config_table(path) == {}
    assert env_pin.apply_project_env(environ={}, path=path) == {}
    assert path.read_text(encoding="utf-8") == BROKEN


def test_first_run_migration_refuses_unreadable_config():
    """An unreadable file reads as unconfigured: auto-migration must not overwrite it."""
    from smartmemory_app.config import _detect_and_migrate

    path = _put("toml")
    with pytest.warns(UserWarning, match="Fix or remove it"):
        assert _detect_and_migrate() is False
    assert path.read_text(encoding="utf-8") == BROKEN
