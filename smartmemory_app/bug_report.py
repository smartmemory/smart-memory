"""Automated CLI bug reporting for the public SmartMemory tracker."""

from __future__ import annotations

import importlib.metadata
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


_APP_ID = "6991beef19a3c34691c33ca0"
_BASE_URL = f"https://base44.app/api/apps/{_APP_ID}"
_UPLOAD_URL = f"{_BASE_URL}/integration-endpoints/Core/UploadFile"
_BUG_REPORT_URL = f"{_BASE_URL}/entities/BugReport"
_MAX_LOG_LINES = 2_000
_MAX_LOG_BYTES = 512 * 1024


class BugReportError(RuntimeError):
    """Raised when the tracker record could not be created."""


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


@dataclass(frozen=True)
class BugReportResult:
    """Outcome details needed for concise CLI confirmation."""

    record_id: str | None
    debug_log_attached: bool
    debug_log_available: bool
    upload_error: str | None = None


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


def build_bug_report_payload(
    *,
    test_id: str | None,
    test_title: str | None,
    message: str,
    severity: str,
    environment: EnvironmentInfo,
    timestamp: str,
    debug_log_url: str | None = None,
) -> dict[str, str]:
    """Build a schema-valid BugReport entity payload."""
    effective_test_id = test_id or f"CLI-{timestamp}"
    payload = {
        "test_id": effective_test_id,
        "test_title": test_title or test_id or "Ad-hoc CLI report",
        "actual_outcome": message,
        "severity": severity,
        "browser_info": environment.browser_info,
    }
    if debug_log_url:
        payload["debug_log_url"] = debug_log_url
    return payload


def submit_bug_report(
    *,
    test_id: str | None,
    test_title: str | None,
    message: str,
    severity: str,
    log_path: Path | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> BugReportResult:
    """Upload available diagnostics and create a public tracker BugReport."""
    report_time = now or datetime.now(timezone.utc)
    timestamp = report_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    effective_log_path = log_path or debug_log_path()

    try:
        log_tail = read_log_tail(effective_log_path)
    except OSError as exc:
        log_tail = None
        upload_error = f"could not read debug log ({_error_summary(exc)})"
    else:
        upload_error = None

    owns_client = client is None
    try:
        http_client = client or httpx.Client(timeout=30.0)
    except Exception as exc:
        raise BugReportError(
            f"HTTP client could not start ({_error_summary(exc)})"
        ) from exc
    debug_log_url = None
    try:
        if log_tail:
            filename = f"cli-debug-{timestamp}.log"
            try:
                upload_response = http_client.post(
                    _UPLOAD_URL,
                    headers={"X-App-Id": _APP_ID},
                    files={"file": (filename, log_tail, "text/plain")},
                )
                upload_response.raise_for_status()
                debug_log_url = _find_string_value(upload_response.json(), "file_url")
                if debug_log_url is None:
                    upload_error = "upload response did not contain file_url"
            except (httpx.HTTPError, ValueError) as exc:
                upload_error = f"debug log upload failed ({_error_summary(exc)})"

        payload = build_bug_report_payload(
            test_id=test_id,
            test_title=test_title,
            message=message,
            severity=severity,
            environment=gather_environment(),
            timestamp=timestamp,
            debug_log_url=debug_log_url,
        )
        try:
            create_response = http_client.post(
                _BUG_REPORT_URL,
                headers={
                    "X-App-Id": _APP_ID,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            create_response.raise_for_status()
            response_payload = create_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BugReportError(
                f"tracker request failed ({_error_summary(exc)})"
            ) from exc
    finally:
        if owns_client:
            http_client.close()

    return BugReportResult(
        record_id=_find_string_value(response_payload, "id"),
        debug_log_attached=debug_log_url is not None,
        debug_log_available=log_tail is not None,
        upload_error=upload_error,
    )


def _installed_version(package_name: str) -> str:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _find_string_value(payload: Any, key: str) -> str | None:
    """Find a named string in top-level or nested JSON response envelopes."""
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        for nested in payload.values():
            found = _find_string_value(nested, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for nested in payload:
            found = _find_string_value(nested, key)
            if found is not None:
                return found
    return None


def _error_summary(exc: Exception) -> str:
    """Return a one-line, non-empty error suitable for confused testers."""
    summary = " ".join(str(exc).split())
    return summary or type(exc).__name__
