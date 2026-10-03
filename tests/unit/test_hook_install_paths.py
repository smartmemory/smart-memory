"""Regression flows for shell-safe hook registration and upgrade repair."""

import copy
import json
from pathlib import Path, PureWindowsPath
import shlex
import subprocess

import pytest
from click.testing import CliRunner

from smartmemory_app import setup


@pytest.fixture
def hook_home(tmp_path, monkeypatch):
    home = tmp_path / "test_hook_home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(setup, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(setup, "HOOKS_DEST", home / ".claude" / "hooks")
    monkeypatch.setattr(setup, "SETTINGS", home / ".claude" / "settings.json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(home / "data"))
    return home


@pytest.mark.parametrize("user", ["tester", "First Last", "Cash$ and `tick`"])
def test_windows_commands_are_quoted_forward_slash_paths(hook_home, monkeypatch, user):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    home = PureWindowsPath("C:/Users") / user
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(setup, "HOOKS_DEST", Path.home() / ".claude" / "hooks")
    setup._register_hooks()
    settings = json.loads(setup.SETTINGS.read_text())
    assert len(settings["hooks"]) == 6
    for entry in settings["hooks"].values():
        command = entry[0]["hooks"][0]["command"]
        # shlex is a tokenizer, not a bash interpreter: it retains backslashes
        # before $ and backticks in double quotes. Check actual shell arguments.
        parsed = subprocess.run(
            ["bash", "-c", "set -- " + command + '; printf "%s\\n" "$@"'],
            capture_output=True,
            text=True,
            check=True,
        )
        args = parsed.stdout.splitlines()
        assert args[0] == "bash"
        assert args[1].startswith(f"C:/Users/{user}/.claude/hooks/smartmemory-")
        assert "\\" not in args[1]
        assert command.startswith('bash "') and command.endswith('"')


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("directory", ["hooks", "Space hooks"])
def test_posix_paths_are_unchanged(hook_home, monkeypatch, platform, directory):
    monkeypatch.setattr(setup.sys, "platform", platform)
    monkeypatch.setattr(setup, "HOOKS_DEST", hook_home / directory)
    for entry in setup._get_hook_registrations().values():
        command = entry["hooks"][0]["command"]
        filename = command.rsplit("/", 1)[1]
        assert command == f"bash {setup.HOOKS_DEST / filename}"


def test_reinstall_repairs_six_hooks_preserving_foreign_entries(hook_home, monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(
        setup, "HOOKS_DEST", PureWindowsPath("C:/Users/First Last/.claude/hooks")
    )
    old = {}
    for event, entry in setup._get_hook_registrations().items():
        entry = copy.deepcopy(entry)
        filename = shlex.split(entry["hooks"][0]["command"])[1].rsplit("/", 1)[1]
        entry["hooks"][0]["command"] = f"bash {setup.HOOKS_DEST / filename}"
        entry["hooks"][0]["timeout"] = 12
        old[event] = [entry]
    foreign = {"type": "command", "command": "bash C:\\Other\\hooks\\session-start.sh"}
    old["SessionStart"][0]["hooks"].append(foreign)
    foreign_entry = {"matcher": "custom", "hooks": [foreign]}
    old["SessionStart"].extend([foreign_entry, copy.deepcopy(foreign_entry)])
    elsewhere = {
        "matcher": "",
        "hooks": [
            {"type": "command", "command": "bash C:\\Other\\smartmemory-orient.sh"}
        ],
    }
    old["SessionStart"].append(elsewhere)
    setup.SETTINGS.parent.mkdir(parents=True)
    setup.SETTINGS.write_text(
        json.dumps({"hooks": old, "permissions": {"allow": ["Bash(true)"]}})
    )

    setup._register_hooks()
    first = setup.SETTINGS.read_bytes()
    modified = setup.SETTINGS.stat().st_mtime_ns
    setup._register_hooks()
    assert setup.SETTINGS.read_bytes() == first
    assert setup.SETTINGS.stat().st_mtime_ns == modified
    settings = json.loads(first)
    assert settings["permissions"] == {"allow": ["Bash(true)"]}
    for event, expected in setup._get_hook_registrations().items():
        installed = settings["hooks"][event][0]["hooks"][0]
        assert installed["command"] == expected["hooks"][0]["command"]
        assert installed["timeout"] == 12
        assert len(settings["hooks"][event]) == (4 if event == "SessionStart" else 1)
    assert settings["hooks"]["SessionStart"][0]["hooks"][1] == foreign
    assert settings["hooks"]["SessionStart"][1:] == [
        foreign_entry,
        foreign_entry,
        elsewhere,
    ]


def test_setup_repairs_hooks_and_copies_utf8_scripts(hook_home, monkeypatch):
    """Run the real click setup and filesystem installer in a temporary home."""
    from smartmemory_app.cli import cli

    monkeypatch.setattr(setup.sys, "platform", "win32")
    setup.SETTINGS.parent.mkdir(parents=True)
    old = copy.deepcopy(setup._get_hook_registrations())
    for entry in old.values():
        command = entry["hooks"][0]["command"]
        entry["hooks"][0]["command"] = "bash " + shlex.split(command)[1].replace(
            "/", "\\"
        )
    setup.SETTINGS.write_text(json.dumps({"hooks": {k: [v] for k, v in old.items()}}))
    monkeypatch.setattr(setup, "_ensure_spacy", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "_ensure_lazy_models", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "_ensure_embedding_model", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "_start_daemon_local", lambda: {"status": "ok"})
    monkeypatch.setattr("smartmemory_app.daemon.is_running", lambda **kw: False)
    monkeypatch.setattr("smartmemory_app.launch_metrics.emit", lambda *a, **kw: None)
    result = CliRunner().invoke(
        cli,
        ["setup", "--mode", "local"],
        input=f"n\nnone\nlocal\nsm\n{hook_home / 'data'}\n",
    )
    assert result.exit_code == 0, result.output
    settings = json.loads(setup.SETTINGS.read_text())
    for event, entry in setup._get_hook_registrations().items():
        assert settings["hooks"][event] == [entry]
    for name in setup.HOOK_NAMES:
        script = setup.HOOKS_DEST / name
        assert "export PYTHONUTF8=1" in script.read_text()
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_generated_command_executes_a_path_with_spaces(hook_home, monkeypatch):
    """Bash executes the quoted command rather than splitting its script path."""
    monkeypatch.setattr(setup.sys, "platform", "win32")
    dest = hook_home / "First Last $ cash ` tick"
    dest.mkdir(parents=True)
    monkeypatch.setattr(setup, "HOOKS_DEST", dest)
    (dest / "smartmemory-orient.sh").write_text("#!/bin/bash\nprintf 'hook ran'\n")
    result = subprocess.run(
        ["bash", "-c", setup._hook_command("smartmemory-orient.sh")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "hook ran"


def test_duplicate_mixed_entries_keep_foreign_commands(hook_home, monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "win32")
    entry = setup._get_hook_registrations()["SessionStart"]
    foreign = {"type": "command", "command": "echo 中文"}
    entry["hooks"].append(foreign)
    setup.SETTINGS.parent.mkdir(parents=True)
    setup.SETTINGS.write_text(
        json.dumps({"hooks": {"SessionStart": [entry, entry]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    setup._register_hooks()
    settings = json.loads(setup.SETTINGS.read_text(encoding="utf-8"))
    assert settings["hooks"]["SessionStart"] == [entry, entry]
