"""FastAPI router for lifecycle endpoints — DIST-AGENT-HOOKS-1.

Mounted at /lifecycle on the root app (not under /memory).
Each endpoint creates a short-lived MemoryLifecycle instance,
loads session state, executes, saves state.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request

from smartmemory_app.lifecycle import MemoryLifecycle, run_phase
from smartmemory_app.lifecycle_config import LifecycleConfig

log = logging.getLogger(__name__)

lifecycle_router = APIRouter()

# DAEMON-HEALTH-STARVE-1: every phase below is synchronous store, embedding and
# file-lock work that can take seconds. Run on the event loop (the old ``async def``
# bodies did), one hook call stalled every request, /health included, so the daemon
# looked dead while it served hooks. They now run on this bounded pool, separate from
# the threadpool that serves /health and the viewer routes, so a burst of hook calls
# queues here instead of exhausting that one.
_PHASE_THREADS = max(1, int(os.environ.get("SMARTMEMORY_LIFECYCLE_THREADS", "4") or 4))
_phase_pool = ThreadPoolExecutor(
    max_workers=_PHASE_THREADS, thread_name_prefix="smartmemory-lifecycle"
)


async def _off_loop(fn, /, *args, **kwargs):
    """Run blocking lifecycle work on the phase pool, keeping the request's contextvars."""
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(_phase_pool, call)


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
    lc = await _off_loop(_get_lifecycle, body)
    result = await _off_loop(lc.orient, cwd=body.get("cwd"))
    return {"context": result}


@lifecycle_router.post("/recall")
async def recall(request: Request):
    body = await request.json()
    lc = await _off_loop(_get_lifecycle, body)
    prompt = body.get("prompt", "")
    # cwd is load-bearing: MemoryLifecycle.recall scopes the search to the
    # workspace it resolves (HOOK-RECALL-RELEVANCE-1). Dropping it here made the
    # daemon path recall from the shared team instead of the session's workspace
    # — the same class of leak as the Cabbage demo corpus bleeding into forge
    # sessions (2026-08-23..27). The CLI path always passed it; this one did not.
    result = await _off_loop(lc.recall, prompt, cwd=body.get("cwd"))
    return {"context": result}


@lifecycle_router.post("/observe")
async def observe(request: Request):
    body = await request.json()
    await _off_loop(lambda: run_phase(_get_lifecycle(body), "observe", body))
    return {"status": "ok"}


@lifecycle_router.post("/distill")
async def distill(request: Request):
    body = await request.json()
    await _off_loop(lambda: run_phase(_get_lifecycle(body), "distill", body))
    return {"status": "ok"}


@lifecycle_router.post("/learn")
async def learn(request: Request):
    body = await request.json()
    await _off_loop(lambda: run_phase(_get_lifecycle(body), "learn", body))
    return {"status": "ok"}


@lifecycle_router.post("/persist")
async def persist(request: Request):
    body = await request.json()
    lc = await _off_loop(_get_lifecycle, body)
    await _off_loop(
        lc.persist, cwd=body.get("cwd"), transcript_path=body.get("transcript_path")
    )
    return {"status": "ok"}


@lifecycle_router.get("/status")
def status():  # reads config.toml: sync, so FastAPI runs it off the loop
    config = LifecycleConfig.from_config(_load_lifecycle_config())
    return {
        "enabled": config.enabled,
        "recall_strategy": config.recall_strategy.value,
        "orient_budget": config.orient_budget,
        "rules_card_enabled": config.rules_card_enabled,
        "rules_card_budget": config.rules_card_budget,
        "recall_budget": config.recall_budget,
    }
