"""CLI-TOUR-LLM-ROUTE-1: one plain-English message per LLM-route failure.

`sm ask`, `sm tour` and `sm doctor` used to print raw exception chains
(``HTTP 502: Unable to answer ...: AuthenticationError: Error code: 401 - {...}``,
``(TypeError)``). Every failure now maps to a stable ``code`` and one message that
says what was attempted (provider, model, endpoint), which key was consulted and
where (names only, never values), the exact next command, and where to look. The
raw detail goes to the daemon log and the crash report, never to the terminal.

The daemon's 502 body is ``{"detail": <message>, "code": <code>, "llm": {...}}``.
``detail`` stays a string, so an older CLI prints the plain text too.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

LLM_KEY_MISSING = "llm_key_missing"
LLM_KEY_REJECTED = "llm_key_rejected"
LLM_PROVIDER_NONE = "llm_provider_none"
LLM_ROUTE_UNRESOLVED = "llm_route_unresolved"
LLM_REQUEST_FAILED = "llm_request_failed"
CODES = (
    LLM_KEY_MISSING,
    LLM_KEY_REJECTED,
    LLM_PROVIDER_NONE,
    LLM_ROUTE_UNRESOLVED,
    LLM_REQUEST_FAILED,
)

# Where the daemon looks for a provider key, in order (config.load_llm_keys_from_stores).
KEY_SOURCES = (
    "the environment of the shell that ran sm start or sm restart, "
    "then the keychain, then your shell profile"
)
WHERE_TO_LOOK = "Details: sm doctor, or send them to us with: sm report --send"
DOTENV_NOTE = (
    ".env files are not read: export the key in the shell, then run sm restart."
)

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/"  # core ANTHROPIC_OPENAI_BASE_URL (main)

# WRAPPER-2.0-LLM-PROVIDER-ROUTE-PORT-1: the send guard is an ALLOWLIST of normalized hosts
# (F1: the r1 denylist let any host that no vendor owns, and OpenAI's own trailing-dot and
# regional hosts, through for every provider). Hosts compare by exact string equality after
# ``normalized_host``; there is no prefix, suffix or substring match anywhere.
ANTHROPIC_FAMILY = frozenset({"anthropic", "claude-agent"})


def normalized_host(endpoint: Optional[str]) -> str:
    """``endpoint``'s host, lowercased, userinfo and port dropped, one trailing dot stripped.

    "" when the endpoint has no host (no scheme, unparsable): nothing is allowed to "".
    """
    try:
        host = urlparse(endpoint or "").hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[:-1] if host.endswith(".") else host


def is_loopback(host: str) -> bool:
    """``localhost`` or a loopback IP literal (127.0.0.0/8, ::1). ``host`` is normalized."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def core_serves_anthropic() -> bool:
    """Whether the installed core routes ``claude-*`` ids to api.anthropic.com (main core 7d792b92)."""
    try:
        from smartmemory.utils import llm as core_llm
    except ImportError:
        return False
    return hasattr(core_llm, "ANTHROPIC_OPENAI_BASE_URL")


def allowed_hosts(provider: Optional[str], llm_base_url: str = "") -> frozenset[str]:
    """The exact normalized hosts ``provider`` may send to, besides loopback for local and unknown ones.

    - A cloud provider: its hosts in ``config.LLM_PROVIDER_TABLE``. The Anthropic family
      gets api.anthropic.com only when core has the Anthropic route; on core 2.0 without it
      a call for them goes to core's OpenAI client, which is refused.
    - A local OpenAI-compatible server (ollama, lmstudio, localai): the host of
      ``llm_base_url``, else of its localhost default.
    - Anything else (an unknown or unsupported name, an unreadable config): nothing.
    """
    from smartmemory_app.config import LLM_PROVIDER_TABLE, _LOCAL_OPENAI_PROVIDERS

    picked = (provider or "").strip().lower()
    if picked in _LOCAL_OPENAI_PROVIDERS:
        host = normalized_host(llm_base_url or _LOCAL_OPENAI_PROVIDERS[picked])
        return frozenset({host} - {""})
    if picked in ANTHROPIC_FAMILY and not core_serves_anthropic():
        return frozenset()
    return frozenset(LLM_PROVIDER_TABLE.get(picked, (None, None, ()))[2])


def refused_host(
    provider: Optional[str], endpoint: Optional[str], llm_base_url: str = ""
) -> Optional[str]:
    """The normalized host ``endpoint`` would reach when the picked ``provider`` may not send there, else None.

    Allowed: the provider's own hosts (``allowed_hosts``), and loopback for a local or
    unknown provider. A cloud provider never gets loopback: its key must not go to a
    local listener it did not ask for. Provider none is never refused here: the none
    block owns it (storage._block_llm_transport). An endpoint with no host is refused as
    ``"(no host)"``.
    """
    from smartmemory_app.config import LLM_PROVIDER_KEY_ENV

    picked = (provider or "").strip().lower()
    if picked == "none":
        return None
    host = normalized_host(endpoint)
    if host and host in allowed_hosts(picked, llm_base_url):
        return None
    cloud = picked in LLM_PROVIDER_KEY_ENV or picked in ANTHROPIC_FAMILY
    if host and not cloud and is_loopback(host):
        return None
    return host or "(no host)"


class LlmProviderNone(RuntimeError):
    """Raised instead of sending when llm_provider is none (no LLM is configured)."""

    def __init__(self) -> None:
        super().__init__("llm_provider is none: no LLM is configured, nothing was sent")


# Where a refused call's endpoint came from (LlmRouteRefused.source).
SOURCE_BASE_URL = "OPENAI_BASE_URL"


def refusal_remedy(provider: str, source: Optional[str]) -> str:
    """What to do about a refused call (names only, never values)."""
    parts = []
    if source == SOURCE_BASE_URL:
        parts.append(
            "OPENAI_BASE_URL in the environment points there: unset it, or pick lmstudio or ollama "
            "with llm_base_url set to that server (sm setup), then sm restart."
        )
    if (provider or "").lower() in ANTHROPIC_FAMILY and not core_serves_anthropic():
        parts.append(
            f"llm_provider {provider} cannot be served by this install (core has no Anthropic route): "
            "install the Claude Agent SDK (claude-agent) or pick another provider with sm setup."
        )
    return " ".join(parts) or (
        "Pick the provider that endpoint belongs to with sm setup, then sm restart."
    )


class LlmRouteRefused(RuntimeError):
    """Raised instead of sending when the call would reach a host the picked provider may not use."""

    def __init__(
        self, provider: str, target: str, source: Optional[str] = None
    ) -> None:
        self.provider = provider
        self.target = target  # the normalized host, or "the Claude Agent SDK"
        self.source = source  # SOURCE_BASE_URL when the endpoint came from that env var
        super().__init__(
            f"llm_provider is {provider}, but this call would go to {target}, so it was refused "
            f"and nothing was sent. {refusal_remedy(provider, source)}"
        )


@dataclass(frozen=True)
class LlmRoute:
    """What an LLM call would use: provider label, model, endpoint and the key it needs."""

    provider: str
    model: str
    endpoint: str
    key_name: Optional[
        str
    ]  # None: the endpoint owns its credentials, or no key is used


def route_for(
    provider: Optional[str], environ: Optional[Mapping[str, str]] = None
) -> LlmRoute:
    """The route core's call_llm takes in ``environ`` (os.environ by default).

    Mirrors call_llm's order only far enough to name the key and endpoint for a
    message: an explicit OPENAI_BASE_URL owns its credentials, a pinned
    LLM_PROVIDER is core's, then the model name decides (Groq, Gemini, Claude,
    else OpenAI). The daemon has already pinned SMARTMEMORY_LLM_MODEL from config
    (storage.pin_llm_model), so the model read here is the one call_llm uses.
    """
    env = os.environ if environ is None else environ
    label = (provider or "none").lower()
    model = env.get("SMARTMEMORY_LLM_MODEL") or ""
    if not model:
        from smartmemory_app.storage import _provider_default_model

        model = _provider_default_model(label, dict(env)) or ""
    name = model.lower()
    if base := env.get("OPENAI_BASE_URL"):
        key = "GROQ_API_KEY" if "groq.com" in base else None
        return LlmRoute(label, model, base, key)
    if env.get("LLM_PROVIDER"):
        return LlmRoute(label, model, f"LLM_PROVIDER {env['LLM_PROVIDER']}", None)
    try:
        from smartmemory.utils.llm import is_groq_model
    except ImportError:  # a stubbed or older core: the bare Groq ids core uses

        def is_groq_model(model: str) -> bool:
            return model.startswith(("groq/", "openai/gpt-oss", "llama", "meta-llama/"))

    if is_groq_model(name):
        return LlmRoute(label, model, GROQ_URL, "GROQ_API_KEY")
    if name.startswith("gemini"):
        return LlmRoute(label, model, GEMINI_URL, "GEMINI_API_KEY")
    if name in ("sonnet", "opus", "haiku"):
        try:
            from smartmemory.utils.llm import _claude_agent_sdk_available

            sdk = _claude_agent_sdk_available()
        except Exception:
            sdk = False
        if sdk:
            return LlmRoute(
                label, model, "Claude Agent SDK", None
            )  # the SDK's own sign-in
        # No SDK: core expands the alias to a claude-* id and calls the Anthropic API (never OpenAI).
        return LlmRoute(label, model, ANTHROPIC_URL, "ANTHROPIC_API_KEY")
    if name.startswith(("claude", "anthropic/")):
        return LlmRoute(label, model, ANTHROPIC_URL, "ANTHROPIC_API_KEY")
    return LlmRoute(label, model or "gpt-4o-mini", OPENAI_URL, "OPENAI_API_KEY")


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class LlmFailure:
    code: str
    route: LlmRoute
    status: Optional[int] = None
    # A send-guard refusal (LlmRouteRefused): where the call was headed and why.
    refused_target: Optional[str] = None
    refused_source: Optional[str] = None

    def message(self) -> str:
        r = self.route
        if self.code == LLM_ROUTE_UNRESOLVED and self.refused_target:
            return self._refused_message()
        attempted = (
            f"SmartMemory tried to answer with provider {r.provider}, "
            f"model {r.model or '(none)'}, endpoint {r.endpoint or '(none)'}."
        )
        restart = f"  export {r.key_name or 'GROQ_API_KEY'}=<your key>\n  sm restart"
        if self.code == LLM_PROVIDER_NONE:
            return (
                "No LLM is configured (llm_provider is none), so there is no written answer. "
                "Nothing was sent to any LLM provider.\n"
                "To turn written answers on, pick a provider:\n"
                "  sm setup\n"
                "or set one directly:\n"
                "  sm config llm_provider groq\n"
                "  export GROQ_API_KEY=<your key>\n"
                "  sm restart\n"
                f"{WHERE_TO_LOOK}"
            )
        if self.code == LLM_KEY_MISSING:
            return (
                f"{attempted}\n"
                f"It needs {r.key_name}, and none was found in {KEY_SOURCES}.\n"
                f"Set it, then restart the daemon:\n{restart}\n"
                f"Or pick another provider: sm setup. {DOTENV_NOTE}\n"
                f"{WHERE_TO_LOOK}"
            )
        if self.code == LLM_KEY_REJECTED:
            key = r.key_name or "the provider key"
            return (
                f"{attempted}\n"
                f"The {r.provider} key {key} (from {KEY_SOURCES}) was rejected "
                f"({self.status or 401}).\n"
                f"Set a valid key, then restart the daemon:\n{restart}\n"
                f"Or pick another provider: sm setup.\n"
                f"{WHERE_TO_LOOK}"
            )
        if self.code == LLM_ROUTE_UNRESOLVED:
            return (
                f"{attempted}\n"
                "The LLM provider, model or endpoint could not be resolved, so nothing was sent.\n"
                "Check the setting, then restart the daemon:\n"
                "  sm config llm_provider\n"
                "  sm restart\n"
                f"{WHERE_TO_LOOK}"
            )
        status = f" (HTTP {self.status})" if self.status else ""
        return (
            f"{attempted}\n"
            f"The request failed{status}. The full error is in ~/.smartmemory/daemon.log.\n"
            "Check your network and the provider's status, then retry. To change provider: sm setup.\n"
            f"{WHERE_TO_LOOK}"
        )

    def _refused_message(self) -> str:
        r = self.route
        if r.provider in ANTHROPIC_FAMILY and not core_serves_anthropic():
            # M1: say what fixes it, and never name api.anthropic.com: core 2.0 sent this call
            # to its OpenAI client, not to Anthropic.
            return (
                f"SmartMemory tried to answer with provider {r.provider}, model {r.model or '(none)'}. "
                "Nothing was sent.\n"
                f"{refusal_remedy(r.provider, self.refused_source)}\n"
                "  pip install 'smartmemory-core[claude]'   (then: sm config llm_provider claude-agent)\n"
                "  sm setup\n"
                "  sm restart\n"
                f"{WHERE_TO_LOOK}"
            )
        return (
            f"SmartMemory tried to answer with provider {r.provider}, model {r.model or '(none)'}, "
            f"but the call was headed for {self.refused_target}, which llm_provider {r.provider} may not "
            "use, so it was refused and nothing was sent.\n"
            f"{refusal_remedy(r.provider, self.refused_source)}\n"
            f"{WHERE_TO_LOOK}"
        )

    def payload(self) -> dict[str, Any]:
        r = self.route
        return {
            "detail": self.message(),
            "code": self.code,
            "llm": {
                "provider": r.provider,
                "model": r.model,
                "endpoint": r.endpoint,
                "key_name": r.key_name,
                "key_sources": KEY_SOURCES,
            },
        }


def classify(exc: BaseException, provider: Optional[str], environ=None) -> LlmFailure:
    """Map an exception from call_llm to one stable failure code."""
    route = route_for(provider, environ)
    if isinstance(exc, LlmProviderNone):
        return LlmFailure(LLM_PROVIDER_NONE, route)
    if refused := _route_refusal(exc):
        # The endpoint is where the refused call was headed, never the route's guess.
        return LlmFailure(
            LLM_ROUTE_UNRESOLVED,
            replace(route, endpoint=refused.target),
            refused_target=refused.target,
            refused_source=refused.source,
        )
    status = _status_code(exc)
    text = str(exc)
    if status in (401, 403) or type(exc).__name__ in (
        "AuthenticationError",
        "PermissionDeniedError",
    ):
        return LlmFailure(LLM_KEY_REJECTED, route, status or 401)
    if isinstance(exc, ValueError) and "No API key found" in text:
        return LlmFailure(LLM_KEY_MISSING, route)
    if type(exc).__name__ in ("ProviderRouteRefused", "MissingModelError") or (
        isinstance(exc, (ValueError, TypeError, LookupError))
    ):
        return LlmFailure(LLM_ROUTE_UNRESOLVED, route)
    return LlmFailure(LLM_REQUEST_FAILED, route, status)


def _route_refusal(exc: BaseException) -> Optional[LlmRouteRefused]:
    """The LlmRouteRefused that ``exc`` is or was raised from (core may wrap it), else None."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, LlmRouteRefused):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def missing_key(provider: Optional[str], environ=None) -> Optional[LlmFailure]:
    """An ``llm_key_missing`` failure when the route needs a key that is not set."""
    env = os.environ if environ is None else environ
    route = route_for(provider, env)
    if route.key_name and not env.get(route.key_name):
        return LlmFailure(LLM_KEY_MISSING, route)
    return None


def provider_none(environ=None) -> LlmFailure:
    return LlmFailure(LLM_PROVIDER_NONE, route_for("none", environ))


def render(payload: Any) -> Optional[str]:
    """The message a CLI prints for a daemon body carrying a known ``code``, else None."""
    if isinstance(payload, dict) and payload.get("code") in CODES:
        detail = payload.get("detail") or payload.get("notice")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return None
