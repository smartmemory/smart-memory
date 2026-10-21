"""Wrapper-configured entry point for the persistent core worker."""

import argparse

from smartmemory_app.storage import apply_runtime_config, _resolve_data_dir


def run_configured_worker(data_dir, *, idle_exit: float, lease: float) -> int:
    """Apply the persisted provider config, then run core's work-graph worker.

    The one place the wrapper starts core's ``run_worker``: without
    ``apply_runtime_config()`` first, core's ``get_default_model()`` ranks an
    importable Claude Agent SDK above the configured provider (TOUR-ASK-502-1).
    """
    apply_runtime_config()
    from smartmemory.pipeline.work_graph.worker import run_worker

    return run_worker(str(data_dir), idle_exit=idle_exit, lease=lease)


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
