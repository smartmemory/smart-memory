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

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

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


class LlmProviderNone(RuntimeError):
    """Raised instead of sending when llm_provider is none (no LLM is configured)."""

    def __init__(self) -> None:
        super().__init__("llm_provider is none: no LLM is configured, nothing was sent")


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
    if name.startswith(("claude", "anthropic/")) or name in ("sonnet", "opus", "haiku"):
        return LlmRoute(
            label, model, "Claude", None
        )  # SDK sign-in or ANTHROPIC_API_KEY
    if name.startswith("deepseek"):
        return LlmRoute(label, model, "DeepSeek", "DEEPSEEK_API_KEY")
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

    def message(self) -> str:
        r = self.route
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
