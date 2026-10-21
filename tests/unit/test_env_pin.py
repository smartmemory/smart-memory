"""CLI-ENV-DETECT-1: per-project use/ignore/override of inherited SMARTMEMORY_* vars."""

import logging
import os
import tomllib

import pytest
from click.testing import CliRunner

from smartmemory_app import env_pin, env_sources
from smartmemory_app.config import (
    SmartMemoryConfig,
    config_path,
    load_config,
    save_config,
)

CANARY = "CANARY-VALUE-5c2e"


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    """A non-git project dir as cwd, an isolated config dir, no SMARTMEMORY_* set."""
    for name in list(os.environ):
        if name.startswith("SMARTMEMORY_"):
            monkeypatch.delenv(name)
    (tmp_path / "xdg").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(env_pin, "_applied", False)
    monkeypatch.setattr(env_sources, "_pre_pin_names", None)
    save_config(SmartMemoryConfig(mode="local"))  # the project's own config
    return proj


def _reset():
    env_pin._applied = False
    env_sources._pre_pin_names = None


def _invoke(args, env, tty=False, input=None, monkeypatch=None):
    from smartmemory_app import cli as cli_module

    _reset()
    if monkeypatch is not None:
        monkeypatch.setattr(env_pin, "_stdin_is_tty", lambda: tty)
    return CliRunner().invoke(cli_module.cli, args, env=env, input=input)


# ── apply: one row per decision shape ─────────────────────────────────────────

APPLY_ROWS = [
    # (decision, stored override, shell value, expected value after apply)
    ("ignore", None, "remote", None),
    ("override", "local", "remote", "local"),
    ("override", "local", None, "local"),  # a project value stands without the shell
    ("use", None, "remote", "remote"),
    (
        "override",
        "bogus",
        "remote",
        "remote",
    ),  # invalid stored value: shell kept, WARNING
    (None, None, "remote", "remote"),  # undecided
]


@pytest.mark.parametrize("decision,stored,shell,expected", APPLY_ROWS)
def test_apply_decisions(project, caplog, decision, stored, shell, expected):
    if decision == "override" and stored == "bogus":
        raw = {
            "projects": {
                env_sources.project_key(project): {
                    "env": {"SMARTMEMORY_MODE": "override"},
                    "env_override": {"SMARTMEMORY_MODE": "bogus"},
                }
            }
        }
        env_pin._write_table(config_path(), raw)
    elif decision:
        env_pin.set_decision("SMARTMEMORY_MODE", decision, stored)
    environ = {} if shell is None else {"SMARTMEMORY_MODE": shell}
    with caplog.at_level(logging.WARNING):
        env_pin.apply_project_env(environ)
    assert environ.get("SMARTMEMORY_MODE") == expected
    assert ("no valid stored value" in caplog.text) == (stored == "bogus")
    assert "bogus" not in caplog.text and "remote" not in caplog.text


def test_decision_is_per_project(project, tmp_path):
    env_pin.set_decision("SMARTMEMORY_MODE", "ignore")
    other = tmp_path / "other"
    other.mkdir()
    environ = {"SMARTMEMORY_MODE": "remote"}
    env_pin.apply_project_env(environ, project_dir=other)
    assert environ == {"SMARTMEMORY_MODE": "remote"}


SET_ERROR_ROWS = [
    ("SMARTMEMORY_API_KEY", "override", "sk", "holds a secret"),
    ("SMARTMEMORY_SYNC_TOKEN", "override", "t", "holds a secret"),
    ("SMARTMEMORY_MODE", "override", "cloud", "must be local or remote"),
    ("SMARTMEMORY_DAEMON_PORT", "override", "70000", "must be a port number"),
    ("SMARTMEMORY_TEAM_ID", "override", "", "needs a value"),
    ("SMARTMEMORY_MODE", "maybe", None, "decision must be one of"),
]


@pytest.mark.parametrize("name,decision,value,message", SET_ERROR_ROWS)
def test_set_decision_rejects(name, decision, value, message):
    with pytest.raises(ValueError, match=message):
        env_pin.set_decision(name, decision, value)
    assert "projects" not in tomllib.loads(config_path().read_text())


def test_store_lives_in_user_config_and_survives_save_config(project):
    env_pin.set_decision("SMARTMEMORY_TEAM_ID", "override", "team-x")
    env_pin.set_decision("SMARTMEMORY_MODE", "ignore")
    cfg = load_config()
    cfg.llm_provider = "groq"
    save_config(cfg)  # `smartmemory config llm_provider groq` rewrites the file
    raw = tomllib.loads(config_path().read_text())
    assert raw["local"]["llm_provider"] == "groq"
    assert raw["projects"][str(project.resolve())] == {
        "env": {"SMARTMEMORY_TEAM_ID": "override", "SMARTMEMORY_MODE": "ignore"},
        "env_override": {"SMARTMEMORY_TEAM_ID": "team-x"},
    }
    env_pin.set_decision("SMARTMEMORY_TEAM_ID", "use")  # drops the stored value
    assert env_pin.clear_decisions("SMARTMEMORY_MODE") == ["SMARTMEMORY_MODE"]
    raw = tomllib.loads(config_path().read_text())
    assert raw["projects"][str(project.resolve())] == {
        "env": {"SMARTMEMORY_TEAM_ID": "use"}
    }
    assert env_pin.clear_decisions() == ["SMARTMEMORY_TEAM_ID"]
    assert "projects" not in tomllib.loads(config_path().read_text())
    assert raw["smartmemory"]["mode"] == "local"


# ── CLI: resolution as seen by `smartmemory config` ───────────────────────────

RESOLVE_ROWS = [
    # (pin, override value, shell SMARTMEMORY_MODE, mode shown)
    (None, None, "remote", "remote"),  # today: env beats the config file
    ("use", None, "remote", "remote"),
    ("ignore", None, "remote", "local"),  # as if unset: the project's config file
    ("override", "local", "remote", "local"),
]


@pytest.mark.parametrize("pin,value,shell,shown", RESOLVE_ROWS)
def test_cli_resolution(monkeypatch, pin, value, shell, shown):
    if pin:
        env_pin.set_decision("SMARTMEMORY_MODE", pin, value)
    result = _invoke(["config"], {"SMARTMEMORY_MODE": shell}, monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert f"mode              = {shown}" in result.output
    warned = "WARNING: SmartMemory is using settings inherited" in result.output
    assert warned == (pin is None)


def test_non_tty_warns_once_never_prompts_never_prints_values(monkeypatch, project):
    home = project.parent / "home"
    (home / ".zshrc").write_text(f'export SMARTMEMORY_MODE="{CANARY}"\n')
    monkeypatch.setenv("SHELL", "/bin/zsh")
    env = {"SMARTMEMORY_MODE": "remote", "SMARTMEMORY_TEAM_ID": CANARY}
    result = _invoke(["config"], env, tty=False, input=None, monkeypatch=monkeypatch)
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if "WARNING" in ln]
    assert lines == [
        "WARNING: SmartMemory is using settings inherited from your shell that this "
        "project has not reviewed: SMARTMEMORY_MODE (~/.zshrc:1), SMARTMEMORY_TEAM_ID "
        "(source unknown). Run `smartmemory env` in a terminal to use, ignore or "
        "override them."
    ]
    assert "Choice" not in result.output
    assert CANARY not in result.output.replace(f"team_id           = {CANARY}", "")


def test_real_non_tty_stdin_does_not_hang(project):
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "SMARTMEMORY_ENV_CHECK"}
    env["SMARTMEMORY_MODE"] = "remote"
    proc = subprocess.run(
        [sys.executable, "-m", "smartmemory_app", "config"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=project,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("WARNING: SmartMemory is using settings inherited") == 1


QUIET_ROWS = [
    # (command, SMARTMEMORY_ENV_CHECK, warns)
    ("config", None, True),
    ("setup", None, True),
    ("tour", None, True),
    ("lifecycle", None, False),
    ("doctor", None, False),
    ("env", None, False),
    ("config", "0", False),
]


@pytest.mark.parametrize("command,switch,warns", QUIET_ROWS)
def test_which_commands_warn(monkeypatch, capsys, command, switch, warns):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    if switch is not None:
        monkeypatch.setenv("SMARTMEMORY_ENV_CHECK", switch)
    env_pin.check_inherited(command, is_tty=False)
    assert ("WARNING" in capsys.readouterr().err) == warns


def test_tty_prompt_ignore_then_next_command_uses_project_config(monkeypatch):
    env = {"SMARTMEMORY_MODE": "remote", "SMARTMEMORY_DATA_DIR": "/elsewhere"}
    first = _invoke(
        ["config"], env, tty=True, input="ignore\n", monkeypatch=monkeypatch
    )
    assert first.exit_code == 0, first.output
    assert "SMARTMEMORY_DATA_DIR  source unknown" in first.output
    assert "mode              = local" in first.output  # applied to this command too
    second = _invoke(["config"], env, tty=True, input="", monkeypatch=monkeypatch)
    assert second.exit_code == 0
    assert "Choice" not in second.output and "WARNING" not in second.output
    assert "mode              = local" in second.output
    assert "data_dir          = ~/.smartmemory" in second.output


def test_tty_prompt_each_and_secret_cannot_be_overridden(monkeypatch, project):
    env = {"SMARTMEMORY_API_KEY": CANARY, "SMARTMEMORY_MODE": "remote"}
    # override for all: the secret falls back to a use/ignore question first.
    result = _invoke(
        ["config"],
        env,
        tty=True,
        input="override\nignore\nbad\nlocal\n",
        monkeypatch=monkeypatch,
    )
    assert result.exit_code == 0, result.output
    assert "SMARTMEMORY_API_KEY holds a secret" in result.output
    assert "must be local or remote" in result.output
    assert CANARY not in result.output
    entry = tomllib.loads(config_path().read_text())["projects"][str(project.resolve())]
    assert entry == {
        "env": {"SMARTMEMORY_API_KEY": "ignore", "SMARTMEMORY_MODE": "override"},
        "env_override": {"SMARTMEMORY_MODE": "local"},
    }
    env_pin.clear_decisions()
    result = _invoke(
        ["config"], env, tty=True, input="each\nuse\nignore\n", monkeypatch=monkeypatch
    )
    assert result.exit_code == 0, result.output
    entry = tomllib.loads(config_path().read_text())["projects"][str(project.resolve())]
    assert entry == {
        "env": {"SMARTMEMORY_API_KEY": "use", "SMARTMEMORY_MODE": "ignore"}
    }


def test_env_command_set_list_reset_and_config_alias(monkeypatch):
    env = {"SMARTMEMORY_MODE": "remote", "SMARTMEMORY_TEAM_ID": CANARY}
    r = _invoke(
        ["env", "set", "SMARTMEMORY_TEAM_ID", "ignore"], env, monkeypatch=monkeypatch
    )
    assert r.exit_code == 0 and "SMARTMEMORY_TEAM_ID: ignore" in r.output
    r = _invoke(
        ["env", "set", "SMARTMEMORY_MODE", "override"], env, monkeypatch=monkeypatch
    )
    assert r.exit_code != 0 and "override needs a VALUE" in r.output
    r = _invoke(["env", "set", "PATH", "ignore"], env, monkeypatch=monkeypatch)
    assert r.exit_code != 0 and "only SMARTMEMORY_* settings" in r.output
    for args in (["env"], ["config", "env"]):
        r = _invoke(args, env, monkeypatch=monkeypatch)
        assert r.exit_code == 0, r.output
        assert "SMARTMEMORY_TEAM_ID  ignore" in r.output
        assert "SMARTMEMORY_MODE     undecided" in r.output
        assert "smartmemory env set NAME use|ignore" in r.output
        assert CANARY not in r.output and "remote" not in r.output
    r = _invoke(["env", "reset"], env, monkeypatch=monkeypatch)
    assert r.exit_code == 0 and "Forgot: SMARTMEMORY_TEAM_ID" in r.output


def test_user_facing_strings_have_no_em_dash_or_semicolon():
    import inspect
    import re

    src = inspect.getsource(env_pin)
    strings = re.findall(r'(?<![rb])"((?:[^"\\\n]|\\.)*)"', src)
    prose = [s for s in strings if " " in s]
    assert prose, "no strings found"
    assert not [s for s in prose if "—" in s or ";" in s]


def test_config_set_never_writes_inherited_values_into_the_file(monkeypatch):
    """Live run 2026-10-17: `config mode local` persisted an inherited team id."""
    env = {"SMARTMEMORY_TEAM_ID": "team-other", "SMARTMEMORY_DATA_DIR": "/elsewhere"}
    r = _invoke(["config", "mode", "local"], env, monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    raw = tomllib.loads(config_path().read_text())
    assert (
        raw["remote"]["team_id"] == "" and raw["local"]["data_dir"] == "~/.smartmemory"
    )
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "team-other")
    assert load_config().team_id == "team-other"
    assert load_config(apply_env=False).team_id == ""
