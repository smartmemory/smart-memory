"""Bounded, anonymous PostHog crash and support reporting. Never raises."""

import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from smartmemory_app.bug_report import debug_log_path, gather_environment
from smartmemory_app.report_privacy import (
    private_text,
    endpoint_inventory,
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


def bounded_report(call, *args, **kwargs) -> ReportResult:
    """Report functions enqueue synchronously, then bound their transport wait."""
    return call(*args, **kwargs)


def flush_outbox() -> set[str]:
    from smartmemory_app import report_outbox

    try:
        return report_outbox.flush(_post, enabled)
    except Exception as exc:
        log.warning("Report outbox unavailable (%s)", type(exc).__name__)
        return set()


def flush_in_background(
    report_id: str | None = None, *, wait: float = 0.1
) -> ReportResult:
    results = []

    def run():
        results.append(flush_outbox())

    thread = threading.Thread(target=run, name="smartmemory-report-outbox", daemon=True)
    thread.start()
    thread.join(wait)
    if report_id and results and report_id in results[0]:
        return ReportResult("sent", report_id)
    return ReportResult("queued", report_id)


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


def _fingerprint(exceptions: list[dict], dedupe_key: str | None) -> str:
    frame = next(
        (f for f in reversed(exceptions[0]["stacktrace"]["frames"]) if f["in_app"]), {}
    )
    identity = (
        exceptions[0]["type"],
        frame.get("abs_path"),
        frame.get("function"),
        frame.get("lineno"),
    )
    return hashlib.sha256(json.dumps(dedupe_key or identity).encode()).hexdigest()


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
    """Transport runs outside the queue lock while its immutable claim is held."""
    try:
        host = os.environ.get("SMARTMEMORY_CRASH_REPORT_HOST", DEFAULT_HOST).rstrip("/")
        with httpx.Client(timeout=5, trust_env=True) as client:
            response = client.post(host + "/i/v0/e/", json=payload)
            response.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Crash/support report could not be sent (%s)", type(exc).__name__)
        return False


def _capture(
    event: str,
    properties: dict,
    data_dir: Path,
    omitted: tuple[str, ...] = (),
    fingerprint: str | None = None,
    dedupe_seconds: int = 600,
    hosts: tuple[str, ...] | None = None,
    wait: float = 0.4,
) -> ReportResult:
    key = os.environ.get("SMARTMEMORY_CRASH_REPORT_KEY", PUBLIC_PROJECT_KEY)
    if not key.startswith("phc_"):
        raise ValueError("Crash reporting requires a public project key")
    properties = private_value(properties, omitted, hosts=hosts)
    for name in ("log_tail_cli", "log_tail_daemon"):
        if name not in properties:
            continue  # Non-exception events carry no log tails at all.
        properties[name] = (
            properties[name].encode("utf-8")[-30_000:].decode("utf-8", errors="ignore")
        )
    payload = {
        "api_key": key,
        "event": event,
        "distinct_id": _install_id(data_dir),
        "properties": properties,
    }
    from smartmemory_app import report_outbox

    payload, accepted = report_outbox.enqueue(
        payload, fingerprint or uuid.uuid4().hex, dedupe_seconds
    )
    if not accepted:
        return ReportResult("suppressed")
    return flush_in_background(payload["properties"]["report_id"], wait=wait)


def report_exception(
    exc: BaseException,
    *,
    source: str,
    args: list[str] | None = None,
    data_dir: Path | None = None,
    handled: bool = False,
    context: dict | None = None,
    dedupe_key: str | None = None,
    dedupe_seconds: int = 600,
) -> ReportResult:
    try:
        if not enabled():
            flush_outbox()
            return ReportResult("disabled")
        data_dir = data_dir or debug_log_path().parent
        exceptions = _exception_list(exc, handled)
        properties = _common(source, data_dir, args)
        properties["$exception_list"] = exceptions
        if context:
            properties["diagnostic_step"] = next(
                (
                    frame["function"]
                    for frame in reversed(exceptions[0]["stacktrace"]["frames"])
                    if frame["in_app"]
                ),
                source,
            )
            properties["doctor"] = context.get("doctor", [])
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
        return _capture(
            "$exception",
            properties,
            data_dir,
            omitted,
            _fingerprint(exceptions, dedupe_key),
            dedupe_seconds,
            hosts=endpoint_inventory(
                sys.argv[1:] if args is None else args
            ).private_hosts,
        )
    except Exception as error:
        log.warning("Crash report unavailable (%s)", type(error).__name__)
        return ReportResult("failed")


def report_event(
    event: str,
    properties: dict,
    *,
    dedupe_key: str,
    dedupe_seconds: int = 86400,
    wait: float = 0.4,
) -> ReportResult:
    """Send one anonymous, non-exception event (e.g. a hook deadline overrun).

    Unlike ``report_exception`` this carries no log tails, no traceback and no
    command arguments: only versions, OS and the caller's bounded properties,
    which still pass the ``private_value`` text boundary. ``dedupe_key`` limits
    it to one event per install per ``dedupe_seconds``. The event is queued in
    the outbox first, then flushed for at most ``wait`` seconds; anything left
    is delivered by the next CLI or daemon start.
    """
    try:
        if not enabled():
            return ReportResult("disabled")
        data_dir = debug_log_path().parent
        env = gather_environment()
        payload = {
            "$process_person_profile": False,
            "$geoip_disable": True,
            "report_id": uuid.uuid4().hex[:12],
            "smartmemory_version": env.wrapper_version,
            "smartmemory_core_version": env.core_version,
            "python_version": env.python_version,
            "os": env.os_version,
            "source": "event",
            **properties,
        }
        fingerprint = hashlib.sha256(
            json.dumps(["event", event, dedupe_key]).encode()
        ).hexdigest()
        return _capture(
            event,
            payload,
            data_dir,
            fingerprint=fingerprint,
            dedupe_seconds=dedupe_seconds,
            wait=wait,
        )
    except Exception as error:
        log.warning("Event report unavailable (%s)", type(error).__name__)
        return ReportResult("failed")


def report_in_background(
    exc: BaseException,
    *,
    source: str,
    data_dir: Path | None = None,
    handled: bool = False,
) -> None:
    # Enqueue before returning, so a short-lived daemon cannot lose the report.
    report_exception(exc, source=source, data_dir=data_dir, handled=handled)


def crash_notice(exc: BaseException, args: list[str]) -> str:
    result = bounded_report(report_exception, exc, source="cli", args=args)
    if result.status == "sent":
        return f"Crash report sent (ID {result.report_id}). Disable: SMARTMEMORY_CRASH_REPORTS=0"
    if result.status == "failed":
        return "Crash report could not be sent; run smartmemory report --zip"
    if result.status == "queued":
        return "Crash report queued for the next CLI or daemon start. Run smartmemory report --zip for a local copy."
    return ""


def send_support_report(texts: dict[str, str]) -> ReportResult:
    """Explicit manual sends remain available when automatic reporting is off."""
    try:
        if not enabled():
            flush_outbox()
            return ReportResult("disabled")
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
