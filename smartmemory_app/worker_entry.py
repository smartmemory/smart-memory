"""Wrapper-configured entry point for the persistent core worker."""

import argparse

from smartmemory_app.storage import apply_runtime_config, _resolve_data_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

    install_daemon_diagnostics(_resolve_data_dir(args.data_dir), redact_output=True)
    apply_runtime_config()
    from smartmemory.pipeline.work_graph.worker import run_worker

    return run_worker(str(_resolve_data_dir(args.data_dir)), idle_exit=0, lease=60)


if __name__ == "__main__":
    main()
