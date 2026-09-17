"""Shared compatibility check for interactive installation diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import sys

from smartmemory_app.update_check import version_lt

# Every wrapper and core release below 1.4.39 is yanked on PyPI. Keep this
# equal to the oldest unyanked smartmemory-core release.
MIN_CORE_VERSION = "1.4.39"


@dataclass(frozen=True)
class InstallationCheck:
    """Installed Python and smartmemory-core compatibility verdict."""

    python_version: tuple[int, int, int]
    core_version: str | None
    python_ok: bool
    core_ok: bool

    @property
    def ok(self) -> bool:
        return self.python_ok and self.core_ok


def check_installation() -> InstallationCheck:
    """Return the compatibility verdict shared by ``doctor`` and ``setup``."""
    python_version = tuple(sys.version_info[:3])
    try:
        core_version = importlib.metadata.version("smartmemory-core")
    except importlib.metadata.PackageNotFoundError:
        core_version = None

    return InstallationCheck(
        python_version=python_version,
        core_version=core_version,
        python_ok=python_version >= (3, 11, 0),
        core_ok=(
            core_version is not None and not version_lt(core_version, MIN_CORE_VERSION)
        ),
    )
