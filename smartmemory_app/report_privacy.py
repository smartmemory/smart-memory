"""Privacy boundary for diagnostics uploaded to support."""

import re
import os
import json
import getpass
import ipaddress
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from smartmemory_app.diagnostics import redact_credentials

_BODY = re.compile(
    r"(?i)content|daemon (?:request|response):|\b(?:kwargs|body|request_body|response_body)\s*[:=]|command="
)
_RECORD = re.compile(r"^(?:\d{4}-\d\d-\d\d|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\b)")
_URL = re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^\s\"']+")
_PROFILE = re.compile(
    r"(?i)(?:[a-z]:[/\\]+Users[/\\]+|/(?:Users|home)/)[^/\\\"'\r\n:;,<>|]+"
)


def _private_url(match: re.Match) -> str:
    value = match[0]
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        public = host in {"api.smartmemory.ai", "localhost"}
        try:
            public |= ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        return value if public else f"{url.scheme}://<custom-host>"
    except ValueError:
        return value.split(":", 1)[0] + "://<custom-host>"


@dataclass(frozen=True)
class EndpointInventory:
    proxies: dict[str, str]
    private_hosts: tuple[str, ...]


def endpoint_inventory(args: list[str] | None = None) -> EndpointInventory:
    """One source inventory for probes and uploads, including unsaved CLI URLs."""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}

    urls = [
        value
        for name, value in os.environ.items()
        if name.lower()
        in {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "smartmemory_api_url",
            "smartmemory_llm_base_url",
            "openai_base_url",
            "hf_endpoint",
        }
    ]
    urls.extend(value for name, value in proxies.items() if name != "no")
    # Values stay in memory only. Both --api-url URL and --api-url=URL work.
    urls.extend(
        arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        for arg in (args or [])
        if "://" in arg
    )
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None and constants.ENDPOINT != "https://huggingface.co":
        urls.append(constants.ENDPOINT)
    try:
        from smartmemory_app.config import load_config

        cfg = load_config()
        urls.extend((cfg.api_url or "", cfg.llm_base_url or ""))
    except Exception:
        pass
    hosts = set()
    for value in urls:
        try:
            host = urlsplit(value if "://" in value else "//" + value).hostname
            if not host or host.lower() in {"localhost", "api.smartmemory.ai"}:
                continue
            try:
                if ipaddress.ip_address(host).is_loopback:
                    continue
            except ValueError:
                pass
            hosts.add(host)
        except ValueError:
            continue
    return EndpointInventory(proxies, tuple(sorted(hosts, key=len, reverse=True)))


def known_private_hosts() -> tuple[str, ...]:
    """Compatibility helper using the same discovery as network probes."""
    return endpoint_inventory().private_hosts


# These keys belong to our outgoing schema, never to a user identity.
_SCHEMA_KEYS = frozenset(
    """
$process_person_profile $geoip_disable report_id smartmemory_version smartmemory_core_version
python_version os command source log_tail_cli log_tail_daemon $exception_list diagnostic_step
doctor environment config message type value mechanism handled synthetic stacktrace frames
filename abs_path function lineno in_app platform status check
""".split()
)


def private_text(value: str, *, hosts: tuple[str, ...] | None = None) -> str:
    """Remove credentials and the home prefix without collecting user identity."""
    value = redact_credentials(value)
    usernames = set()
    for name in ("USERNAME", "USER"):
        if os.environ.get(name):
            usernames.add(os.environ[name])
    try:
        usernames.add(getpass.getuser())
    except (OSError, KeyError):
        pass
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
    value = _PROFILE.sub("~", value)
    value = _URL.sub(_private_url, value)
    for host in known_private_hosts() if hosts is None else hosts:
        value = re.sub(re.escape(host), "<custom-host>", value, flags=re.IGNORECASE)
    for username in sorted(usernames, key=len, reverse=True):
        if username:
            value = re.sub(
                r"<[^>]*>|" + re.escape(username),
                lambda match: match[0] if match[0].startswith("<") else "<user>",
                value,
                flags=re.IGNORECASE,
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


def private_value(
    value, omitted: tuple[str, ...] = (), *, hosts: tuple[str, ...] | None = None
):
    """Apply the text boundary to every string in the outgoing properties."""
    if hosts is None:
        hosts = known_private_hosts()
    if isinstance(value, str):
        text = private_text(value, hosts=hosts)
        for item in omitted:
            text = text.replace(item, "<omitted>")
        return text
    if isinstance(value, dict):
        return {
            (
                key if key in _SCHEMA_KEYS else private_text(str(key), hosts=hosts)
            ): private_value(item, omitted, hosts=hosts)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [private_value(item, omitted, hosts=hosts) for item in value]
    return value
