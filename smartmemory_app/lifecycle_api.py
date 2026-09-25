"""FastAPI router for lifecycle endpoints — DIST-AGENT-HOOKS-1.

Mounted at /lifecycle on the root app (not under /memory).
Each endpoint creates a short-lived MemoryLifecycle instance,
loads session state, executes, saves state.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from smartmemory_app.lifecycle import MemoryLifecycle, as_text
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


@lifecycle_router.post("/observe")
async def observe(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    lc.observe(
        tool_name=body.get("tool_name", "unknown"),
        tool_input=body.get("tool_input", {}),
        tool_result=as_text(body.get("tool_response")),
        transcript_path=body.get("transcript_path"),
        cwd=body.get("cwd"),
    )
    return {"status": "ok"}


@lifecycle_router.post("/distill")
async def distill(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    lc.distill(response=body.get("last_assistant_message", ""), cwd=body.get("cwd"))
    return {"status": "ok"}


@lifecycle_router.post("/learn")
async def learn(request: Request):
    body = await request.json()
    lc = _get_lifecycle(body)
    lc.learn(
        tool_name=body.get("tool_name", "unknown"),
        error=as_text(body.get("error") or body.get("tool_response")),
        cwd=body.get("cwd"),
    )
    return {"status": "ok"}


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
        "recall_budget": config.recall_budget,
    }
