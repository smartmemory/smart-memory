"""CLI-ENV-DETECT-1: inherited SMARTMEMORY_* detection (names + sources, never values)."""

import dataclasses
import subprocess

import pytest
import tomli_w

from smartmemory_app import env_sources
from smartmemory_app.env_sources import InheritedVar, inherited_vars, rc_sources

CANARY_ENV = "CANARY-ENV-VALUE-7f3a"
CANARY_RC = "CANARY-RC-VALUE-91bd"


@pytest.fixture(autouse=True)
def _no_snapshot(monkeypatch):
    monkeypatch.setattr(env_sources, "_pre_pin_names", None)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_interface_shape():
    fields = {f.name: f.type for f in dataclasses.fields(InheritedVar)}
    assert fields == {"name": "str", "source": "str | None", "decision": "str | None"}
    assert InheritedVar.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        InheritedVar("SMARTMEMORY_MODE", None, None).name = "x"


# One row per assignment shape the lookup must recognise (or must not).
RC_ROWS = [
    # (shell, rc file, line, expected source or None)
    ("/bin/zsh", ".zshrc", "export SMARTMEMORY_MODE=remote", "~/.zshrc:2"),
    ("/bin/zsh", ".zshenv", "SMARTMEMORY_MODE=remote", "~/.zshenv:2"),
    ("/bin/zsh", ".zprofile", "  export SMARTMEMORY_MODE='remote'", "~/.zprofile:2"),
    ("/bin/bash", ".bashrc", "declare -x SMARTMEMORY_MODE=remote", "~/.bashrc:2"),
    (
        "/bin/bash",
        ".bash_profile",
        "typeset -x SMARTMEMORY_MODE=1",
        "~/.bash_profile:2",
    ),
    ("/bin/sh", ".profile", "export SMARTMEMORY_MODE=remote", "~/.profile:2"),
    (
        "/usr/local/bin/fish",
        ".config/fish/config.fish",
        "set -gx SMARTMEMORY_MODE remote",
        "~/.config/fish/config.fish:2",
    ),
    (
        "/usr/local/bin/fish",
        ".config/fish/conf.d/sm.fish",
        "set -Ux SMARTMEMORY_MODE remote",
        "~/.config/fish/conf.d/sm.fish:2",
    ),
    # Not assignments of this name: comment, longer name, mention in a string.
    ("/bin/zsh", ".zshrc", "# export SMARTMEMORY_MODE=remote", None),
    ("/bin/zsh", ".zshrc", "export SMARTMEMORY_MODE_X=remote", None),
    ("/bin/zsh", ".zshrc", 'echo "SMARTMEMORY_MODE=remote"', None),
    # A file of another shell is still found when the current shell has none.
    ("/bin/zsh", ".bashrc", "export SMARTMEMORY_MODE=remote", "~/.bashrc:2"),
]


@pytest.mark.parametrize("shell,rc,line,expected", RC_ROWS)
def test_rc_source_shapes(tmp_path, shell, rc, line, expected):
    _write(tmp_path / rc, f"# header\n{line}\n")
    assert (
        rc_sources({"SMARTMEMORY_MODE"}, tmp_path, shell=shell).get("SMARTMEMORY_MODE")
        == expected
    )


def test_last_assignment_in_load_order_wins(tmp_path):
    _write(tmp_path / ".zshenv", "export SMARTMEMORY_MODE=a\n")
    _write(
        tmp_path / ".zshrc",
        "x=1\nexport SMARTMEMORY_MODE=b\nexport SMARTMEMORY_MODE=c\n",
    )
    assert rc_sources({"SMARTMEMORY_MODE"}, tmp_path, "/bin/zsh") == {
        "SMARTMEMORY_MODE": "~/.zshrc:3"
    }


def test_current_shell_preferred_over_other_shells(tmp_path):
    _write(tmp_path / ".bashrc", "export SMARTMEMORY_MODE=a\n")
    _write(tmp_path / ".zshrc", "export SMARTMEMORY_MODE=b\n")
    assert rc_sources({"SMARTMEMORY_MODE"}, tmp_path, "/bin/bash") == {
        "SMARTMEMORY_MODE": "~/.bashrc:1"
    }


def test_inherited_vars_lists_prefix_only_sorted_with_sources(tmp_path):
    _write(tmp_path / ".zshrc", "export SMARTMEMORY_TEAM_ID=t\n")
    env = {
        "SHELL": "/bin/zsh",
        "SMARTMEMORY_TEAM_ID": "x",
        "SMARTMEMORY_MODE": "remote",
        "SMARTMEMORY_ENV_CHECK": "0",  # meta switch, never reported
        "GROQ_API_KEY": "k",
        "PATH": "/bin",
    }
    got = inherited_vars(environ=env, home=tmp_path, project_dir=tmp_path)
    assert got == [
        InheritedVar("SMARTMEMORY_MODE", None, None),
        InheritedVar("SMARTMEMORY_TEAM_ID", "~/.zshrc:1", None),
    ]


def test_no_vars_returns_empty(tmp_path):
    assert inherited_vars(environ={"PATH": "/bin"}, home=tmp_path) == []


def test_decisions_read_from_user_config_per_project(tmp_path):
    proj_a, proj_b = tmp_path / "a", tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    cfg = tmp_path / ".config" / "smartmemory" / "config.toml"
    key_a = env_sources.project_key(proj_a)
    _write(
        cfg,
        tomli_w.dumps(
            {
                "smartmemory": {"mode": "local"},
                "projects": {
                    key_a: {
                        "env": {
                            "SMARTMEMORY_MODE": "ignore",
                            "SMARTMEMORY_TEAM_ID": "override",
                            "SMARTMEMORY_DATA_DIR": "bogus",
                        },
                        "env_override": {"SMARTMEMORY_TEAM_ID": CANARY_RC},
                    }
                },
            }
        ),
    )
    env = {
        "SMARTMEMORY_MODE": "remote",
        "SMARTMEMORY_TEAM_ID": "t",
        "SMARTMEMORY_DATA_DIR": "/d",
    }
    got_a = {v.name: v.decision for v in inherited_vars(env, tmp_path, proj_a)}
    got_b = {v.name: v.decision for v in inherited_vars(env, tmp_path, proj_b)}
    assert got_a == {
        "SMARTMEMORY_DATA_DIR": None,  # unknown decision string is dropped
        "SMARTMEMORY_MODE": "ignore",
        "SMARTMEMORY_TEAM_ID": "override",
    }
    assert set(got_b.values()) == {None}


def test_project_key_is_git_top_level(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert env_sources.project_key(sub) == env_sources.project_key(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert env_sources.project_key(plain) == str(plain.resolve())


def test_never_returns_or_prints_values(tmp_path, capsys, caplog):
    _write(tmp_path / ".zshrc", f'export SMARTMEMORY_MODE="{CANARY_RC}"\n')
    env = {"SHELL": "/bin/zsh", "SMARTMEMORY_MODE": CANARY_ENV}
    got = inherited_vars(env, tmp_path, tmp_path)
    cap = capsys.readouterr()
    blob = repr(got) + cap.out + cap.err + caplog.text
    assert got == [InheritedVar("SMARTMEMORY_MODE", "~/.zshrc:1", None)]
    assert CANARY_ENV not in blob and CANARY_RC not in blob


def test_default_environ_uses_pre_pin_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "t")
    env_sources.record_pre_pin_names(
        ["SMARTMEMORY_MODE", "SMARTMEMORY_TEAM_ID", "PATH"]
    )
    names = [v.name for v in inherited_vars(home=tmp_path, project_dir=tmp_path)]
    # SMARTMEMORY_MODE was removed by an "ignore" pin but the shell did set it.
    assert names == ["SMARTMEMORY_MODE", "SMARTMEMORY_TEAM_ID"]


def test_unreadable_config_warns_and_reports_undecided(tmp_path, caplog):
    _write(tmp_path / ".config" / "smartmemory" / "config.toml", "not = [valid")
    got = inherited_vars({"SMARTMEMORY_MODE": "x"}, tmp_path, tmp_path)
    assert got == [InheritedVar("SMARTMEMORY_MODE", None, None)]
    assert "project env decisions ignored" in caplog.text
