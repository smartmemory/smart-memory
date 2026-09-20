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
import threading
import time
import webbrowser
from pathlib import Path

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
    message = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@",
        r"\1<redacted>@",
        message,
    )
    message = re.sub(
        r"(?i)\b(api[_ -]?key|token|password|secret|authorization)\b"
        r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", message)
    message = re.sub(r"(?<![A-Za-z0-9:/])/(?:[^\s'\";,)]*)", "<path>", message)
    message = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\s'\";,)]*)", "<path>", message)
    summary = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return summary[:240]


def _safe_warmup_reason(exc: BaseException) -> str:
    """Flatten grouped warmup failures into one redacted health-safe summary."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_safe_warmup_reason(item) for item in exc.exceptions)[:480]
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


def _build_app() -> FastAPI:
    app = FastAPI()

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

    @app.get("/health")
    def health():
        """Daemon health check. Used by daemon.is_running() to verify ownership."""

        from smartmemory_app.config import load_config

        cfg = load_config()
        startup_status, startup_reason = _get_startup_state()
        if startup_status in {"warming", "degraded"}:
            from smartmemory_app.config import llm_key_present

            mode = "remote" if cfg.mode == "remote" else "lite"
            response = {
                "service": "smartmemory",
                "status": startup_status,
                "memories": -1,
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
        try:
            from smartmemory_app.storage import get_memory

            mem = get_memory()
            backend_ok = mem is not None
            from smartmemory_app.remote_backend import RemoteMemory

            if not isinstance(mem, RemoteMemory):
                try:
                    from smartmemory_app.local_api import _rw_lock

                    with _rw_lock:
                        snapshot = mem._graph.backend.serialize()
                    nodes = snapshot.get("nodes", [])
                    node_count = len(
                        [n for n in nodes if n.get("memory_type") != "Version"]
                    )
                except Exception:
                    node_count = 0  # backend exists but empty/new — still healthy
        except Exception as exc:
            degraded_reason = _safe_degraded_reason(exc)
        if not backend_ok:
            degraded_reason = (
                degraded_reason or "SmartMemory could not open saved memories."
            )
            log.warning(
                "SmartMemory could not open saved memories; status is degraded "
                "and the memory count is unavailable: %s",
                degraded_reason,
            )
        # Enrichment queue status (SQLite-backed, separate worker process)
        async_info: dict = {"enabled": False}
        try:
            from smartmemory_app.enrichment_queue import stats as queue_stats

            qs = queue_stats()
            async_info = {"enabled": True, **qs}
        except Exception:
            pass

        # DIST-OBSIDIAN-LITE-1: capability block lets the Obsidian plugin (and
        # any other client) detect lite vs remote-proxy mode + which write
        # operations are available. `mode` is the source of truth — clients
        # should branch on this rather than infer from llm_provider, etc.
        from smartmemory_app.remote_backend import RemoteMemory as _RM

        mode = "remote" if isinstance(mem, _RM) else "lite"
        capabilities = _capabilities(mode)

        from smartmemory_app.config import llm_key_present

        response = {
            "service": "smartmemory",
            "status": "ok" if backend_ok else "degraded",
            "memories": node_count,
            "llm_provider": cfg.llm_provider,
            "llm_key_present": llm_key_present(),
            "embedding_provider": cfg.embedding_provider,
            "pid": os.getpid(),
            "async_enrichment": async_info,
            "mode": mode,
            "capabilities": capabilities,
        }
        if degraded_reason:
            response["degraded_reason"] = degraded_reason
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
    from smartmemory_app.hf_progress import discrete_huggingface_progress
    from smartmemory_app.storage import get_memory

    _startup_line("Loading SmartMemory...")
    started = time.perf_counter()
    backend_ok = True
    failure_reasons: list[str] = []
    _set_last_warmup_failure(None)
    with discrete_huggingface_progress(_startup_line):
        try:
            get_memory(on_progress=_startup_line)
        except Exception as exc:
            backend_ok = False
            reason = _safe_warmup_reason(exc)
            failure_reasons.append(reason)
            log.warning(
                "Saved memories and startup model prerequisites are unavailable: %s",
                reason,
            )
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
            service.embed("warmup")
            _startup_line(
                f"Search model ready ({time.perf_counter() - model_started:.1f}s)"
            )
        except Exception as exc:
            backend_ok = False
            reason = _safe_warmup_reason(exc)
            failure_reasons.append(reason)
            log.warning("The warmed search model is unavailable: %s", reason)
            _startup_line(
                "Warning: The search model could not start. "
                "Run sm doctor after startup for help."
            )

    _set_last_warmup_failure("; ".join(failure_reasons) or None)
    elapsed = time.perf_counter() - started
    if backend_ok:
        _startup_line(f"SmartMemory startup complete ({elapsed:.1f}s)")
    else:
        _startup_line(f"SmartMemory startup finished with a problem ({elapsed:.1f}s)")
    return backend_ok


def _sync_hooks() -> None:
    """Refresh installed hooks, warning explicitly when that upgrade is lost."""
    try:
        from smartmemory_app.setup import _copy_hooks

        _copy_hooks()
    except Exception as exc:
        reason = _safe_degraded_reason(exc)
        log.warning(
            "Hook sync failed; updated hook scripts were not installed: %s", reason
        )
        _startup_line(f"Warning: hook sync failed ({reason})")


def _start_background_warmup() -> threading.Thread:
    """Start model/backend warmup and publish only states actually observed."""
    _set_startup_state("warming")

    def run() -> None:
        try:
            backend_ok = _warm_backend()
            _sync_hooks()
        except BaseException as exc:
            reason = _safe_warmup_reason(exc)
            log.warning(
                "Background startup failed; saved memories remain unavailable: %s",
                reason,
            )
            _set_startup_state("degraded", reason)
            return

        if backend_ok:
            _set_startup_state("ok")
        else:
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
            "Background warmup unavailable; startup will block while models load: %s",
            _safe_degraded_reason(exc),
        )
        run()
    return thread


def main(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """Start the SmartMemory daemon.

    Starts HTTP serving while the memory backend warms in a background thread.
    ``/health`` reports ``warming`` until model/backend setup has really finished.
    """
    from smartmemory_app.storage import _shutdown, _resolve_data_dir

    data_path = _resolve_data_dir()
    data_path.mkdir(parents=True, exist_ok=True)
    pid_file = data_path / "daemon.pid"

    # Load LLM API keys: env → keychain → shell profile.
    # setup stores keys in all three locations. Keychain and profile
    # are available immediately without sourcing .zshrc in a new shell.
    from smartmemory_app.config import LLM_KEY_ENV_VARS, llm_key_present

    for key_name in LLM_KEY_ENV_VARS:
        if os.environ.get(key_name):
            continue
        # Try keychain
        try:
            import keyring

            stored = keyring.get_password("smartmemory", key_name)
            if stored:
                os.environ[key_name] = stored
                print(f"  Loaded {key_name} from keychain", flush=True)
                continue
        except Exception:
            pass
        # Try shell profile
        try:
            from smartmemory_app.setup import _read_env_from_profile

            stored = _read_env_from_profile(key_name)
            if stored:
                os.environ[key_name] = stored
                print(f"  Loaded {key_name} from shell profile", flush=True)
        except Exception:
            pass

    # no-silent-degradation: make the LLM-extraction state explicit at boot so a
    # missing key is a loud, actionable banner line — not a thing the user only
    # discovers when `add` quietly stores Tier-1-only memories.
    if llm_key_present():
        _present = [k for k in LLM_KEY_ENV_VARS if os.environ.get(k)]
        print(f"  LLM extraction: enabled ({', '.join(_present)})", flush=True)
    else:
        print(
            "  LLM extraction: DISABLED — no LLM API key found. Memories will store "
            "with Tier-1 (spaCy) extraction only; entity extraction and enrichment "
            "are off. Run `smartmemory setup` to add a key.",
            flush=True,
        )

    # Publish the process marker before warmup so stop/status lifecycle commands
    # refer to the same process that serves the observed warming health response.
    pid_file.write_text(str(os.getpid()))

    def _cleanup():
        _shutdown()
        pid_file.unlink(missing_ok=True)

    # atexit handles cleanup on normal exit AND uvicorn's graceful SIGTERM shutdown.
    # Do NOT install a custom SIGTERM handler — uvicorn needs SIGTERM to trigger
    # its graceful shutdown (drain active requests, then exit → atexit fires).
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
    print(
        "Enrichment queue: SQLite-backed (run `smartmemory worker --loop` for Tier 2)",
        flush=True,
    )

    # Warm only after all process-level lifecycle pieces are installed. The health
    # route sees the state set immediately before the thread starts; it never guesses
    # that a process with only a PID marker is warming.
    _start_background_warmup()

    if open_browser:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://localhost:{port}")
        ).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
