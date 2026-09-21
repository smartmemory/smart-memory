"""Shared compatibility check for interactive installation diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.metadata
import os
import sys

from smartmemory_app.update_check import version_lt

# Every wrapper and core release below 1.4.39 is yanked on PyPI. Keep this
# equal to the oldest unyanked smartmemory-core release.
MIN_CORE_VERSION = "1.4.39"

PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)
SOCKS_PROXY_SCHEMES = frozenset({"socks4", "socks5", "socks5h"})
SOCKS_PROXY_PREFIXES = tuple(f"{scheme}://" for scheme in SOCKS_PROXY_SCHEMES)
SOCKS_PROXY_ERROR = (
    "SOCKS proxy support is missing for the daemon's local API calls (socksio)."
    "\n  Run: pip install httpx[socks]"
)
PYSOCKS_ERROR = (
    "SOCKS proxy support is missing for the wrapper's own outbound calls, "
    "including WikipediaGrounder (PySocks).\n  Run: pip install pysocks"
)


def _socks_proxy_is_configured() -> bool:
    """Return whether a standard proxy variable contains a SOCKS URL."""
    return any(
        value.strip().lower().startswith(SOCKS_PROXY_PREFIXES)
        for name in PROXY_ENV_VARS
        if (value := os.environ.get(name))
    )


def _socksio_is_importable() -> bool:
    """Return whether httpx's optional SOCKS transport can be imported."""
    try:
        importlib.import_module("socksio")
    except ImportError:
        return False
    return True


def _pysocks_is_importable() -> bool:
    """Return whether requests' optional SOCKS transport can be imported."""
    try:
        importlib.import_module("socks")
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class InstallationCheck:
    """Installed Python and smartmemory-core compatibility verdict."""

    python_version: tuple[int, int, int]
    core_version: str | None
    python_ok: bool
    core_ok: bool
    socks_proxy_configured: bool
    socks_support_ok: bool
    pysocks_support_ok: bool

    @property
    def ok(self) -> bool:
        """Whether setup can proceed (the existing Python/core contract)."""
        return self.python_ok and self.core_ok

    @property
    def doctor_ok(self) -> bool:
        """Whether all installation diagnostics pass."""
        return self.ok and self.socks_support_ok and self.pysocks_support_ok


def check_installation() -> InstallationCheck:
    """Return the compatibility verdict shared by ``doctor`` and ``setup``."""
    python_version = tuple(sys.version_info[:3])
    try:
        core_version = importlib.metadata.version("smartmemory-core")
    except importlib.metadata.PackageNotFoundError:
        core_version = None

    socks_proxy_configured = _socks_proxy_is_configured()

    return InstallationCheck(
        python_version=python_version,
        core_version=core_version,
        python_ok=python_version >= (3, 11, 0),
        core_ok=(
            core_version is not None and not version_lt(core_version, MIN_CORE_VERSION)
        ),
        socks_proxy_configured=socks_proxy_configured,
        socks_support_ok=(not socks_proxy_configured or _socksio_is_importable()),
        pysocks_support_ok=(not socks_proxy_configured or _pysocks_is_importable()),
    )
