"""Guide handled install failures through the existing anonymous reporter."""

import logging
import threading

import click

log = logging.getLogger(__name__)


def is_install_failure(exc: BaseException, args: list[str]) -> bool:
    """Click aborts and argument errors are user control flow, never failures."""
    if isinstance(exc, (click.Abort, click.UsageError, KeyboardInterrupt)):
        return False
    install_path = bool(args and args[0] in {"setup", "start", "restart", "warm"})
    cause, seen = exc, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if type(cause).__name__ in {
            "StoreBusyError",
            "MissingModelError",
            "HFModelUnavailable",
        }:
            install_path = True
        tb = cause.__traceback__
        while tb:
            module = tb.tb_frame.f_globals.get("__name__", "")
            function = tb.tb_frame.f_code.co_name
            if module == "smartmemory_app.remote_cli" and function == "require_local":
                return (
                    False  # A local-only operation requested in remote mode is usage.
                )
            if module in {
                "smartmemory_app.setup",
                "smartmemory_app.setup_tui",
                "smartmemory_app.store_reset",
            }:
                install_path = True
            if (
                module == "smartmemory_app.cli"
                and function == "_ensure_first_run_models"
            ):
                install_path = True
            if (
                module == "smartmemory_app.cli"
                and function == "_daemon_request"
                and str(exc).startswith(("Store busy.", "Store reset refused:"))
            ):
                install_path = True
            tb = tb.tb_next
        cause = cause.__cause__ or cause.__context__
    return install_path


def doctor_summary(*, timeout: float = 9.0) -> list[str]:
    """Share doctor logic in-process with one hard wall deadline."""
    from smartmemory_app.support_diagnostics import local_checks

    rows = []

    def run():
        try:
            from smartmemory_app.install_check import (
                check_installation,
                python_requirement,
            )

            status = check_installation()
            rows.append(
                f"Python: {'OK' if status.python_ok else 'FAIL'} ({python_requirement()})"
            )
            rows.append(
                f"Core: {'OK' if status.core_ok else 'FAIL'}. Fix: python -m pip install --upgrade smartmemory"
            )
            rows.extend(local_checks(budget=max(0.05, timeout - 1)))
        except Exception as exc:
            rows.append(
                f"Doctor: warning: unavailable ({type(exc).__name__}). Fix: smartmemory doctor"
            )

    thread = threading.Thread(
        target=run, name="smartmemory-install-doctor", daemon=True
    )
    thread.start()
    thread.join(min(timeout, 10.0))
    return (
        list(rows)
        if not thread.is_alive()
        else ["Doctor: timed out. Fix: smartmemory doctor"]
    )


def relevant_hint(exc: BaseException, rows: list[str]) -> str:
    text = str(exc).lower()
    if "key validation" in text or "api url" in text or "remote authentication" in text:
        return (
            "Remote authentication failed. "
            "Fix: smartmemory setup --mode remote --api-url <your-service> --api-key <valid-key>"
        )
    priorities = (
        ("busy" in text or "reset refused" in text, "Write lock"),
        ("bash" in text, "Git Bash"),
        ("model" in text or "download" in text, "Embedding runtime"),
        ("dimension" in text or "vector" in text, "Vector"),
        ("dll" in text or "native" in text, "Native"),
    )
    failed = [
        row
        for row in rows
        if any(
            word in row.lower() for word in ("fail", "warning:", "missing", "timed out")
        )
    ]
    for matches, prefix in priorities:
        if matches:
            chosen = next((row for row in failed if row.startswith(prefix)), None)
            if chosen:
                return chosen
    return next(
        (row for row in failed if "Fix:" in row),
        "Fix: smartmemory doctor, then retry smartmemory setup",
    )


def handled_install_failure(
    exc: BaseException, args: list[str], *, rows: list[str] | None = None
):
    """Never replace or hide the original Click error if diagnostics fail."""
    from smartmemory_app.crash_reporter import bounded_report, report_exception
    from smartmemory_app.report_privacy import private_text

    try:
        rows = doctor_summary() if rows is None else [*rows, *doctor_summary()]
        result = bounded_report(
            report_exception,
            exc,
            source="install",
            args=args,
            handled=True,
            context={"doctor": rows},
        )
        click.echo(
            private_text(relevant_hint(exc, rows))
            .encode("ascii", "backslashreplace")
            .decode(),
            err=True,
        )
        notice = {
            "queued": "Automatic crash report queued for the next CLI or daemon start.",
            "disabled": "Automatic crash reporting disabled (SMARTMEMORY_CRASH_REPORTS=0 or config).",
            "suppressed": "Automatic crash report suppressed (duplicate or rate limit).",
            "failed": "Automatic crash report could not be sent. Run smartmemory report --zip.",
        }.get(result.status, f"Report ID {result.report_id}")
        click.echo(notice, err=True)
        return result
    except Exception as error:
        log.warning("Install troubleshooting unavailable (%s)", type(error).__name__)
