"""`smartmemory doctor` (DIST-INSTALL-RESOLVE-1).

The command exists to catch the pip-backtracked-wrapper trap: an ancient
`smartmemory-core` from the dead-`/auth/me` era installed next to a modern
wrapper. These tests pin its three verdicts (OK / too old / missing) and the
Python floor, and pin the floor itself to the PyPI yank boundary.
"""

from collections import namedtuple
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_mod
from smartmemory_app import setup as setup_mod
from smartmemory_app.cli import MIN_CORE_VERSION, cli
from smartmemory_app.setup import setup as setup_cmd

_VersionInfo = namedtuple("_VersionInfo", "major minor micro releaselevel serial")
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_proxy_environment(monkeypatch):
    """Never let doctor tests inherit the developer machine's proxy settings."""
    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _fake_pkg_version(core: str | None):
    def _version(name: str) -> str:
        if name == "smartmemory-core":
            if core is None:
                raise PackageNotFoundError(name)
            return core
        return "1.4.92"

    return _version


def test_doctor_floor_matches_pypi_yank_boundary():
    """Everything below 1.4.39 is yanked on PyPI; the floor must not drift from it."""
    assert MIN_CORE_VERSION == "1.4.39"


def test_doctor_passes_on_current_core(runner):
    with patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert f"✓ smartmemory-core {MIN_CORE_VERSION} OK" in result.output
    assert "All checks passed." in result.output


def test_doctor_fails_when_lowercase_socks_proxy_lacks_support(runner, monkeypatch):
    monkeypatch.setenv("all_proxy", "socks5h://proxy.example:1080")
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch(
            "smartmemory_app.install_check._socksio_is_importable",
            return_value=False,
            create=True,
        ),
    ):
        result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "SmartMemory cannot use your network's SOCKS proxy" in result.output
    assert "pip install httpx[socks]" in result.output
    assert "All checks passed." not in result.output


def test_doctor_passes_when_socks_proxy_support_is_installed(runner, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks4://proxy.example:1080")
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch(
            "smartmemory_app.install_check._socksio_is_importable",
            return_value=True,
            create=True,
        ),
    ):
        result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "\u2713 SOCKS proxy support is installed" in result.output
    assert "All checks passed." in result.output


def test_doctor_passes_without_a_proxy_and_does_not_import_socksio(runner):
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch(
            "smartmemory_app.install_check._socksio_is_importable", create=True
        ) as socksio_is_importable,
    ):
        result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "SOCKS proxy" not in result.output
    socksio_is_importable.assert_not_called()


def test_doctor_ignores_http_proxy(runner, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch(
            "smartmemory_app.install_check._socksio_is_importable", create=True
        ) as socksio_is_importable,
    ):
        result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "SOCKS proxy" not in result.output
    socksio_is_importable.assert_not_called()


def test_doctor_fails_on_backtracked_core(runner):
    """Wrapper 1.1.5 pinned core 0.7.1 — the exact install the bug report hit."""
    with patch("importlib.metadata.version", _fake_pkg_version("0.7.1")):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "smartmemory-core 0.7.1 is too old" in result.output
    assert f"floor is {MIN_CORE_VERSION}" in result.output
    assert "pip-backtracked install" in result.output
    assert "python -m venv .venv" in result.output
    assert "All checks passed." not in result.output


def test_doctor_fails_just_below_floor(runner):
    with patch("importlib.metadata.version", _fake_pkg_version("1.4.38")):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "1.4.38 is too old" in result.output


def test_doctor_fails_when_core_missing(runner):
    with patch("importlib.metadata.version", _fake_pkg_version(None)):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "smartmemory-core is not installed" in result.output
    assert "pip install smartmemory" in result.output


def test_doctor_fails_on_old_python(runner, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "version_info", _VersionInfo(3, 10, 4, "final", 0))
    with patch("importlib.metadata.version", _fake_pkg_version("1.4.92")):
        result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "Python 3.10.4 is too old" in result.output
    # The core check still runs and passes; only the Python row fails.
    assert "✓ smartmemory-core 1.4.92 OK" in result.output


def test_doctor_uses_shared_version_ordering():
    """doctor and the update hint must agree on ordering (same version_lt)."""
    from smartmemory_app import install_check, update_check

    assert install_check.version_lt is update_check.version_lt


def test_doctor_and_setup_share_installation_check():
    """doctor and setup must use one compatibility check and one core floor."""
    from smartmemory_app import install_check

    assert cli_mod.check_installation is install_check.check_installation
    assert setup_mod.check_installation is install_check.check_installation
    assert cli_mod.MIN_CORE_VERSION == install_check.MIN_CORE_VERSION


def test_setup_aborts_on_too_old_core(runner):
    with patch("importlib.metadata.version", _fake_pkg_version("1.4.36")):
        result = runner.invoke(setup_cmd, ["--mode", "local"])

    assert result.exit_code == 1
    assert "Setup stopped because your SmartMemory version is old or incomplete." in (
        result.output
    )
    assert (
        "smartmemory uninstall\n"
        "pip install -U smartmemory\n"
        "sm doctor\n"
        "smartmemory setup"
    ) in result.output
    assert "All checks passed." in result.output
    assert "Traceback" not in result.output


def test_setup_aborts_when_core_missing(runner):
    with patch("importlib.metadata.version", _fake_pkg_version(None)):
        result = runner.invoke(setup_cmd, ["--mode", "local"])

    assert result.exit_code == 1
    assert "old or incomplete" in result.output
    assert "pip install -U smartmemory" in result.output
    assert "Traceback" not in result.output


def test_setup_proceeds_on_healthy_core(runner, tmp_path):
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch("smartmemory_app.config.config_path", return_value=tmp_path / "config"),
        patch("smartmemory_app.setup._setup_click") as setup_click,
    ):
        result = runner.invoke(setup_cmd, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    setup_click.assert_called_once_with("local", None)


def test_setup_warns_but_proceeds_when_socks_proxy_support_is_missing(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.example:1080")
    with (
        patch("importlib.metadata.version", _fake_pkg_version(MIN_CORE_VERSION)),
        patch(
            "smartmemory_app.install_check._socksio_is_importable",
            return_value=False,
            create=True,
        ),
        patch("smartmemory_app.config.config_path", return_value=tmp_path / "config"),
        patch("smartmemory_app.setup._setup_click") as setup_click,
    ):
        result = runner.invoke(setup_cmd, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "Warning: SmartMemory cannot use your network's SOCKS proxy" in result.output
    assert "pip install httpx[socks]" in result.output
    setup_click.assert_called_once_with("local", None)


def test_setup_preflight_raises_click_exception(runner):
    with patch("importlib.metadata.version", _fake_pkg_version("1.4.36")):
        result = runner.invoke(
            setup_cmd,
            ["--mode", "local"],
            standalone_mode=False,
        )

    assert isinstance(result.exception, click.ClickException)
    assert "Traceback" not in result.output
