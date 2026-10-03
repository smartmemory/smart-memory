"""Bounded, anonymous PostHog crash and support reporting. Never raises."""

import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from filelock import FileLock

from smartmemory_app.bug_report import debug_log_path, gather_environment
from smartmemory_app.report_privacy import (
    private_text,
    private_value,
    read_private_log_tail,
    safe_command,
    safe_log_text,
)

PUBLIC_PROJECT_KEY = "phc_eGs3l5JzZLqjSKOGVRrRChCJX90umc0mU5Ju2WxuBSs"  # PostHog project "SmartMemory" (277617)
DEFAULT_HOST = "https://us.i.posthog.com"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportResult:
    status: str
    report_id: str | None = None


def enabled() -> bool:
    if os.environ.get("SMARTMEMORY_CRASH_REPORTS") == "0":
        return False
    try:
        from smartmemory_app.config import load_config

        return load_config().crash_reports is True
    except Exception:
        log.warning("Crash reporting disabled because config could not be read")
        return False


def _install_id(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".install-id"
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(str(uuid.uuid4()))
    except FileExistsError:
        pass
    # Validate persisted data so a modified file cannot become an identity field.
    return str(uuid.UUID(path.read_text(encoding="utf-8").strip()))


def _exception_list(exc: BaseException, handled: bool) -> list[dict]:
    items, seen = [], set()
    while exc is not None and id(exc) not in seen and len(items) < 20:
        seen.add(id(exc))
        frames, tb = [], exc.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            module = str(frame.f_globals.get("__name__", ""))
            filename = frame.f_code.co_filename
            frames.append(
                {
                    "filename": Path(filename).name,
                    "abs_path": filename,
                    "function": frame.f_code.co_name,
                    "lineno": tb.tb_lineno,
                    "in_app": module == "smartmemory"
                    or module.startswith(("smartmemory.", "smartmemory_app.")),
                    "platform": "python",
                }
            )
            tb = tb.tb_next
        # No locals or source lines. Body-bearing exception messages are omitted.
        items.append(
            {
                "type": type(exc).__name__,
                "value": safe_log_text(str(exc), max_bytes=2_000),
                "mechanism": {"handled": handled, "synthetic": False},
                "stacktrace": {"type": "raw", "frames": frames[-100:]},
            }
        )
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return items  # Outer exception first, innermost cause last.


def _reserve(data_dir: Path, exceptions: list[dict]) -> bool:
    """Persist each reservation under a cross-process lock, including failed sends."""
    frame = next(
        (f for f in reversed(exceptions[0]["stacktrace"]["frames"]) if f["in_app"]), {}
    )
    identity = (
        exceptions[0]["type"],
        frame.get("abs_path"),
        frame.get("function"),
        frame.get("lineno"),
    )
    fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".crash-report-state.json"
    with FileLock(str(path) + ".lock", timeout=0.1):
        state = json.loads(path.read_text()) if path.exists() else {}
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        recent = {
            key: stamp
            for key, stamp in state.get("recent", {}).items()
            if now - stamp < 600
        }
        count = state.get("count", 0) if state.get("day") == day else 0
        if fingerprint in recent or count >= 20:
            return False
        recent[fingerprint] = now
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({"day": day, "count": count + 1, "recent": recent})
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def _common(source: str, data_dir: Path, args: list[str] | None = None) -> dict:
    env = gather_environment()
    properties = {
        "$process_person_profile": False,
        "$geoip_disable": True,
        "report_id": uuid.uuid4().hex[:12],
        "smartmemory_version": env.wrapper_version,
        "smartmemory_core_version": env.core_version,
        "python_version": env.python_version,
        "os": env.os_version,
        "command": safe_command(sys.argv[1:] if args is None else args),
        "source": source,
    }
    for name, filename in (
        ("log_tail_cli", "cli-debug.log"),
        ("log_tail_daemon", "daemon.log"),
    ):
        try:
            properties[name] = read_private_log_tail(data_dir / filename)
        except OSError:
            log.warning("Crash report log tail unavailable: %s", filename)
            properties[name] = ""
    return properties


def _post(payload: dict) -> bool:
    """Use HTTP timeouts plus a wall-clock bound, including slow proxy/DNS setup."""
    result = []

    def send():
        try:
            host = os.environ.get("SMARTMEMORY_CRASH_REPORT_HOST", DEFAULT_HOST).rstrip(
                "/"
            )
            with httpx.Client(timeout=5, trust_env=True) as client:
                response = client.post(host + "/i/v0/e/", json=payload)
                response.raise_for_status()
            result.append(True)
        except Exception as exc:
            # Transport errors can include user URLs. Only log the error class.
            log.warning(
                "Crash/support report could not be sent (%s)", type(exc).__name__
            )
            result.append(False)

    thread = threading.Thread(target=send, name="smartmemory-report-send", daemon=True)
    thread.start()
    thread.join(5)
    if not result:
        log.warning("Crash/support report exceeded the five-second send budget")
    return bool(result and result[0])


def _capture(
    event: str, properties: dict, data_dir: Path, omitted: tuple[str, ...] = ()
) -> ReportResult:
    key = os.environ.get("SMARTMEMORY_CRASH_REPORT_KEY", PUBLIC_PROJECT_KEY)
    if not key.startswith("phc_"):
        raise ValueError("Crash reporting requires a public project key")
    properties = private_value(properties, omitted)
    for name in ("log_tail_cli", "log_tail_daemon"):
        properties[name] = (
            properties[name].encode("utf-8")[-30_000:].decode("utf-8", errors="ignore")
        )
    payload = {
        "api_key": key,
        "event": event,
        "distinct_id": _install_id(data_dir),
        "properties": properties,
    }
    sent = _post(payload)
    return ReportResult(
        "sent" if sent else "failed", properties["report_id"] if sent else None
    )


def report_exception(
    exc: BaseException,
    *,
    source: str,
    args: list[str] | None = None,
    data_dir: Path | None = None,
    handled: bool = False,
) -> ReportResult:
    try:
        if not enabled():
            return ReportResult("disabled")
        data_dir = data_dir or debug_log_path().parent
        exceptions = _exception_list(exc, handled)
        if not _reserve(data_dir, exceptions):
            return ReportResult("suppressed")
        properties = _common(source, data_dir, args)
        properties["$exception_list"] = exceptions
        # CLI input values can be reflected in otherwise innocuous error messages.
        inputs = (
            arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
            for arg in (args or [])[1:]
            if not arg.startswith("--") or "=" in arg
        )
        omitted = tuple(
            private_text(arg)
            for arg in inputs
            if len(arg) >= 4 and "<redacted>" not in private_text(arg)
        )
        return _capture("$exception", properties, data_dir, omitted)
    except Exception as error:
        log.warning("Crash report unavailable (%s)", type(error).__name__)
        return ReportResult("failed")


def report_in_background(
    exc: BaseException,
    *,
    source: str,
    data_dir: Path | None = None,
    handled: bool = False,
) -> None:
    try:
        if enabled():
            threading.Thread(
                target=report_exception,
                args=(exc,),
                kwargs={"source": source, "data_dir": data_dir, "handled": handled},
                name="smartmemory-crash-report",
                daemon=True,
            ).start()
    except Exception as error:
        log.warning("Background crash report unavailable (%s)", type(error).__name__)


def crash_notice(exc: BaseException, args: list[str]) -> str:
    result = report_exception(exc, source="cli", args=args)
    if result.status == "sent":
        return f"Crash report sent (ID {result.report_id}). Disable: SMARTMEMORY_CRASH_REPORTS=0"
    if result.status == "failed":
        return "Crash report could not be sent; run smartmemory report --zip"
    return ""


def send_support_report(texts: dict[str, str]) -> ReportResult:
    """Explicit manual sends remain available when automatic reporting is off."""
    try:
        data_dir = debug_log_path().parent
        properties = _common("manual", data_dir, ["report", "--send"])
        properties.update(
            {
                "doctor": texts["doctor.txt"],
                "environment": texts["environment.txt"],
                "config": texts["config.txt"],
                "message": texts.get("message.txt", ""),
                "log_tail_cli": texts["cli-debug.log"],
                "log_tail_daemon": texts["daemon.log"],
            }
        )
        return _capture("smartmemory_support_report", properties, data_dir)
    except Exception as error:
        log.warning("Support report unavailable (%s)", type(error).__name__)
        return ReportResult("failed")
