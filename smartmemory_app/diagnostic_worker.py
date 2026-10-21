"""Offline diagnostics child entry point, never constructs or repairs a store."""

import json
import sys


def main() -> None:
    task = sys.argv[1]
    if task == "native":
        from smartmemory_app.install_check import native_library_checks

        rows = native_library_checks()
    else:
        from smartmemory_app.store_diagnostics import diagnostic_rows

        rows = diagnostic_rows(task)
    for row in rows:
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
