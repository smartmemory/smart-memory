"""Wrapper-configured entry point for the persistent core worker."""

import argparse
import inspect
import logging
import subprocess
import sys
from pathlib import Path

from smartmemory_app.storage import apply_runtime_config, _resolve_data_dir

logger = logging.getLogger(__name__)

#: Core's on-demand worker exits after this many idle seconds (core ``work_graph/spawn.py``).
ON_DEMAND_IDLE_EXIT = 300


def spawn_worker_if_idle(data_dir) -> bool:
    """Core's on-demand spawn, launching this held entry instead of ``smartmemory.cli worker run``.

    WRAPPER-2.0-LLM-PROVIDER-NONE-PORT-1: core's worker never imports the wrapper, so it
    runs without ``apply_runtime_config()``: no provider-none hold, no route pin. It then
    sent LLM work queued before a switch to ``none`` to whatever provider was ambient.
    Same lock, log and idle exit as core's; only the entry module differs.
    """
    from smartmemory.pipeline.work_graph.spawn import worker_is_running
    from smartmemory.utils.process import spawn_detached

    try:
        path = Path(data_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if worker_is_running(path):
            return False
        with (path / "worker.log").open("ab") as output:
            spawn_detached(
                [
                    sys.executable,
                    "-m",
                    "smartmemory_app.worker_entry",
                    "--data-dir",
                    str(path),
                    "--idle-exit",
                    str(ON_DEMAND_IDLE_EXIT),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        return True
    except Exception:
        logger.warning(
            "Worker spawn failed; deferred work waits for the next worker",
            exc_info=True,
        )
        return False


def install_worker_spawner() -> None:
    """Point core's ``spawn_worker_if_idle`` at :func:`spawn_worker_if_idle`, always.

    Core's factory imports the spawner inside ``create_lite_memory`` (call time), so
    every store built after this runs gets the held entry. A spawner already replaced
    in this process (a test fake) is left alone.
    """
    from smartmemory.pipeline.work_graph import spawn

    current = spawn.spawn_worker_if_idle
    if current is spawn_worker_if_idle:
        return
    if getattr(current, "__module__", None) != spawn.__name__:
        logger.warning(
            "Core worker spawner already replaced in this process (%s.%s); left as is",
            getattr(current, "__module__", "?"),
            getattr(current, "__qualname__", "?"),
        )
        return
    spawn.spawn_worker_if_idle = spawn_worker_if_idle


def run_configured_worker(data_dir, *, idle_exit: float, lease: float) -> int:
    """Apply the persisted provider config, then run core's work-graph worker.

    The one place the wrapper starts core's ``run_worker``: without
    ``apply_runtime_config()`` first, core's ``get_default_model()`` ranks an
    importable Claude Agent SDK above the configured provider (TOUR-ASK-502-1).
    """
    apply_runtime_config()
    from smartmemory.pipeline.work_graph.worker import run_worker

    return run_worker(
        str(data_dir),
        idle_exit=idle_exit,
        lease=lease,
        **_v2_kwargs(data_dir, run_worker),
    )


def _v2_kwargs(data_dir, run_worker) -> dict:
    """``{"v2": cfg}`` only for a core whose ``run_worker`` accepts ``v2``; ``{}`` runs the legacy drain.

    An older core has neither ``lite_handlers`` nor a ``v2`` parameter, so passing ``v2=None`` to it raises TypeError.
    """
    if "v2" not in inspect.signature(run_worker).parameters:
        logger.warning(
            "core run_worker has no v2 parameter (older core); the worker runs the legacy drain only, no v2 records"
        )
        return {}
    return {"v2": _v2_config(data_dir)}


def _v2_config(data_dir):
    """Core's switch-gated v2 worker config (U20.S1-HANDLERS): None, the legacy drain, while the switch is OFF."""
    try:
        from smartmemory.pipeline.work_graph.lite_handlers import lite_v2_worker_config
    except ImportError:
        logger.warning(
            "core has no lite_handlers (older core); the worker runs the legacy drain only, no v2 records"
        )
        return None
    return lite_v2_worker_config(str(data_dir))


def main():
    from smartmemory_app.billing_mode import bind_lite_billing_mode

    bind_lite_billing_mode()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--idle-exit", type=float, default=0.0)
    args = parser.parse_args()
    from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

    install_daemon_diagnostics(_resolve_data_dir(args.data_dir), redact_output=True)
    return run_configured_worker(
        _resolve_data_dir(args.data_dir), idle_exit=args.idle_exit, lease=60
    )


if __name__ == "__main__":
    main()
