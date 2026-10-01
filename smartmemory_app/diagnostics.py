"""Credential-safe diagnostic text shared by health and lifecycle reporting."""

import re


def redact_credentials(message: str) -> str:
    """Preserve useful error text and file paths while hiding credential values."""
    # Provider keys also appear in free-text exceptions without a field name.
    message = re.sub(
        r"\b(?:gsk_|sk-(?:proj-|ant-)?)[A-Za-z0-9_-]+", "<redacted>", message
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
        r"(?i)\b([\w-]*(?:api[_ -]?key|key|token|password|secret|authorization))\b[\"\x27]?"
        r"(?:\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|"
        r"(?:(?:bearer|basic|token)\s+)?[^\s,;&\"\x27]+)",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", message)
    return message
