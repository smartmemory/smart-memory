"""`smartmemory doctor` (DIST-INSTALL-RESOLVE-1).

The command exists to catch the pip-backtracked-wrapper trap: an ancient
`smartmemory-core` from the dead-`/auth/me` era installed next to a modern
wrapper. These tests pin its three verdicts (OK / too old / missing) and the
Python floor, and pin the floor itself to the PyPI yank boundary.
"""

from collections import namedtuple
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_mod
from smartmemory_app.cli import MIN_CORE_VERSION, cli

_VersionInfo = namedtuple("_VersionInfo", "major minor micro releaselevel serial")


@pytest.fixture
def runner():
    return CliRunner()


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
    from smartmemory_app import update_check

    assert cli_mod._version_lt is update_check.version_lt
