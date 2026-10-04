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
    from packaging.specifiers import SpecifierSet

    python_version = tuple(sys.version_info[:3])
    try:
        core_version = importlib.metadata.version("smartmemory-core")
    except importlib.metadata.PackageNotFoundError:
        core_version = None

    socks_proxy_configured = _socks_proxy_is_configured()

    return InstallationCheck(
        python_version=python_version,
        core_version=core_version,
        python_ok=SpecifierSet(python_requirement()).contains(
            ".".join(map(str, python_version))
        ),
        core_ok=(
            core_version is not None and not version_lt(core_version, MIN_CORE_VERSION)
        ),
        socks_proxy_configured=socks_proxy_configured,
        socks_support_ok=(not socks_proxy_configured or _socksio_is_importable()),
        pysocks_support_ok=(not socks_proxy_configured or _pysocks_is_importable()),
    )


def python_requirement() -> str:
    """Wheel metadata is generated from pyproject.toml's requires-python."""
    try:
        return importlib.metadata.metadata("smartmemory")["Requires-Python"]
    except importlib.metadata.PackageNotFoundError:
        import tomllib
        from pathlib import Path

        return tomllib.loads(
            (Path(__file__).parent.parent / "pyproject.toml").read_text()
        )["project"]["requires-python"]


def native_library_checks() -> list[str]:
    """Share actual imports between first-run checks and offline doctor probes."""
    rows = []
    for module, wheel in (("usearch.index", "usearch"), ("numpy", "numpy")):
        try:
            importlib.import_module(module)
            rows.append(f"Native {wheel}: OK")
        except (ImportError, OSError) as exc:
            rows.append(
                f"Native {wheel}: FAIL: {type(exc).__name__}: {exc}. "
                f"Fix: python -m pip install --force-reinstall {wheel}"
            )
    try:
        from smartmemory.plugins.embedding import EmbeddingService

        service = EmbeddingService()
        backend = service.backend_name
        rows.append(f"Native embedding: OK ({backend})")
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        rows.append(
            f"Native embedding: FAIL: {type(exc).__name__}: {exc}. "
            f'Fix: python -m pip install --force-reinstall "smartmemory-core[onnx]"'
        )
    return rows


def run_native_checks() -> list[str]:
    from smartmemory_app.diagnostic_process import offline_checks

    return offline_checks("native")


def first_run_check(data_dir) -> None:
    """Cache only passes, retry failures, and report each failed version once."""
    import json
    import logging
    from pathlib import Path
    from packaging.specifiers import SpecifierSet
    from filelock import FileLock, Timeout
    from smartmemory_app.install_troubleshooting import handled_install_failure

    path = Path(data_dir) / ".install-check-pass.json"
    try:
        from smartmemory_app import __version__

        version = __version__
        identity = {
            "version": version,
            "python": list(sys.version_info[:3]),
            "probe": 1,
            "backend": os.getenv("SMARTMEMORY_EMBEDDING_BACKEND", "auto"),
            "model": os.getenv("SMARTMEMORY_EMBEDDING_LOCAL_MODEL", ""),
        }
        if path.exists() and json.loads(path.read_text()) == identity:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path) + ".lock", timeout=0):
            if path.exists() and json.loads(path.read_text()) == identity:
                return
            rows = run_native_checks()
            if not SpecifierSet(python_requirement()).contains(
                ".".join(map(str, sys.version_info[:3]))
            ):
                rows.insert(
                    0,
                    f"Python: FAIL: requires {python_requirement()}. "
                    f"Fix: python -m venv .venv using a supported Python",
                )
            failed = [row for row in rows if "FAIL:" in row or "warning:" in row]
            if failed:
                path.unlink(missing_ok=True)
                failure_path = path.with_name(".install-check-failure.json")
                signature = {"install": identity, "failures": failed}
                previous = (
                    json.loads(failure_path.read_text())
                    if failure_path.exists()
                    else None
                )
                from smartmemory_app.report_outbox import was_delivered

                pending_path = path.with_name(".install-check-pending.json")
                pending = (
                    json.loads(pending_path.read_text())
                    if pending_path.exists()
                    else {}
                )
                if pending.get("signature") == signature and was_delivered(
                    pending.get("report_id", "")
                ):
                    failure_path.write_text(json.dumps(signature), encoding="utf-8")
                    pending_path.unlink(missing_ok=True)
                    previous = signature
                if previous != signature:
                    error_class = (
                        OSError
                        if any("OSError:" in row for row in failed)
                        else ImportError
                        if any("ImportError:" in row for row in failed)
                        else RuntimeError
                    )
                    result = handled_install_failure(
                        error_class("Native installation self-check failed"),
                        ["install-check"],
                        rows=rows,
                    )
                    if result is not None and result.status == "sent":
                        failure_path.write_text(json.dumps(signature), encoding="utf-8")
                        pending_path.unlink(missing_ok=True)
                    elif result is not None and result.status == "queued":
                        pending_path.write_text(
                            json.dumps(
                                {"signature": signature, "report_id": result.report_id}
                            ),
                            encoding="utf-8",
                        )
                else:
                    import click

                    from smartmemory_app.report_privacy import private_text

                    click.echo(private_text("\n".join(failed)), err=True)
                return
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps(identity), encoding="utf-8")
                temporary.replace(path)
                path.with_name(".install-check-failure.json").unlink(missing_ok=True)
                path.with_name(".install-check-pending.json").unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
    except Timeout:
        return  # A concurrent CLI owns this installation check.
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Installation self-check unavailable (%s)", type(exc).__name__
        )
