"""Killable offline probes. Completed rows survive a later timed-out probe."""

import json
import os
import subprocess
import sys


def offline_checks(task: str, *, budget: float = 6.0) -> list[str]:
    env = os.environ.copy()
    env.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        SMARTMEMORY_HF_ALLOW_DOWNLOAD="false",
        SMARTMEMORY_CRASH_REPORTS="0",
        PYTHONUTF8="1",
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "smartmemory_app.diagnostic_worker", task],
            env=env,
            capture_output=True,
            timeout=budget,
        )
        output = result.stdout
        failed = result.returncode != 0
        timeout = False
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        failed, timeout = True, True
    rows = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, str):
                rows.append(row)
        except ValueError:
            continue  # Libraries may print startup banners, never treat those as checks.
    if failed:
        prefix = (
            "Native libraries"
            if task == "native"
            else "Embedding runtime / remaining doctor checks"
        )
        rows.append(
            f"{prefix}: warning: {'timed out' if timeout else 'probe failed'}. Fix: smartmemory doctor"
        )
    return rows
