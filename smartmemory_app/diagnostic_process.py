"""Killable offline probes. Completed rows survive a later timed-out probe."""

import json
import os
import subprocess
import sys
import time
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def offline_checks(task: str, *, budget: float | None = None) -> list[str]:
    if task == "doctor":
        from smartmemory_app.daemon import _health_response
        from smartmemory_app.config import load_config

        cached = None
        try:
            health = _health_response(2).json()
            if (
                health.get("service") == "smartmemory"
                and health.get("status") == "ok"
                and health.get("mode") == "lite"
            ):
                cfg = load_config()
                if (
                    health.get("data_dir")
                    and Path(health["data_dir"]).resolve()
                    == Path(cfg.data_dir).expanduser().resolve()
                    and health.get("embedding_provider")
                    == cfg.embedding_provider
                    == "local"
                ):
                    cached = health.get("embedding_check")
                if not cached:
                    log.warning(
                        "Doctor cannot reuse this daemon's embedding warmup. Checking in an offline child."
                    )
        except Exception:
            pass  # No running daemon: the bounded offline child is the normal check.
        rows = []
        for phase, seconds in (
            ("owners", 40),
            ("sqlite", 6),
            ("dependencies", 6),
            ("native", 10),
            ("vectors", 10),
            ("embedding", 20),
        ):
            started = time.monotonic()
            phase_budget = min(seconds, budget) if budget is not None else seconds
            if (
                phase == "embedding"
                and isinstance(cached, str)
                and cached.startswith("Embedding runtime: OK")
            ):
                rows.append(cached)
            else:
                rows.extend(offline_checks(phase, budget=phase_budget))
            rows.append(
                f"Diagnostic phase {phase}: {time.monotonic() - started:.2f}s (budget {phase_budget:g}s)"
            )
        return rows
    budget = 6.0 if budget is None else budget
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
        labels = {
            "native": "Native libraries",
            "owners": "Write lock",
            "sqlite": "SQLite",
            "dependencies": "Dependencies",
            "vectors": "Vector load",
            "embedding": "Embedding runtime",
        }
        prefix = labels.get(task, task)
        if timeout:
            detail = f"probe exceeded {budget:g} s" + (
                "; slow cold start" if task == "embedding" else "; timed out"
            )
            log.warning("%s not checked: %s", prefix, detail)
            rows.append(f"{prefix}: not checked ({detail})")
        else:
            rows.append(f"{prefix}: warning: probe failed. Fix: smartmemory doctor")
    return rows
