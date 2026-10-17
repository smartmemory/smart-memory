"""CLI-TENANT-1: pre-flight checks for query commands.

Two failures used to look like success or like an unrelated error:

* ``sm ask`` with no LLM key anywhere.
* A remote CLI using a different workspace than the web login, which returns
  empty results from a workspace the user never looked at.

Both now say what is wrong, which names are involved and the command that fixes
it. Only names and ids are printed, never a key value.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import click

from smartmemory_app.config import LLM_KEY_ENV_VARS

log = logging.getLogger(__name__)

# llm_provider (as setup writes it) -> the env var that provider reads.
_PROVIDER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# Remote paths whose empty result looks like "no memories" but may mean "wrong workspace".
_LISTING_PATHS = ("/memory/search", "/memory/list", "/memory/recall")
_workspace_warned: set[tuple[str, str]] = set()


def _stored_llm_key_present() -> bool:
    """Whether the daemon would find a key in the keychain or shell profile.

    The daemon loads keys env, then keychain, then shell profile at boot
    (viewer_server.main), so a key missing from this shell's env can still be live.
    Only presence is returned, never a value.
    """
    for name in LLM_KEY_ENV_VARS:
        try:
            import keyring

            if keyring.get_password("smartmemory", name):
                return True
        except Exception:
            pass
        try:
            from smartmemory_app.setup import _read_env_from_profile

            if _read_env_from_profile(name):
                return True
        except Exception:
            pass
    return False


def llm_key_available() -> bool:
    return any(os.environ.get(k) for k in LLM_KEY_ENV_VARS) or _stored_llm_key_present()


def _missing_key_names(provider: str) -> str:
    chosen = _PROVIDER_KEY_ENV.get((provider or "").lower())
    if chosen:
        return chosen
    first, *rest = _PROVIDER_KEY_ENV.values()
    return f"{first} (or {', '.join(rest)})"


def no_llm_key_error(provider: str = "none") -> click.ClickException:
    """The error for a written answer with no LLM provider key anywhere."""
    names = _missing_key_names(provider)
    example = _PROVIDER_KEY_ENV.get((provider or "").lower(), "GROQ_API_KEY")
    return click.ClickException(
        "sm ask needs an LLM provider key and none is set.\n"
        f"Missing: {names}.\n"
        "Set one, then restart the daemon so it picks the key up:\n"
        f"  export {example}=<your key>\n"
        "  sm restart\n"
        "Or run `sm setup` to choose a provider.\n"
        "To list the matching memories without a written answer, run `sm ask --extractive`."
    )


def require_llm_key(cfg: Any, *, extractive: bool = False) -> None:
    """Fail before any request when local `sm ask` has no key to synthesize with.

    Remote mode answers with the hosted LLM, so a local key is not required there.
    """
    if extractive or cfg.mode == "remote" or llm_key_available():
        return
    raise no_llm_key_error(cfg.llm_provider)


def daemon_missing_key_error() -> click.ClickException:
    """The daemon answered extractively although this shell has a key."""
    return click.ClickException(
        "The SmartMemory daemon has no LLM provider key, so it returned excerpts instead of an answer.\n"
        "It was started before the key was set. Run `sm restart` so it loads the key.\n"
        "To list the matching memories without a written answer, run `sm ask --extractive`."
    )


@dataclass(frozen=True)
class WorkspaceMismatch:
    using: str
    source: str
    web_default: str
    tenant: str
    api_url: str

    def warning(self) -> str:
        tenant = f", tenant {self.tenant}" if self.tenant else ""
        if self.source == "SMARTMEMORY_TEAM_ID":
            fix = (
                "Use the web workspace: `unset SMARTMEMORY_TEAM_ID` (and remove it from your shell profile), "
                f"or set SMARTMEMORY_TEAM_ID={self.web_default}."
            )
        else:
            fix = (
                "Use the web workspace: run `smartmemory setup --mode remote`, "
                "which resets the workspace to your login default."
            )
        return (
            f"Warning: this CLI uses workspace {self.using} (from {self.source}), "
            f"but your SmartMemory login defaults to workspace {self.web_default}{tenant} at {self.api_url}. "
            "The web UI shows that workspace, so results here can differ from what you see there.\n"
            + fix
        )

    def empty_hint(self) -> str:
        return (
            f"No results in workspace {self.using}. The web UI shows workspace {self.web_default}, "
            "so the memories you expect may be there."
        )


def workspace_mismatch(cfg: Any, mem: Any) -> WorkspaceMismatch | None:
    """Compare the workspace the CLI sends with the default the login resolves to.

    Both facts come from the hosted ``/auth/me`` call RemoteMemory already makes.
    None means they agree, or the login could not be resolved (the request itself
    will then report the real error).
    """
    web_default = getattr(mem, "web_default_team_id", "") or ""
    using = getattr(mem, "_team_id", "") or ""
    if not web_default or not using or using == web_default:
        if not web_default:
            log.warning(
                "Workspace check skipped: /auth/me did not return a default workspace for %s",
                cfg.api_url,
            )
        return None
    source = (
        "SMARTMEMORY_TEAM_ID"
        if os.environ.get("SMARTMEMORY_TEAM_ID")
        else "the config file"
    )
    return WorkspaceMismatch(
        using=using,
        source=source,
        web_default=web_default,
        tenant=getattr(mem, "web_tenant_id", "") or "",
        api_url=cfg.api_url,
    )


def warn_on_workspace_mismatch(cfg: Any, mem: Any) -> WorkspaceMismatch | None:
    """Print the mismatch once per process and workspace pair, on stderr."""
    mismatch = workspace_mismatch(cfg, mem)
    if mismatch is None:
        return None
    marker = (mismatch.using, mismatch.web_default)
    if marker not in _workspace_warned:
        _workspace_warned.add(marker)
        click.echo(mismatch.warning(), err=True)
    return mismatch


def _is_empty_listing(path: str, result: Any) -> bool:
    if path in ("/memory/recall",):
        return isinstance(result, dict) and not result.get("context")
    if path in _LISTING_PATHS or path == "/memory/decisions/search":
        rows = None
        if isinstance(result, dict):
            rows = result.get("items", result.get("results", result.get("decisions")))
        elif isinstance(result, list):
            rows = result
        return rows is not None and len(rows) == 0
    return False


def note_empty_result(
    mismatch: WorkspaceMismatch | None, path: str, result: Any
) -> None:
    """An empty answer from a mismatched workspace is never reported as plain emptiness."""
    if mismatch is not None and _is_empty_listing(path, result):
        click.echo(mismatch.empty_hint(), err=True)
