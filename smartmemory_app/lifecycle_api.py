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
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request

from smartmemory_app.lifecycle import MemoryLifecycle, run_phase
from smartmemory_app.lifecycle_config import LifecycleConfig

log = logging.getLogger(__name__)

lifecycle_router = APIRouter()


def _env_number(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        log.warning("%s=%r is not a positive number, using %s", name, raw, default)
        return default
    return value


# DAEMON-HEALTH-STARVE-1: every phase below is synchronous store, embedding and
# file-lock work that can take seconds. Run on the event loop (the old ``async def``
# bodies did), one hook call stalled every request, /health included, so the daemon
# looked dead while it served hooks. They now run on bounded pools, separate from
# the threadpool that serves /health and the viewer routes, so a burst of hook calls
# queues here instead of exhausting that one.
#
# SM-LIFECYCLE-POOL-SPLIT-1: the read phases (orient, recall) have their own pool. With
# one pool for all six, minute-long recall scans held every slot and observe, persist
# and every later recall queued behind them (DAEMON-RECALL-STARVE-1, 2026-10-30).
#   SMARTMEMORY_LIFECYCLE_THREADS       write pool: observe, distill, learn, persist (4)
#   SMARTMEMORY_LIFECYCLE_READ_THREADS  read pool: orient, recall (4)
#   SMARTMEMORY_LIFECYCLE_RECALL_CAP    seconds a read may run before it answers empty (4.5)
_PHASE_THREADS = _env_number("SMARTMEMORY_LIFECYCLE_THREADS", 4, int)
_phase_pool = ThreadPoolExecutor(
    max_workers=_PHASE_THREADS, thread_name_prefix="smartmemory-lifecycle"
)
_READ_THREADS = _env_number("SMARTMEMORY_LIFECYCLE_READ_THREADS", 4, int)
_read_pool = ThreadPoolExecutor(
    max_workers=_READ_THREADS, thread_name_prefix="smartmemory-lifecycle-read"
)
# Under the hook client's 5 s read timeout (cli._lifecycle_via_daemon), so a capped
# read reaches the client as an empty answer, not a client timeout that would start
# a cold in-process engine running into the same slow search.
_RECALL_CAP_S = _env_number("SMARTMEMORY_LIFECYCLE_RECALL_CAP", 4.5, float)
# Body field the hook client stamps on reads: absolute epoch seconds after which it
# no longer waits for the answer (cli._lifecycle_via_daemon).
CLIENT_DEADLINE_KEY = "client_deadline"
_REPLY_MARGIN_S = 0.25  # left for the answer to reach the client before its deadline

_read_counts = {"dropped": 0, "capped": 0}
_read_counts_lock = threading.Lock()


def _count(kind: str) -> int:
    with _read_counts_lock:
        _read_counts[kind] += 1
        return _read_counts[kind]


async def _off_loop(fn, /, *args, **kwargs):
    """Run blocking lifecycle write work on the phase pool (see ``_run_on``)."""
    return await _run_on(_phase_pool, fn, *args, **kwargs)


async def _run_on(pool, fn, /, *args, **kwargs):
    """Run blocking lifecycle work on ``pool``, keeping the request's contextvars.

    The work holds the store (``store_in_use``): uvicorn's shutdown abandons this thread
    at its drain bound, and the daemon must not close the store under it.
    """
    call = functools.partial(
        contextvars.copy_context().run, _holding_store, fn, *args, **kwargs
    )
    return await asyncio.get_running_loop().run_in_executor(pool, call)


def _holding_store(fn, /, *args, **kwargs):
    from smartmemory_app.storage import store_in_use

    with store_in_use():
        return fn(*args, **kwargs)


class _Expired(Exception):
    """A queued read reached a pool slot after its answer was no longer wanted."""


def _client_deadline(phase: str, body: dict) -> float | None:
    raw = body.get(CLIENT_DEADLINE_KEY)
    if raw is None:
        return None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
    ):
        log.warning(
            "Lifecycle %s ignored a non-numeric %s, applying only the %.1fs cap",
            phase,
            CLIENT_DEADLINE_KEY,
            _RECALL_CAP_S,
        )
        return None
    return float(raw)


def _drop(phase: str, why: str) -> dict:
    log.warning(
        "Lifecycle %s dropped without running: %s (dropped so far: %d)",
        phase,
        why,
        _count("dropped"),
    )
    return {"context": ""}


async def _read_phase(request: Request, phase: str, work) -> dict:
    """Run a read phase (orient, recall) on the read pool under a time cap.

    Answers ``{"context": work(body)}``. Three cases answer ``{"context": ""}``, the
    shape the hook client already treats as "nothing to inject", each with a WARNING
    and a running count:

    - the client's ``client_deadline`` has passed, or the client has disconnected,
      before the work is queued: ``work`` never runs;
    - the work was still queued at the cap or deadline: it is cancelled, or skipped
      when it reaches a slot, so ``work`` never runs;
    - the work started but did not finish within the cap: the route answers at the
      cap. Python cannot kill a thread, so that worker runs until ``work`` returns
      and holds one read slot meanwhile. The read pool's size bounds how many such
      stragglers can pile up, and observe/distill/learn/persist never wait for them
      because they run on the write pool.
    """
    body = await request.json()
    client_deadline = _client_deadline(phase, body)
    now = time.time()
    if client_deadline is not None and now >= client_deadline:
        return _drop(phase, f"client deadline passed {now - client_deadline:.2f}s ago")
    if await request.is_disconnected():
        return _drop(phase, "client disconnected")
    budget = _RECALL_CAP_S
    if client_deadline is not None:
        budget = min(budget, client_deadline - now - _REPLY_MARGIN_S)
    if budget <= 0:
        return _drop(phase, "client deadline leaves no time to answer")
    start_by = now + budget

    def run():
        late = time.time() - start_by
        if late >= 0:
            raise _Expired(f"reached a read slot {late:.2f}s past its deadline")
        return work(body)

    try:
        result = await asyncio.wait_for(_run_on(_read_pool, run), timeout=budget)
    except _Expired as exc:
        return _drop(phase, str(exc))
    except TimeoutError:
        log.warning(
            "Lifecycle %s answered empty at its %.2fs cap; a started worker "
            "finishes in the background (capped so far: %d)",
            phase,
            budget,
            _count("capped"),
        )
        return {"context": ""}
    return {"context": result}


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
    return await _read_phase(
        request, "orient", lambda body: _get_lifecycle(body).orient(cwd=body.get("cwd"))
    )


@lifecycle_router.post("/recall")
async def recall(request: Request):
    # cwd is load-bearing: MemoryLifecycle.recall scopes the search to the
    # workspace it resolves (HOOK-RECALL-RELEVANCE-1). Dropping it here made the
    # daemon path recall from the shared team instead of the session's workspace
    # — the same class of leak as the Cabbage demo corpus bleeding into forge
    # sessions (2026-08-23..27). The CLI path always passed it; this one did not.
    return await _read_phase(
        request,
        "recall",
        lambda body: _get_lifecycle(body).recall(
            body.get("prompt", ""), cwd=body.get("cwd")
        ),
    )


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
