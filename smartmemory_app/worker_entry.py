"""Wrapper-configured entry point for the persistent core worker."""

import argparse
import inspect
import logging

from smartmemory_app.storage import apply_runtime_config, _resolve_data_dir

logger = logging.getLogger(__name__)


def run_configured_worker(data_dir, *, idle_exit: float, lease: float) -> int:
    """Apply the persisted provider config, then run core's work-graph worker.

    The one place the wrapper starts core's ``run_worker``: without
    ``apply_runtime_config()`` first, core's ``get_default_model()`` ranks an
    importable Claude Agent SDK above the configured provider (TOUR-ASK-502-1).
    """
    apply_runtime_config()
    from smartmemory.pipeline.work_graph.worker import run_worker

    return run_worker(
        str(data_dir), idle_exit=idle_exit, lease=lease, **_v2_kwargs(data_dir, run_worker)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

    install_daemon_diagnostics(_resolve_data_dir(args.data_dir), redact_output=True)
    return run_configured_worker(
        _resolve_data_dir(args.data_dir), idle_exit=0, lease=60
    )


if __name__ == "__main__":
    main()
