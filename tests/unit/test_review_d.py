"""Job D regressions for the startup deadline and executable quoting."""

import copy
import json
import shutil
import subprocess
from types import SimpleNamespace

from smartmemory_app import daemon, setup


def test_direct_start_daemon_has_one_deadline(monkeypatch):
    def status(**kwargs):
        assert daemon._deadline.get() is not None
        assert 0 < daemon._remaining(1000) <= daemon.START_TIMEOUT
        return {"status": "ok"}

    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda: False)
    monkeypatch.setattr(daemon, "get_status", status)
    assert daemon.start_daemon() == {"status": "ok"}
    assert daemon._deadline.get() is None


def test_hook_executable_and_script_expand_nothing_and_repair(tmp_path, monkeypatch):
    shell = tmp_path / "Git $HOME `printf injected` with spaces" / "bin" / "bash.exe"
    shell.parent.mkdir(parents=True)
    shell.symlink_to(shutil.which("bash"))
    dest = tmp_path / "Hooks $HOME `printf injected` with spaces"
    dest.mkdir()
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(setup, "HOOKS_DEST", dest)
    monkeypatch.setattr(setup, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(setup, "_resolve_hook_shell", lambda: str(shell))
    script = dest / "smartmemory-orient.sh"
    script.write_text("printf 'test_hook_literal_paths'\n")
    old = copy.deepcopy(setup._get_hook_registrations())
    for entry in old.values():
        command = entry["hooks"][0]["command"]
        # Previous release quoted the shell but left its expansions active.
        entry["hooks"][0]["command"] = command.replace("\\$", "$", 1).replace(
            "\\`", "`", 2
        )
    setup.SETTINGS.write_text(json.dumps({"hooks": {k: [v] for k, v in old.items()}}))
    setup._register_hooks()
    settings = json.loads(setup.SETTINGS.read_text())
    for event, expected in setup._get_hook_registrations().items():
        assert settings["hooks"][event] == [expected]
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "test_hook_literal_paths"
    before = setup.SETTINGS.read_bytes()
    setup._register_hooks()
    assert setup.SETTINGS.read_bytes() == before
