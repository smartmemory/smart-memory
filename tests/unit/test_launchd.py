"""Tests for DIST-DAEMON-1 Task 10: launchd plist template + install/uninstall."""

import plistlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from smartmemory_app.install_check import PROXY_ENV_VARS


PLIST_TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "smartmemory_app"
    / "data"
    / "ai.smartmemory.daemon.plist"
)
PROXY_PASSTHROUGH_VARS = frozenset((*PROXY_ENV_VARS, "NO_PROXY", "no_proxy"))


@pytest.fixture(autouse=True)
def isolated_proxy_environment(monkeypatch):
    """Never let launchd tests inherit the developer machine's proxy settings."""
    for name in PROXY_PASSTHROUGH_VARS:
        monkeypatch.delenv(name, raising=False)


class TestPlistTemplate:
    def test_template_exists(self):
        assert PLIST_TEMPLATE.exists(), (
            "Plist template must be shipped in smartmemory_app/data/"
        )

    def test_template_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        tree = ET.parse(PLIST_TEMPLATE)
        root = tree.getroot()
        assert root.tag == "plist"

    def test_template_has_required_placeholders(self):
        content = PLIST_TEMPLATE.read_text()
        for placeholder in [
            "{PYTHON_PATH}",
            "{DATA_DIR}",
            "{BIN_DIR}",
            "{LAUNCHD_LABEL}",
            "{LAUNCHD_COMMAND}",
            "{PROXY_ENV_XML}",
        ]:
            assert placeholder in content, f"Template must contain {placeholder}"

    def test_template_label_is_installed_from_shared_constant(self):
        content = PLIST_TEMPLATE.read_text()
        assert "<string>{LAUNCHD_LABEL}</string>" in content

    def test_template_has_keepalive(self):
        content = PLIST_TEMPLATE.read_text()
        assert "<key>KeepAlive</key>" in content

    def test_template_has_run_at_load(self):
        content = PLIST_TEMPLATE.read_text()
        assert "<key>RunAtLoad</key>" in content


class TestInstallLaunchdPlist:
    @staticmethod
    def _installed_environments(launch_agents):
        return [
            plistlib.loads((launch_agents / name).read_bytes())["EnvironmentVariables"]
            for name in (
                "ai.smartmemory.daemon.plist",
                "ai.smartmemory.worker.plist",
            )
        ]

    def test_install_substitutes_placeholders(self, tmp_path, monkeypatch):
        """_install_launchd_plist() substitutes all placeholders and writes valid plist."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        mock_cfg = MagicMock()
        mock_cfg.daemon_port = 9014
        mock_cfg.data_dir = str(tmp_path / ".smartmemory")

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            result = _install_launchd_plist()

        assert result is True
        plist = launch_agents / "ai.smartmemory.daemon.plist"
        assert plist.exists()
        content = plist.read_text()
        # No unsubstituted placeholders
        assert "{PYTHON_PATH}" not in content
        assert "{DAEMON_PORT}" not in content
        assert "{DATA_DIR}" not in content
        assert "{BIN_DIR}" not in content
        # Substituted values present
        assert "9014" in content
        assert "ai.smartmemory.daemon" in content

    def test_install_captures_proxy_environment_for_both_jobs(
        self, tmp_path, monkeypatch
    ):
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()
        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)
        monkeypatch.setenv(
            "HTTPS_PROXY", "http://user:pass@127.0.0.1:7890/path?a=1&b=2"
        )
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

        mock_cfg = MagicMock(daemon_port=9014, data_dir=str(tmp_path / ".smartmemory"))
        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            assert _install_launchd_plist() is True

        for environment in self._installed_environments(launch_agents):
            assert environment["HTTPS_PROXY"] == (
                "http://user:pass@127.0.0.1:7890/path?a=1&b=2"
            )
            assert environment["NO_PROXY"] == "localhost,127.0.0.1"

    def test_install_omits_unset_and_empty_proxy_environment(
        self, tmp_path, monkeypatch
    ):
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()
        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)
        monkeypatch.setenv("HTTP_PROXY", "")

        mock_cfg = MagicMock(daemon_port=9014, data_dir=str(tmp_path / ".smartmemory"))
        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            assert _install_launchd_plist() is True

        for environment in self._installed_environments(launch_agents):
            assert PROXY_PASSTHROUGH_VARS.isdisjoint(environment)

    def test_install_uses_config_data_dir(self, tmp_path, monkeypatch):
        """_install_launchd_plist() uses load_config().data_dir, not env var or default."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()
        custom_dir = tmp_path / "custom-memories"

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)
        # Set env var to a DIFFERENT path — plist must ignore it
        monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "env-override"))

        mock_cfg = MagicMock()
        mock_cfg.daemon_port = 9014
        mock_cfg.data_dir = str(custom_dir)

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            _install_launchd_plist()

        content = (launch_agents / "ai.smartmemory.daemon.plist").read_text()
        assert str(custom_dir) in content, "Plist must use config data_dir, not env var"
        assert "env-override" not in content, (
            "Plist must NOT use SMARTMEMORY_DATA_DIR env var"
        )

    def test_install_calls_launchctl_load(self, tmp_path, monkeypatch):
        """_install_launchd_plist() calls launchctl load on the installed plist."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        mock_cfg = MagicMock()
        mock_cfg.daemon_port = 9014
        mock_cfg.data_dir = str(tmp_path / ".smartmemory")

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            _install_launchd_plist()

        # Find the launchctl load call
        load_calls = [c for c in mock_run.call_args_list if "load" in str(c)]
        assert len(load_calls) >= 1, "Must call launchctl load"

    def test_install_returns_false_non_darwin(self, tmp_path, monkeypatch):
        """_install_launchd_plist() returns False on non-macOS platforms."""
        launch_agents = tmp_path / "LaunchAgents"

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        with patch("platform.system", return_value="Linux"):
            from smartmemory_app.setup import _install_launchd_plist

            result = _install_launchd_plist()

        assert result is False
        assert not launch_agents.exists() or not list(launch_agents.iterdir())

    def test_install_returns_false_on_launchctl_failure(self, tmp_path, monkeypatch):
        """_install_launchd_plist() returns False when launchctl load fails."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        mock_cfg = MagicMock()
        mock_cfg.daemon_port = 9014
        mock_cfg.data_dir = str(tmp_path / ".smartmemory")

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")
            from smartmemory_app.setup import _install_launchd_plist

            result = _install_launchd_plist()

        assert result is False

    def test_install_unloads_existing_before_overwrite(self, tmp_path, monkeypatch):
        """_install_launchd_plist() unloads existing plist before writing new one."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()
        existing = launch_agents / "ai.smartmemory.daemon.plist"
        existing.write_text("<?xml version='1.0'?><plist><dict/></plist>")

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        mock_cfg = MagicMock()
        mock_cfg.daemon_port = 9014
        mock_cfg.data_dir = str(tmp_path / ".smartmemory")

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("smartmemory_app.config.load_config", return_value=mock_cfg),
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _install_launchd_plist

            _install_launchd_plist()

        # First call should be unload, second should be load
        calls = mock_run.call_args_list
        unload_calls = [c for c in calls if "unload" in str(c)]
        assert len(unload_calls) >= 1, "Must unload existing plist before overwriting"


class TestUninstallLaunchdPlist:
    def test_uninstall_removes_plist(self, tmp_path, monkeypatch):
        """_uninstall_launchd_plist() unloads and deletes the plist file."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()
        plist = launch_agents / "ai.smartmemory.daemon.plist"
        plist.write_text("<?xml version='1.0'?><plist><dict/></plist>")

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("platform.system", return_value="Darwin"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            from smartmemory_app.setup import _uninstall_launchd_plist

            _uninstall_launchd_plist()

        assert not plist.exists(), "Plist file must be removed after uninstall"
        unload_calls = [c for c in mock_run.call_args_list if "unload" in str(c)]
        assert len(unload_calls) >= 1, "Must call launchctl unload"

    def test_uninstall_noop_when_no_plist(self, tmp_path, monkeypatch):
        """_uninstall_launchd_plist() is a no-op when plist doesn't exist."""
        launch_agents = tmp_path / "LaunchAgents"
        launch_agents.mkdir()

        monkeypatch.setattr("smartmemory_app.setup.LAUNCH_AGENTS_DIR", launch_agents)

        with (
            patch("smartmemory_app.setup.subprocess.run") as mock_run,
            patch("platform.system", return_value="Darwin"),
        ):
            from smartmemory_app.setup import _uninstall_launchd_plist

            _uninstall_launchd_plist()

        mock_run.assert_not_called()

    def test_uninstall_skips_non_darwin(self, tmp_path, monkeypatch):
        """_uninstall_launchd_plist() is a no-op on non-macOS."""
        with patch("platform.system", return_value="Linux"):
            from smartmemory_app.setup import _uninstall_launchd_plist

            _uninstall_launchd_plist()  # should not raise


class TestUninstallCommand:
    @staticmethod
    def _invoke(tmp_path, monkeypatch, *args):
        config_file = tmp_path / "config" / "smartmemory" / "config.toml"
        data_dir = tmp_path / ".smartmemory"

        monkeypatch.setattr("smartmemory_app.config.config_path", lambda: config_file)
        monkeypatch.setattr("smartmemory_app.setup.DATA_DIR", data_dir)

        with (
            patch("smartmemory_app.daemon.stop_daemon"),
            patch("smartmemory_app.setup._uninstall_launchd_plist"),
            patch("smartmemory_app.setup._deregister_hooks"),
            patch("smartmemory_app.setup._remove_hooks"),
            patch("smartmemory_app.setup._remove_skills"),
        ):
            from smartmemory_app.setup import uninstall

            result = CliRunner().invoke(uninstall, list(args))

        return result

    def test_uninstall_removes_existing_config_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config" / "smartmemory" / "config.toml"
        data_dir = tmp_path / ".smartmemory"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('[smartmemory]\nmode = "remote"\n')
        data_dir.mkdir()

        result = self._invoke(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        assert not config_file.exists()
        assert not data_dir.exists()
        assert f"Removed config file: {config_file}" in result.output

    def test_uninstall_keep_data_still_removes_config_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config" / "smartmemory" / "config.toml"
        data_dir = tmp_path / ".smartmemory"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('[smartmemory]\nmode = "local"\n')
        data_dir.mkdir()

        result = self._invoke(tmp_path, monkeypatch, "--keep-data")

        assert result.exit_code == 0, result.output
        assert not config_file.exists()
        assert data_dir.exists()
        assert f"Removed config file: {config_file}" in result.output

    def test_uninstall_without_config_file_is_a_noop(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config" / "smartmemory" / "config.toml"
        result = self._invoke(tmp_path, monkeypatch, "--keep-data")

        assert result.exit_code == 0, result.output
        assert not config_file.exists()
        assert "Removed config file:" not in result.output
