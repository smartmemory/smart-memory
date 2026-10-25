"""FastAPI router for lifecycle endpoints — DIST-AGENT-HOOKS-1.

Mounted at /lifecycle on the root app (not under /memory).
Each endpoint creates a short-lived MemoryLifecycle instance,
loads session state, executes, saves state.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from smartmemory_app.lifecycle import STRICT_KEY, MemoryLifecycle, run_phase
from smartmemory_app.lifecycle_config import LifecycleConfig

log = logging.getLogger(__name__)

lifecycle_router = APIRouter()


def _get_lifecycle(body: dict) -> MemoryLifecycle:
    """Create a lifecycle instance from request body."""
    session_id = body.get("session_id", "unknown")
    config = LifecycleConfig.from_config(_load_lifecycle_config())
    return MemoryLifecycle(session_id, config)


def _load_lifecycle_config() -> dict:
    """Load [lifecycle] section from config.toml."""
    try:
        from smartmemory_app.config import load_config

        load_config()
        # load_config returns SmartMemoryConfig dataclass, not raw TOML.
        # For lifecycle config, read raw TOML directly.
        import tomllib
        from smartmemory_app.config import config_path

        path = config_path()
        if path.exists():
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            return raw.get("lifecycle", {})
    except Exception:
        pass
    return {}


@lifecycle_router.post("/orient")
async def orient(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    result = lc.orient(cwd=body.get("cwd"))
    return {"context": result}


@lifecycle_router.post("/recall")
async def recall(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    prompt = body.get("prompt", "")
    # cwd is load-bearing: MemoryLifecycle.recall scopes the search to the
    # workspace it resolves (HOOK-RECALL-RELEVANCE-1). Dropping it here made the
    # daemon path recall from the shared team instead of the session's workspace
    # — the same class of leak as the Cabbage demo corpus bleeding into forge
    # sessions (2026-08-23..27). The CLI path always passed it; this one did not.
    result = lc.recall(prompt, cwd=body.get("cwd"))
    return {"context": result}


# What a failed write phase loses, named in the route's WARNING.
_WRITE_PHASE_LOSS = {
    "observe": "tool observation",
    "distill": "turn pair",
    "learn": "error memory",
}


def _write_phase(phase: str, body: dict):
    """Run one hook write phase; a write that did not land is an error response, never ``ok``.

    The phase runs strict, so a failed store raises instead of being logged and
    swallowed inside ``MemoryLifecycle`` (U20.S1-HOOK-STORE-WORKSPACE-REFUSAL-1:
    the route answered ``{"status": "ok"}`` while every 2.0 store was refused).
    The CLI caller treats a non-2xx as "daemon unavailable" and runs the phase
    in-process once more (``cli.run_lifecycle_phase``).
    """
    try:
        run_phase(_get_lifecycle(body), phase, {**body, STRICT_KEY: True})
    except Exception as exc:
        lost = _WRITE_PHASE_LOSS[phase]
        log.warning(
            "Lifecycle %s write failed; %s not stored: %s: %s",
            phase,
            lost,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "phase": phase,
                "lost": lost,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    return {"status": "ok"}


@lifecycle_router.post("/observe")
async def observe(request: Request):
    return _write_phase("observe", await request.json())


@lifecycle_router.post("/distill")
async def distill(request: Request):
    return _write_phase("distill", await request.json())


@lifecycle_router.post("/learn")
async def learn(request: Request):
    return _write_phase("learn", await request.json())


@lifecycle_router.post("/persist")
async def persist(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    lc.persist(cwd=body.get("cwd"), transcript_path=body.get("transcript_path"))
    return {"status": "ok"}


@lifecycle_router.get("/status")
async def status():
    config = LifecycleConfig.from_config(_load_lifecycle_config())
    return {
        "enabled": config.enabled,
        "recall_strategy": config.recall_strategy.value,
        "orient_budget": config.orient_budget,
        "rules_card_enabled": config.rules_card_enabled,
        "rules_card_budget": config.rules_card_budget,
        "recall_budget": config.recall_budget,
    }
