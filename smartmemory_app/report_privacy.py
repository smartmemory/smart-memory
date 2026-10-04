"""Privacy boundary for diagnostics uploaded to support."""

import re
import os
import json
from pathlib import Path

from smartmemory_app.diagnostics import redact_credentials

_BODY = re.compile(
    r"(?i)content|daemon (?:request|response):|\b(?:kwargs|body|request_body|response_body)\s*[:=]|command="
)
_RECORD = re.compile(r"^(?:\d{4}-\d\d-\d\d|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\b)")


def private_text(value: str) -> str:
    """Remove credentials and the home prefix without collecting user identity."""
    value = redact_credentials(value)
    prefixes = {str(Path.home())}
    prefixes.update(
        os.environ[name]
        for name in ("USERPROFILE", "HOME", "LOCALAPPDATA", "APPDATA")
        if os.environ.get(name)
    )
    # An isolated HOME may be nested inside the actual Windows user profile.
    for prefix in tuple(prefixes):
        match = re.match(r"(?i)^([a-z]:[/\\]Users[/\\][^/\\]+)", prefix)
        if match:
            prefixes.add(match[1])
    variants = {}
    for prefix in prefixes:
        prefix = prefix.rstrip("/\\")
        if not prefix or re.fullmatch(r"[a-zA-Z]:", prefix):
            continue
        windows = bool(re.match(r"[a-zA-Z]:[/\\]|\\\\", prefix))
        for form in (prefix, prefix.replace("\\", "/")):
            for variant in (
                form,
                json.dumps(form, ensure_ascii=False)[1:-1],
                json.dumps(form)[1:-1],
            ):
                variants[variant] = windows
    for prefix in sorted(variants, key=len, reverse=True):
        value = re.sub(
            re.escape(prefix),
            "~",
            value,
            flags=re.IGNORECASE if variants[prefix] else 0,
        )
    return value


def safe_log_text(
    value: str, *, max_bytes: int = 30_000, skip_to_record: bool = False
) -> str:
    """Drop body-bearing records, continuations and traceback source-code lines."""
    rows = []
    dropping = skip_to_record
    for line in value.splitlines():
        if _RECORD.match(line):
            dropping = False
        if _BODY.search(line):
            dropping = True
        # Indented lines may contain source literals or multiline memory bodies.
        if dropping or line[:1].isspace():
            continue
        rows.append(line)
    text = private_text("\n".join(rows))
    return text.encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")


def read_private_log_tail(path: Path) -> str:
    """A byte-truncated body record must not become an unlabelled log message."""
    from smartmemory_app.bug_report import read_log_tail

    text = read_log_tail(path, max_bytes=60_000)
    if not text:
        return ""
    return safe_log_text(text, skip_to_record=path.stat().st_size > 60_000)


def safe_command(args: list[str]) -> str:
    """Keep command/option names only. Positional values can be stored memories."""
    command = args[0] if args and re.fullmatch(r"[a-z][a-z-]*", args[0]) else ""
    flags = [
        arg.split("=", 1)[0]
        for arg in args
        if re.fullmatch(r"--[a-z][a-z-]*(?:=.*)?", arg)
    ]
    return private_text(" ".join([command, *flags]).strip())


def private_value(value, omitted: tuple[str, ...] = ()):
    """Apply the text boundary to every string in the outgoing properties."""
    if isinstance(value, str):
        text = private_text(value)
        for item in omitted:
            text = text.replace(item, "<omitted>")
        return text
    if isinstance(value, dict):
        return {
            private_text(str(key)): private_value(item, omitted)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [private_value(item, omitted) for item in value]
    return value
