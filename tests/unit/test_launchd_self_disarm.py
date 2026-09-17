"""Regression tests for launchd jobs left behind by a bare pip uninstall."""

import builtins
import os
import plistlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_app.daemon import (
    _LAUNCHD_DAEMON_LABEL,
    _LAUNCHD_WORKER_LABEL,
)


_JOBS = (
    (
        _LAUNCHD_DAEMON_LABEL,
        "smartmemory_app.viewer_server",
        "main(port=9014, open_browser=False)",
    ),
    (
        _LAUNCHD_WORKER_LABEL,
        "smartmemory_app.enrichment_worker",
        "main()",
    ),
)


@pytest.mark.parametrize(("label", "module", "main_call"), _JOBS)
def test_launchd_guard_boots_out_and_exits_zero_when_package_is_missing(
    label, module, main_call, tmp_path, monkeypatch
):
    from smartmemory_app.setup import _launchd_command

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    command = _launchd_command(label, module, main_call)
    real_import = builtins.__import__

    def missing_package(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "smartmemory_app":
            raise ModuleNotFoundError(
                "No module named 'smartmemory_app'", name="smartmemory_app"
            )
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch("builtins.__import__", side_effect=missing_package),
        patch("subprocess.run") as run,
    ):
        exec(command, {})

    run.assert_called_once_with(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], check=False
    )
    assert (tmp_path / "daemon.log").read_text() == (
        f"SmartMemory was removed; background job {label} switched itself off.\n"
    )


@pytest.mark.parametrize(("label", "module", "main_call"), _JOBS)
def test_launchd_guard_does_not_disarm_on_runtime_failure(
    label, module, main_call, tmp_path, monkeypatch
):
    from smartmemory_app.setup import _launchd_command

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    command = _launchd_command(label, module, main_call)
    real_import = builtins.__import__

    def crash(*_args, **_kwargs):
        raise RuntimeError("temporary startup failure")

    def import_crashing_job(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module:
            return SimpleNamespace(main=crash)
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch("builtins.__import__", side_effect=import_crashing_job),
        patch("subprocess.run") as run,
        pytest.raises(RuntimeError, match="temporary startup failure"),
    ):
        exec(command, {})

    run.assert_not_called()
    assert not (tmp_path / "daemon.log").exists()


def test_launchd_guard_does_not_disarm_when_job_dependency_is_missing(
    tmp_path, monkeypatch
):
    from smartmemory_app.setup import _launchd_command

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    command = _launchd_command(
        _LAUNCHD_DAEMON_LABEL,
        "smartmemory_app.viewer_server",
        "main(port=9014, open_browser=False)",
    )
    real_import = builtins.__import__

    def missing_dependency(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "smartmemory_app.viewer_server":
            raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch("builtins.__import__", side_effect=missing_dependency),
        patch("subprocess.run") as run,
        pytest.raises(ModuleNotFoundError, match="uvicorn"),
    ):
        exec(command, {})

    run.assert_not_called()
    assert not (tmp_path / "daemon.log").exists()


def test_launchd_install_embeds_guard_for_daemon_and_worker(tmp_path, monkeypatch):
    from smartmemory_app import setup as setup_mod

    launch_agents = tmp_path / "LaunchAgents"
    data_dir = tmp_path / "data"
    launch_agents.mkdir()
    monkeypatch.setattr(setup_mod, "LAUNCH_AGENTS_DIR", launch_agents)
    monkeypatch.setenv("GROQ_API_KEY", "")
    config = MagicMock(daemon_port=9014, data_dir=str(data_dir))

    with (
        patch("platform.system", return_value="Darwin"),
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("keyring.get_password", return_value=None),
        patch("smartmemory_app.setup.subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=0)
        assert setup_mod._install_launchd_plist() is True

    for label in (_LAUNCHD_DAEMON_LABEL, _LAUNCHD_WORKER_LABEL):
        content = (launch_agents / f"{label}.plist").read_text()
        plist = plistlib.loads(content.encode())
        assert plist["Label"] == label
        assert "launchctl" in content
        assert "bootout" in content
        assert f'job_label = "{label}"' in content
        assert "{LAUNCHD_COMMAND}" not in content
