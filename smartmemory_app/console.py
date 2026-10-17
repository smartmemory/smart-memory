"""Best-effort Unicode output for console and redirected CLI streams."""

import codecs
import sys


def read_utf8_stdin() -> str:
    """Decode redirected protocol/text input as UTF-8, preserving interactive streams."""
    stream = sys.stdin
    if not stream.isatty() and hasattr(stream, "buffer"):
        return stream.buffer.read().decode("utf-8-sig")
    return stream.read()


def interactive_terminal() -> bool:
    """True only when stdin, stdout and stderr are all terminals.

    A full-screen app (Textual) draws with cursor and colour escapes, and it writes some
    of them to stderr. Any one stream that is a pipe or a file would receive those bytes,
    so an app must not start unless all three are terminals.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                return False
        except Exception:
            return False
    return True


NO_TERMINAL_MESSAGE = "{name} needs a terminal. Run it directly in a terminal window."


def configure_console_encoding() -> None:
    """Use UTF-8 on legacy streams without breaking embedded/captured consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not hasattr(stream, "reconfigure"):
                continue
            encoding = getattr(stream, "encoding", None)
            if encoding and codecs.lookup(encoding).name == "utf-8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # A closed/read-only stream or an embedding's replacement stream
            # must never prevent the command from running.
            pass
