"""Best-effort redacted logging for CLI and detached process failures."""

from contextvars import ContextVar
import copy
import logging
import shlex
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock

from smartmemory_app.diagnostics import redact_credentials

CLI_COMMAND_ARGS: ContextVar[list[str]] = ContextVar("cli_command_args", default=[])


class SharedRotatingFileHandler(RotatingFileHandler):
    """Serialize rollover and release the file between records for Windows sharing."""

    def __init__(self, filename, **kwargs):
        super().__init__(filename, delay=True, **kwargs)
        self.file_lock = FileLock(str(filename) + ".lock", timeout=2)

    def emit(self, record):
        # filelock's own DEBUG messages must not recursively acquire this lock.
        if record.name == "filelock" or (record.name or "").startswith("filelock."):
            return
        try:
            with self.file_lock:
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
        except Exception:
            self.handleError(record)


class RedactingFormatter(logging.Formatter):
    """Redact the fully rendered record, including chained tracebacks."""

    def format(self, record):
        return redact_credentials(super().format(record))


class ConsoleFormatter(RedactingFormatter):
    """Keep normal failures short while retaining tracebacks in debug mode."""

    def format(self, record):
        import os

        if (
            os.environ.get("SMARTMEMORY_DEBUG") != "1"
            and os.environ.get("SMARTMEMORY_LOG_LEVEL", "").upper() != "DEBUG"
        ):
            record = copy.copy(record)
            record.exc_info = None
            record.exc_text = None
        return super().format(record)


def record_cli_crash(exc: Exception, args: list[str]) -> None:
    """Write directly to the CLI file handler without a terminal traceback."""
    try:
        from smartmemory_app.bug_report import gather_environment
        from smartmemory_app.cli import _install_cli_debug_handler

        root = logging.getLogger()
        _install_cli_debug_handler(root)
        try:
            environment = gather_environment().browser_info
        except Exception:
            environment = "environment unavailable"
        record = logging.LogRecord(
            __name__,
            logging.ERROR,
            __file__,
            0,
            "Uncaught CLI failure: command=%s environment=%s",
            (shlex.join(args), environment),
            (type(exc), exc, exc.__traceback__),
        )
        for handler in root.handlers:
            if getattr(handler, "_smartmemory_debug_file", False):
                handler.handle(record)
                handler.flush()
    except Exception:
        pass  # A diagnostic failure must preserve the original command failure.


class RedactingStream:
    """Redact complete output lines before detached stdout/stderr reach disk."""

    def __init__(self, stream):
        self.stream = stream
        self.pending = ""
        self.lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text):
        with self.lock:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self.stream.write(redact_credentials(line) + "\n")
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending:
                self.stream.write(redact_credentials(self.pending))
                self.pending = ""
            self.stream.flush()


def install_daemon_diagnostics(data_dir: Path, *, redact_output: bool = False) -> None:
    """Install process/thread hooks and a redacted daemon file handler once."""
    try:
        if redact_output:
            for name in ("stdout", "stderr"):
                stream = getattr(sys, name)
                if not isinstance(stream, RedactingStream):
                    setattr(sys, name, RedactingStream(stream))
        path = data_dir / "daemon.log"
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_smartmemory_daemon_file", False):
                if Path(handler.baseFilename) == path.resolve():
                    return
                root.removeHandler(handler)
                handler.close()
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = SharedRotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=2, encoding="utf-8"
        )
        handler._smartmemory_daemon_file = True
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        # Existing console handlers must redact exception text as well.
        for console in root.handlers:
            if console is not handler:
                console.setFormatter(
                    RedactingFormatter("%(levelname)s %(name)s: %(message)s")
                )

        def capture(exc_type, exc, tb, source="daemon"):
            if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
                return
            try:
                logging.getLogger(__name__).error(
                    "Uncaught daemon failure", exc_info=(exc_type, exc, tb)
                )
            except Exception:
                pass
            from smartmemory_app.crash_reporter import (
                report_exception,
                report_in_background,
            )

            if source == "daemon":
                # The process is exiting. Wait within the send budget so a daemon
                # reporter thread is not killed before it can deliver the event.
                report_exception(exc, source=source, data_dir=data_dir)
            else:
                report_in_background(exc, source=source, data_dir=data_dir)

        sys.excepthook = capture
        threading.excepthook = lambda args: capture(
            args.exc_type, args.exc_value, args.exc_traceback, "daemon_thread"
        )
    except Exception:
        pass
