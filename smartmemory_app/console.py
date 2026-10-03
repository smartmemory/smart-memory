"""Best-effort Unicode output for console and redirected CLI streams."""

import codecs
import sys


def read_utf8_stdin() -> str:
    """Decode redirected protocol/text input as UTF-8, preserving interactive streams."""
    stream = sys.stdin
    if not stream.isatty() and hasattr(stream, "buffer"):
        return stream.buffer.read().decode("utf-8-sig")
    return stream.read()


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
