"""DIST-DAEMON-1: SmartMemory daemon — HTTP API + static viewer + progress SSE.

Single uvicorn process serving:
  GET  /health                  →  daemon health check (root app, not /memory sub-app)
  GET  /                        →  static/index.html (LocalApp.jsx build)
  /memory/*                     →  local_api.py (graph + ingest + search + recall + clear)
  GET  /memory/progress/stream  →  SSE, fed by events_server.start_background()
                                   (PLAT-PUSH-SSE-1, daemon thread)

The module-level ``app = _build_app()`` is side-effect-free — it does not start uvicorn
or the events server. This makes the module safely importable by tests.
"""

import atexit
import logging
import os
import re
import signal
import sqlite3
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from smartmemory_app.local_api import api as _local_api

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 9014
log = logging.getLogger(__name__)
_startup_state_lock = threading.Lock()
_startup_status: str | None = None
_startup_reason: str | None = None
_last_warmup_failure: str | None = None
_embedding_check: str | None = None


def _set_startup_state(status: str | None, reason: str | None = None) -> None:
    """Publish the daemon's observed startup state for ``/health``."""
    global _startup_status, _startup_reason
    with _startup_state_lock:
        _startup_status = status
        _startup_reason = reason


def _get_startup_state() -> tuple[str | None, str | None]:
    with _startup_state_lock:
        return _startup_status, _startup_reason


def _set_last_warmup_failure(reason: str | None) -> None:
    global _last_warmup_failure
    with _startup_state_lock:
        _last_warmup_failure = reason


def _get_last_warmup_failure() -> str | None:
    with _startup_state_lock:
        return _last_warmup_failure


def _safe_degraded_reason(exc: Exception) -> str:
    """Return a short exception summary without paths or credential values."""
    message = " ".join(str(exc).split())
    from smartmemory_app.diagnostics import redact_credentials

    message = redact_credentials(message)
    message = re.sub(r"(?<![A-Za-z0-9:/])/(?:[^\s'\";,)]*)", "<path>", message)
    message = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\s'\";,)]*)", "<path>", message)
    summary = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return summary[:240]


def _safe_warmup_reason(exc: BaseException) -> str:
    """Flatten grouped warmup failures into one redacted health-safe summary."""
    if isinstance(exc, BaseExceptionGroup):
        return ", ".join(_safe_warmup_reason(item) for item in exc.exceptions)[:480]
    if isinstance(exc, Exception):
        return _safe_degraded_reason(exc)
    return type(exc).__name__


def _capabilities(mode: str) -> dict[str, bool]:
    remote = mode == "remote"
    return {
        "delete": not remote,
        "patch": not remote,
        "neighbors_direction": True,
        "quota": False,
        "auth": False,
        "lineage": True,
        "links": True,
        "decisions": False,
    }


def _count_readonly(path: str) -> int:
    """COUNT non-Version nodes on a read-only connection of its own.

    HOOK-CONSUMER-SCAN-1: WAL readers never wait on the writer, and this connection shares
    neither ``backend._lock`` nor the backend's connection, so a long store query on the
    backend cannot hold it up. The busy timeout is short: a locked file is reported, not waited on.
    """
    conn = sqlite3.connect(
        f"file:{quote(path)}?mode=ro", uri=True, timeout=0.05, check_same_thread=False
    )
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE memory_type IS NOT 'Version'"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _memory_count(backend) -> int:
    """Count non-Version nodes for /health without loading the graph.

    DAEMON-HEALTH-STARVE-1: this used to ``serialize()`` the whole store (every node
    and edge, JSON-decoded) under the viewer's read/write lock on every probe, which
    on a 1.7 GB store took longer than any health timeout. HOOK-CONSUMER-SCAN-1: a Lite
    SQLite file is now counted on a read-only connection that takes neither
    ``backend._lock`` nor the viewer's ``_rw_lock``. Only a backend with no file to open
    (an in-memory database, a non-SQLite store) still takes its lock, and /health runs
    this inside ``_BoundedProbe``, so even that never makes the probe wait.
    """
    namespace = getattr(backend, "cache_namespace", None)
    if isinstance(namespace, str) and namespace.startswith("sqlite:"):
        return _count_readonly(namespace.removeprefix("sqlite:"))
    conn = getattr(backend, "_conn", None)
    lock = getattr(backend, "_lock", None)
    if conn is not None and lock is not None:
        with lock:
            row = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE memory_type IS NOT 'Version'"
            ).fetchone()
        return int(row[0])
    from smartmemory_app.local_api import _rw_lock

    with _rw_lock:
        snapshot = backend.serialize()
    return len(
        [n for n in snapshot.get("nodes", []) if n.get("memory_type") != "Version"]
    )


# HOOK-CONSUMER-SCAN-1: /health never waits on the store. Every store-touching field runs in a
# helper thread and the probe waits at most this long for it; past that it answers with the
# last value (or none) and names the field in ``stale``.
_HEALTH_PROBE_BUDGET_S = 0.1
_HEALTH_STALE_LOG_S = 60.0


class _BoundedProbe:
    """One health field computed off the request thread, single-flight, with a wait budget."""

    def __init__(self, name: str, default, ttl: float = 0.0):
        self.name = name
        self.default = default
        self.ttl = ttl
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._has = False
        self._value = default
        self._at = 0.0
        self._warned = 0.0

    def _run(self, fn) -> None:
        try:
            value = fn()
        except Exception:
            log.warning("Health field %s unavailable", self.name, exc_info=True)
            return
        with self._lock:
            self._value, self._has, self._at = value, True, time.monotonic()

    def get(self, fn, budget: float = _HEALTH_PROBE_BUDGET_S):
        """``(value, fresh)``; not fresh when ``fn`` is still running after ``budget`` s."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                if self._has and time.monotonic() - self._at < self.ttl:
                    return self._value, True
                thread = self._thread = threading.Thread(
                    target=self._run,
                    args=(fn,),
                    name=f"smartmemory-health-{self.name}",
                    daemon=True,
                )
                thread.start()
        thread.join(budget)
        with self._lock:
            fresh = not thread.is_alive() and self._has
            value = self._value
            if not fresh and time.monotonic() - self._warned > _HEALTH_STALE_LOG_S:
                self._warned = time.monotonic()
                log.warning(
                    "Health field %s is still being read after %.2fs (store busy), "
                    "answering with %s",
                    self.name,
                    budget,
                    "the last value" if self._has else "no value",
                )
        return value, fresh


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(Exception)
    async def report_asgi_failure(request, exc):
        from starlette.responses import PlainTextResponse
        from smartmemory_app.crash_reporter import report_in_background

        report_in_background(exc, source="asgi")
        return PlainTextResponse("Internal Server Error", status_code=500)

    # Capture versions at startup — used to detect pip upgrades.
    # On every request, compare against installed versions. If different,
    # exit cleanly — launchd KeepAlive restarts with new code.
    _startup_versions: dict[str, str] = {}
    try:
        from importlib.metadata import version as _pkg_version

        _startup_versions = {
            "smartmemory": _pkg_version("smartmemory"),
            "smartmemory-core": _pkg_version("smartmemory-core"),
        }
    except Exception:
        pass

    # Both metadata lookups measure about 0.3 ms total, negligible beside the
    # request-latency target. Check every request so upgrades cannot stay stale.

    @app.middleware("http")
    async def _version_guard(request, call_next):
        """Auto-restart daemon when pip upgrade is detected.

        Checks installed package versions on every request. If a version mismatch
        is found, the daemon exits cleanly and launchd restarts it.
        """
        if _startup_versions:
            try:
                from importlib.metadata import version as _pkg_version

                for pkg, startup_ver in _startup_versions.items():
                    current = _pkg_version(pkg)
                    if current != startup_ver:
                        print(
                            f"{pkg} version changed ({startup_ver} → {current}), restarting...",
                            flush=True,
                        )
                        os._exit(0)
            except Exception:
                pass
        return await call_next(request)

    # One probe per store-touching /health field (HOOK-CONSUMER-SCAN-1). Single-flight: a
    # slow offer scan (every node, under the backend lock) is joined, never stacked.
    count_probe = _BoundedProbe("memories", -1)
    offer_probe = _BoundedProbe("reextract_offer", None)
    work_probe = _BoundedProbe("work_graph", None)

    @app.get("/health")
    def health():
        """Daemon health check. Used by daemon.is_running() to verify ownership."""

        from smartmemory_app.config import load_config

        cfg = load_config()
        startup_status, startup_reason = _get_startup_state()
        if cfg.mode == "remote":
            # A local health probe proves only this proxy can answer. Opening
            # RemoteMemory here can bootstrap hosted auth, including during stop.
            status = startup_status or "ok"
            response = {
                "service": "smartmemory",
                "status": status,
                "pid": os.getpid(),
                "mode": "remote",
                "host_role": "local_proxy_viewer",
                "local_proxy_status": status,
                "api_url": cfg.api_url,
                "workspace_id": cfg.team_id or None,
                "workspace_selection": "configured"
                if cfg.team_id
                else "hosted_default",
                "hosted_connectivity": "not_checked",
                "hosted_connectivity_hint": "Run: sm status",
                "extraction_location": "hosted",
                "hosted_extraction": "not_checked",
                "memories": -1,
                "work_graph": None,
                "reextract_offer": None,
                "llm_provider": "hosted",
                "llm_key_present": None,
                "embedding_provider": "hosted",
                "async_enrichment": {"enabled": False},
                "capabilities": _capabilities("remote"),
            }
            if status == "degraded":
                response["degraded_reason"] = (
                    startup_reason
                    or "Local proxy/viewer startup did not complete. Run sm doctor."
                )
            return response
        if startup_status in {"warming", "degraded"}:
            from smartmemory_app.config import llm_key_present

            mode = "remote" if cfg.mode == "remote" else "lite"
            response = {
                "service": "smartmemory",
                "status": startup_status,
                "memories": -1,
                "reextract_offer": None,
                "llm_provider": cfg.llm_provider,
                "llm_key_present": llm_key_present(),
                "embedding_provider": cfg.embedding_provider,
                "pid": os.getpid(),
                "async_enrichment": {"enabled": False},
                "mode": mode,
                "capabilities": _capabilities(mode),
            }
            if startup_status == "degraded":
                response["degraded_reason"] = (
                    startup_reason
                    or "SmartMemory startup warmup did not complete. Run sm doctor."
                )
            return response

        backend_ok = False
        degraded_reason = None
        node_count = -1
        mem = None
        stale: list[str] = []
        try:
            from smartmemory_app.storage import get_memory, store_in_use

            # A sync route's thread can outlive uvicorn's drain; hold the store so the
            # daemon's cleanup never closes it under this count (HOOK-CONSUMER-SHUTDOWN-RACE-1
            # F-A). While the daemon stops this raises StoreClosingError: degraded.
            with store_in_use():
                mem = get_memory()
                backend_ok = mem is not None
                from smartmemory_app.remote_backend import RemoteMemory

                if not isinstance(mem, RemoteMemory):

                    def count():
                        try:
                            with store_in_use():
                                return _memory_count(mem._graph.backend)
                        except Exception:
                            log.warning(
                                "Memory count unavailable, reporting 0", exc_info=True
                            )
                            return 0  # backend exists but empty/new — still healthy

                    node_count, fresh = count_probe.get(count)
                    if not fresh:
                        stale.append("memories")
        except Exception as exc:
            degraded_reason = _safe_degraded_reason(exc)
        if not backend_ok:
            degraded_reason = (
                degraded_reason or "SmartMemory could not open saved memories."
            )
            log.warning(
                "SmartMemory could not open saved memories, status is degraded "
                "and the memory count is unavailable: %s",
                degraded_reason,
            )
        offer = None
        if backend_ok and cfg.mode != "remote":
            try:
                from smartmemory.pipeline.work_graph.reextract import reextract_offer
                from smartmemory_app.storage import store_in_use
                from smartmemory_app.work_graph import get_work_graph

                def read_offer():
                    try:
                        with store_in_use():
                            return reextract_offer(mem, get_work_graph())
                    except Exception:
                        log.warning("Re-extraction notice unavailable", exc_info=True)
                        return None

                offer, fresh = offer_probe.get(read_offer)
                if not fresh:
                    stale.append("reextract_offer")
            except Exception:
                log.warning("Re-extraction notice unavailable", exc_info=True)
        work_info = None
        if cfg.mode != "remote":
            try:
                from smartmemory_app.work_graph import get_work_status

                def read_work():
                    try:
                        return get_work_status()
                    except Exception:
                        log.warning("Work graph counts unavailable", exc_info=True)
                        return None

                work_info, fresh = work_probe.get(read_work)
                if not fresh:
                    stale.append("work_graph")
            except Exception:
                log.warning("Work graph counts unavailable", exc_info=True)

        # DIST-OBSIDIAN-LITE-1: capability block lets the Obsidian plugin (and
        # any other client) detect lite vs remote-proxy mode + which write
        # operations are available. `mode` is the source of truth — clients
        # should branch on this rather than infer from llm_provider, etc.
        from smartmemory_app.remote_backend import RemoteMemory as _RM

        mode = "remote" if isinstance(mem, _RM) else "lite"
        capabilities = _capabilities(mode)

        from smartmemory_app.config import llm_key_present
        from smartmemory_app.storage import _resolve_data_dir

        response = {
            "service": "smartmemory",
            "status": "ok" if backend_ok else "degraded",
            "memories": node_count,
            "llm_provider": cfg.llm_provider,
            "llm_key_present": llm_key_present(),
            "embedding_provider": cfg.embedding_provider,
            "pid": os.getpid(),
            "embedding_check": _embedding_check,
            "data_dir": str(_resolve_data_dir().resolve()),
            "work_graph": work_info,
            "reextract_offer": offer,
            "async_enrichment": {
                "enabled": False
            },  # retired queue; compatibility field
            "mode": mode,
            "capabilities": capabilities,
        }
        if degraded_reason:
            response["degraded_reason"] = degraded_reason
        if stale:
            # The store was busy: these fields hold their last value (or -1 / null if none).
            response["stale"] = stale
        return response

    # DIST-AGENT-HOOKS-1: Mount lifecycle API at /lifecycle on root app
    from smartmemory_app.lifecycle_api import lifecycle_router

    app.include_router(lifecycle_router, prefix="/lifecycle")

    # Mount local_api at /memory — sub-app routes (e.g. /graph/full) become /memory/graph/full,
    # matching createFetchAdapter's expected paths (fetchAdapter.js:34-49).
    app.mount("/memory", _local_api)
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


# Module-level app — importable by tests without starting uvicorn or events server.
app = _build_app()


def _startup_line(message: str) -> None:
    """Write one complete startup line for daemon-log streaming."""
    print(message, flush=True)


def _warm_backend() -> bool:
    """Warm the local backend with timed, newline-only progress reporting."""
    global _embedding_check
    _embedding_check = None
    from smartmemory.errors import MissingModelError
    from smartmemory_app.hf_progress import discrete_huggingface_progress
    from smartmemory_app.storage import StoreClosingError, get_memory, store_in_use

    _startup_line("Loading SmartMemory...")
    started = time.perf_counter()
    backend_ok = True
    failure_reasons: list[str] = []
    _set_last_warmup_failure(None)
    with discrete_huggingface_progress(_startup_line):
        try:
            # Hold the store while it opens (HOOK-CONSUMER-WARMUP-STORE-GATE-FU-1): a
            # shutdown that begins meanwhile leaves it open, and get_memory then refuses
            # to publish it. A shutdown that began first refuses this with StoreClosingError.
            with store_in_use():
                get_memory(on_progress=_startup_line)
        except StoreClosingError:
            raise
        except Exception as exc:
            from smartmemory_app.crash_reporter import report_in_background

            report_in_background(exc, source="daemon", handled=True)
            backend_ok = False
            reason = _safe_warmup_reason(exc)
            failure_reasons.append(reason)
            log.warning(
                "Saved memories and startup model prerequisites are unavailable: %s",
                reason,
                exc_info=True,
            )
            failures = exc.exceptions if isinstance(exc, ExceptionGroup) else (exc,)
            if any(isinstance(failure, MissingModelError) for failure in failures):
                for failure in failures:
                    _startup_line(str(failure))
                _set_last_warmup_failure(reason)
                return False
            _startup_line(
                "Warning: SmartMemory could not open saved memories. "
                "Run sm doctor after startup for help."
            )

        # First embed() constructs the selected runtime. Keep this foregrounded so
        # the first real add/search request does not inherit the cold-start delay.
        _startup_line("Warming the search model...")
        model_started = time.perf_counter()
        try:
            from smartmemory.plugins.embedding import EmbeddingService

            service = EmbeddingService()
            vector = service.embed("warmup")
            from smartmemory_app.config import load_config
            from smartmemory_app.store_diagnostics import configured_dimension

            if load_config().embedding_provider == "local":
                expected = configured_dimension(service)
                if (
                    vector is None
                    or not len(vector)
                    or expected is not None
                    and len(vector) != expected
                ):
                    raise ValueError(
                        "Embedding warmup returned an empty or incorrectly sized vector"
                    )
                if expected is None:
                    log.warning("Embedding warmup dimension could not be verified")
                    _embedding_check = (
                        f"Embedding runtime: warning: {service.backend_name}, "
                        "cached startup warmup, dimension unverified"
                    )
                else:
                    _embedding_check = f"Embedding runtime: OK ({service.backend_name}, cached startup warmup, {len(vector)} dimensions)"
            _startup_line(
                f"Search model ready ({time.perf_counter() - model_started:.1f}s)"
            )
        except Exception as exc:
            from smartmemory_app.crash_reporter import report_in_background

            report_in_background(exc, source="daemon", handled=True)
            backend_ok = False
            reason = _safe_warmup_reason(exc)
            failure_reasons.append(reason)
            log.warning(
                "The warmed search model is unavailable: %s", reason, exc_info=True
            )
            _startup_line(
                "Warning: The search model could not start. "
                "Run sm doctor after startup for help."
            )

    _set_last_warmup_failure(", ".join(failure_reasons) or None)
    elapsed = time.perf_counter() - started
    if backend_ok:
        _startup_line(f"SmartMemory startup complete ({elapsed:.1f}s)")
    else:
        _startup_line(f"SmartMemory startup finished with a problem ({elapsed:.1f}s)")
    return backend_ok


# DAEMON-WARMUP-SHUTDOWN-1: launchd sends SIGKILL 5 s after SIGTERM, so the request
# drain and the hook consumer drain are bounded to leave the store close room inside it.
_GRACEFUL_SHUTDOWN_S = 1.5
_HOOK_CONSUMER_STOP_S = 1.5
# HOOK-CONSUMER-SHUTDOWN-RACE-1: once the consumer has stopped, other work on the store
# (the /lifecycle phase pool) gets this long to finish before the daemon exits with the
# store left open rather than close it underneath that work.
_STORE_IDLE_WAIT_S = 0.25

# Set once SIGTERM or a stop request is seen. Warmup checks it, under the lock, before it
# publishes hooks or starts the consumer, and shutdown sets it under the same lock, so
# neither can happen after shutdown began.
_shutdown_requested = threading.Event()
_hook_lifecycle_lock = threading.Lock()

# The serving uvicorn server. Its SIGTERM/SIGINT handler (and ours, outside serve) sets
# ``should_exit``, so the warmup guards read that flag directly: shutdown is seen the
# moment the signal is handled, not on the stop watcher's next 50 ms tick
# (DAEMON-WARMUP-SIGNAL-RACE-FU-1). Signal handlers stay flag-only, no lock is taken.
_lifecycle_server = None


def _shutdown_begun() -> bool:
    """True once a stop request, SIGTERM or SIGINT has been handled for this lifecycle."""
    return _shutdown_requested.is_set() or bool(
        getattr(_lifecycle_server, "should_exit", False)
    )


def _request_shutdown() -> None:
    """Block hook sync and consumer start; waits out one already in progress."""
    with _hook_lifecycle_lock:
        _shutdown_requested.set()


def _sync_hooks() -> None:
    """Refresh installed hooks, warning explicitly when that upgrade is lost."""
    with _hook_lifecycle_lock:
        if _shutdown_begun():
            log.warning(
                "Hook sync skipped, the daemon is shutting down: updated hook scripts were not installed"
            )
            return
        try:
            from smartmemory_app.config import load_config
            from smartmemory_app.setup import _copy_hooks

            if not load_config().hook_sync:
                log.info(
                    "Hook sync off (hook_sync = false in config or SMARTMEMORY_HOOK_SYNC=0): "
                    "installed hooks left as they are"
                )
                return
            _copy_hooks()
        except Exception as exc:
            reason = _safe_degraded_reason(exc)
            log.warning(
                "Hook sync failed, updated hook scripts were not installed: %s", reason
            )
            _startup_line(f"Warning: hook sync failed ({reason})")


_hook_consumer = None


def _start_hook_consumer() -> None:
    """HOOK-WRITE-QUEUE-1: the daemon is the one consumer of the hook write queue.

    Started once the backend is warm, on its own thread, never on the event loop.
    With the backend down the jobs wait on disk and drain on the next start.
    """
    global _hook_consumer
    with _hook_lifecycle_lock:
        if _hook_consumer is not None:
            return
        if _shutdown_begun():
            log.warning(
                "Hook queue consumer not started, the daemon is shutting down. "
                "Hook writes wait on disk for the next start"
            )
            return
        try:
            from smartmemory_app.hook_consumer import start_in_daemon

            _hook_consumer = start_in_daemon()
        except Exception as exc:
            reason = _safe_degraded_reason(exc)
            log.warning(
                "Hook queue consumer did not start, hook writes wait on disk: %s",
                reason,
            )
            _startup_line(f"Warning: hook queue consumer did not start ({reason})")


def _stop_hook_consumer() -> bool:
    """Stop the consumer; False when jobs still run after the bound (retried on the next start)."""
    global _hook_consumer
    _request_shutdown()
    with _hook_lifecycle_lock:
        consumer, _hook_consumer = _hook_consumer, None
    if consumer is None:
        return True
    return consumer.stop(timeout=_HOOK_CONSUMER_STOP_S)


def _stop_hook_consumer_then_close_store() -> bool:
    """Close the store only after every hook job and gated store user has ended.

    A job still running at the bound keeps the store open: the process then exits without
    closing it (closing under a running statement crashed the daemon, SIGSEGV), the job's
    uncommitted write rolls back and it is retried from running/ on the next start.
    Returns True when the store was closed.
    """
    from smartmemory_app.storage import close_store_when_idle

    stopped = _stop_hook_consumer()
    return close_store_when_idle(_STORE_IDLE_WAIT_S if stopped else 0.0)


def _start_background_warmup() -> threading.Thread:
    """Start model/backend warmup and publish only states actually observed."""
    # A warmup begins a new daemon lifecycle; main() starts it before any stop path exists.
    global _lifecycle_server
    _lifecycle_server = None
    _shutdown_requested.clear()
    from smartmemory_app.storage import open_store_gate

    open_store_gate()
    _set_startup_state("warming")

    def run() -> None:
        from smartmemory_app.storage import StoreClosingError, store_in_use

        try:
            backend_ok = _warm_backend()
            if _shutdown_begun():
                log.warning(
                    "Shutdown requested during warmup: hooks not synced and hook queue consumer "
                    "not started. Hook writes wait on disk for the next start"
                )
                return
            if backend_ok:
                from smartmemory_app.work_graph import show_reextract_offer

                # The notice walks the whole store: hold it, so cleanup never closes the
                # connection under this scan (HOOK-CONSUMER-WARMUP-STORE-GATE-FU-1).
                with store_in_use():
                    show_reextract_offer(log.info)
            _sync_hooks()
        except StoreClosingError:
            log.warning(
                "Shutdown began before startup finished with saved memories: hooks not synced "
                "and hook queue consumer not started. Hook writes wait on disk for the next start"
            )
            return
        except BaseException as exc:
            from smartmemory_app.crash_reporter import report_in_background

            report_in_background(exc, source="daemon", handled=True)
            reason = _safe_warmup_reason(exc)
            log.warning(
                "Background startup failed, saved memories remain unavailable: %s",
                reason,
                exc_info=True,
            )
            _set_startup_state("degraded", reason)
            return

        if backend_ok:
            _set_startup_state("ok")
            _start_hook_consumer()
        else:
            log.warning(
                "Hook queue consumer not started (backend not warm), hook writes wait on disk until a healthy start"
            )
            _set_startup_state(
                "degraded",
                _get_last_warmup_failure()
                or "SmartMemory startup warmup did not complete. Run sm doctor.",
            )

    thread = threading.Thread(
        target=run,
        name="smartmemory-backend-warmup",
        daemon=True,
    )
    try:
        thread.start()
    except Exception as exc:
        log.warning(
            "Background warmup unavailable, startup will block while models load: %s",
            _safe_degraded_reason(exc),
        )
        run()
    return thread


def _print_llm_extraction_status() -> None:
    """Disclose the effective LLM route without hiding local extraction/enrichment."""
    from smartmemory_app.config import load_config

    if load_config().mode == "remote":
        print(
            "  LLM extraction: runs on the hosted service (capability not checked here).",
            flush=True,
        )
        print("  Hosted connectivity: not checked here. Run: sm status", flush=True)
        return
    from smartmemory_app.storage import apply_runtime_config, llm_extraction_warning

    apply_runtime_config()
    warning = llm_extraction_warning()
    if warning:
        print(f"  {warning}", flush=True)
    else:
        print("  LLM extraction: enabled (deferred)", flush=True)


def _print_enrichment_status() -> None:
    """Name the active enrichment host without probing either backend."""
    from smartmemory_app.config import load_config

    if load_config().mode == "remote":
        print(
            "Enrichment: runs on the hosted service, not in a local worker.", flush=True
        )
    else:
        print(
            "Enrichment queue: SQLite-backed (run `smartmemory worker --loop` for Tier 2)",
            flush=True,
        )


def main(port: int | None = None, open_browser: bool = True) -> None:
    """Start the SmartMemory daemon.

    Starts HTTP serving while the memory backend warms in a background thread.
    ``/health`` reports ``warming`` until model/backend setup has really finished.
    No ``port`` means the configured one (``SMARTMEMORY_DAEMON_PORT``, then the
    config file, then ``DEFAULT_PORT``), never a hard-coded 9014 (CLI-TEST-HOST-ISOLATION-1).
    """
    if port is None:
        from smartmemory_app.config import load_config

        port = load_config().daemon_port
    from smartmemory_app.storage import _resolve_data_dir

    from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

    data_path = _resolve_data_dir()
    install_daemon_diagnostics(data_path, redact_output=True)
    from smartmemory_app.crash_reporter import flush_in_background
    from smartmemory_app.hook_failures import consume_in_background, marker_directory

    flush_in_background()
    consume_in_background(marker_directory())
    data_path.mkdir(parents=True, exist_ok=True)
    pid_file = data_path / "daemon.pid"

    # Load LLM API keys: env → keychain → shell profile.
    # setup stores keys in all three locations. Keychain and profile
    # are available immediately without sourcing .zshrc in a new shell.
    from smartmemory_app.config import load_llm_keys_from_stores

    load_llm_keys_from_stores()

    # no-silent-degradation: the boot banner and per-add disclosure use the same
    # core route decision, including configured local and subscription providers.
    _print_llm_extraction_status()

    # Publish the process marker before warmup so stop/status lifecycle commands
    # refer to the same process that serves the observed warming health response.
    pid_file.write_text(str(os.getpid()))

    cleaned = False

    def _cleanup():
        nonlocal cleaned
        if cleaned:
            return
        # Finish in-flight hook writes, then close the store only if nothing still runs on it.
        _stop_hook_consumer_then_close_store()
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(
            os.getpid()
        ):
            pid_file.unlink(missing_ok=True)
        cleaned = True

    # _run_until_stopped runs _cleanup after SIGTERM and stop requests; atexit covers
    # other normal exits. uvicorn re-raises SIGTERM once it has drained, which with the
    # default handler killed the process before any cleanup (DAEMON-WARMUP-SHUTDOWN-1).
    atexit.register(_cleanup)

    # Start the sink drain loop as a background daemon thread. It binds no
    # port of its own: PLAT-PUSH-SSE-1 deleted the ws://:9015 server, so
    # events reach the browser only over GET /memory/progress/stream on this
    # same uvicorn port.
    from smartmemory_app.events_server import start_background

    start_background()

    # Enrichment is handled by a separate worker process (smartmemory worker --loop).
    # The ingest endpoint enqueues to a SQLite table; the worker drains it.
    # No in-process threading — avoids the _drain_running import bug and
    # keeps the daemon process stable.
    _print_enrichment_status()

    # Warm only after all process-level lifecycle pieces are installed. The health
    # route sees the state set immediately before the thread starts; it never guesses
    # that a process with only a PID marker is warming.
    _start_background_warmup()

    if open_browser:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://localhost:{port}")
        ).start()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            log_config=None,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
        )
    )
    _run_until_stopped(server, data_path, os.getpid(), _cleanup)


def _install_sigterm_handler(server, received: list[int]):
    """Catch the SIGTERM uvicorn re-raises after draining, so cleanup runs first.

    uvicorn swaps in its own handler while it serves and restores this one after, then
    raises the captured signal again. Returns the previous handler, or None off the main
    thread. The handler only sets flags (no locks): the warmup guards read
    ``should_exit`` directly and the stop watcher relays it to ``_shutdown_requested``.
    """
    if threading.current_thread() is not threading.main_thread():
        return None

    def on_sigterm(signum, frame):
        received.append(signum)
        server.should_exit = True

    return signal.signal(signal.SIGTERM, on_sigterm)


def _run_until_stopped(server, data_path: Path, pid: int, cleanup) -> None:
    """Observe PID-specific local IPC and let uvicorn drain before closing stores."""
    request = data_path / f".daemon.stop-{pid}"
    finished = threading.Event()
    received: list[int] = []

    def observe_stop():
        while not finished.wait(0.05):
            if request.exists():
                server.should_exit = True
            if getattr(server, "should_exit", False):
                # SIGTERM (uvicorn's handler) or a stop request: warmup must not
                # publish hooks or start the consumer from here on.
                _shutdown_requested.set()
                return

    global _lifecycle_server
    _lifecycle_server = server  # before any handler can set should_exit on it
    previous = _install_sigterm_handler(server, received)
    watcher = threading.Thread(
        target=observe_stop, name="smartmemory-stop", daemon=True
    )
    watcher.start()
    try:
        server.run()
    finally:
        finished.set()
        watcher.join(timeout=1)
        try:
            cleanup()
            request.unlink(missing_ok=True)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
                if received:
                    # Exit the way the caller asked, now that the stores are closed.
                    signal.raise_signal(signal.SIGTERM)


if __name__ == "__main__":
    main()
