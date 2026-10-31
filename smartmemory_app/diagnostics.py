"""Credential-safe diagnostic text shared by health and lifecycle reporting."""

import json
import re
from collections.abc import Iterable
from urllib.parse import quote


def _mask_secrets(message: str, secrets: Iterable[str]) -> str:
    """Replace every printed form of each exact secret value, whatever its length."""
    forms = set()
    for secret in secrets:
        if secret and secret.strip():
            for value in {secret, secret.strip()}:
                forms |= {value, json.dumps(value)[1:-1], quote(value, safe="")}
    for form in sorted(forms, key=len, reverse=True):
        message = message.replace(form, "<redacted>")
    return message


def redact_credentials(message: str, secrets: Iterable[str] = ()) -> str:
    """Preserve useful error text and file paths while hiding credential values.

    ``secrets`` are exact values the caller holds (a key it is about to store). They
    are masked by value in every printed form, because the patterns below only know
    the shape of common keys and miss any other accepted key.
    """
    message = _mask_secrets(message, secrets)
    message = re.sub(
        r"(?i)(--[\w-]*(?:key|token|password|secret|authorization))(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(r"\bhf_[A-Za-z0-9]{20,}\b", "<redacted>", message)
    # Provider keys also appear in free-text exceptions without a field name.
    message = re.sub(
        r"(?<![\w/\\.-])(?:gsk_[A-Za-z0-9]{20,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,})"
        r"(?![\w/\\-]|\.[\w])",
        "<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@",
        r"\1<redacted>@",
        message,
    )
    # Query/fragment values can carry vendor-specific signed credentials.
    message = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://[^\s?#]+)[?#][^\s\"\x27]+",
        r"\1?<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(?<![\w/\\-])([\w-]*(?:api[_ -]?key|key|token|password|secret|authorization))\b[\"\x27]?"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|"
        r"(?:(?:bearer|basic|token)\s+)?[^\s,;&\"\x27]+)",
        r"\1=<redacted>",
        message,
    )
    # Whitespace labels must be complete credential words, never suffixes such
    # as "monkey Smith". Exclude path components just as for assignments above.
    message = re.sub(
        r"(?i)(?<![\w/\\-])\b(authorization)\b\s+"
        r"(?:(?:basic|bearer|token)\s+)?\S+",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(?<![\w/\\-])\b(password|passwd|pwd|api[_ -]?key|"
        r"access[_ -]?key|secret|token|key)\b\s+\S+",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", message)
    return message
