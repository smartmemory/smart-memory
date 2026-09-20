"""Local CLI bug report formatting for the public SmartMemory tracker."""

from __future__ import annotations

import importlib.metadata
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_MAX_LOG_LINES = 2_000
_MAX_LOG_BYTES = 512 * 1024
_PREVIEW_LOG_LINES = 40
_PREVIEW_LOG_BYTES = 16 * 1024


@dataclass(frozen=True)
class EnvironmentInfo:
    """Versions and platform details attached to a CLI report."""

    core_version: str
    wrapper_version: str
    python_version: str
    os_version: str

    @property
    def browser_info(self) -> str:
        """Format CLI context for the tracker's existing environment field."""
        return (
            f"CLI smartmemory-core={self.core_version} "
            f"wrapper={self.wrapper_version} {self.os_version} "
            f"python={self.python_version}"
        )


def debug_log_path() -> Path:
    """Return the configured always-on CLI debug log path."""
    try:
        from smartmemory_app.config import load_config

        data_dir = load_config().data_dir
    except Exception:
        data_dir = "~/.smartmemory"
    return Path(data_dir).expanduser() / "cli-debug.log"


def read_log_tail(
    path: Path,
    *,
    max_lines: int = _MAX_LOG_LINES,
    max_bytes: int = _MAX_LOG_BYTES,
) -> str | None:
    """Read a UTF-8-safe tail bounded by both byte and line limits.

    ``None`` distinguishes a missing file from an existing empty file. A read may
    begin in the middle of a multibyte character or line; replacement decoding is
    intentional because diagnostics must remain best-effort.
    """
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(0, size - max_bytes))
            data = log_file.read(max_bytes)
    except FileNotFoundError:
        return None

    lines = data.splitlines(keepends=True)
    return b"".join(lines[-max_lines:]).decode("utf-8", errors="replace")


def gather_environment() -> EnvironmentInfo:
    """Collect installed package versions and the local runtime environment."""
    return EnvironmentInfo(
        core_version=_installed_version("smartmemory-core"),
        wrapper_version=_installed_version("smartmemory"),
        python_version=platform.python_version(),
        os_version=platform.platform(),
    )


def format_bug_report(
    *,
    test_id: str | None,
    test_title: str | None,
    message: str,
    severity: str,
    environment: EnvironmentInfo,
    log_path: Path | None = None,
    timestamp: datetime | None = None,
) -> str:
    """Render a bounded report that a tester can paste into the tracker."""
    report_time = timestamp or datetime.now(timezone.utc)
    if report_time.tzinfo is None:
        report_time = report_time.replace(tzinfo=timezone.utc)
    timestamp_utc = report_time.astimezone(timezone.utc)
    timestamp_id = timestamp_utc.strftime("%Y%m%dT%H%M%SZ")
    effective_log_path = log_path or debug_log_path()

    try:
        log_tail = read_log_tail(
            effective_log_path,
            max_lines=_PREVIEW_LOG_LINES,
            max_bytes=_PREVIEW_LOG_BYTES,
        )
    except OSError as exc:
        preview = f" unavailable (could not read: {_error_summary(exc)})"
    else:
        if log_tail is None:
            preview = " unavailable (file not found)"
        elif not log_tail:
            preview = " unavailable (file is empty)"
        else:
            preview = f"\n{log_tail.rstrip()}"

    effective_test_id = test_id or f"CLI-{timestamp_id}"
    effective_test_title = test_title or test_id or "Ad-hoc CLI report"
    return "\n".join(
        (
            "Bug Report",
            f"Test ID: {effective_test_id}",
            f"Test title: {effective_test_title}",
            f"Severity: {severity}",
            f"Environment: {environment.browser_info}",
            f"Timestamp: {timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "Steps / actual outcome:",
            message,
            "",
            f"Debug log path: {effective_log_path}",
            f"Debug log preview:{preview}",
        )
    )


def _installed_version(package_name: str) -> str:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _error_summary(exc: Exception) -> str:
    """Return a one-line, non-empty error suitable for confused testers."""
    summary = " ".join(str(exc).split())
    return summary or type(exc).__name__
