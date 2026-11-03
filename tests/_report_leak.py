"""Real-home report leak gate (OPS-REPORT-OUTBOX-SENDING-LEAK-1).

``real_home_report_leak_gate`` in conftest.py snapshots ``~/.smartmemory`` reporting
files before and after the session. Live ``sm`` hooks of running sessions drain the
same outbox during a run: each claims a queued report by renaming it to
``<fingerprint>.<pid>.<started>.<lease>.sending`` and puts it back as
``<fingerprint>.json`` when it exits. Neither is content a test wrote, so:

* a gained ``.sending`` claimed less than SENDING_GRACE_SECONDS ago is an in-flight
  live sender and is ignored; an older one is reported with its pid's liveness;
* a gained ``.json`` whose bytes the outbox already held under the same fingerprint
  is a claim put back, not new content.
"""

from __future__ import annotations

import time
from pathlib import Path

import psutil

from smartmemory_app import report_outbox

SENDING_GRACE_SECONDS = 5 * 60


def snapshot(root: Path) -> dict[str, bytes]:
    """Non-empty reporting files under ``root``; tolerates a live sender's renames."""
    paths = [root / "hook-failures.tsv"]
    paths.extend((root / "report-outbox").rglob("*"))
    found = {}
    for path in paths:
        try:
            if path.is_file() and path.stat().st_size:
                found[str(path)] = path.read_bytes()
        except FileNotFoundError:
            continue  # Renamed by a live sender between listing and reading.
    return found


def _pid_state(pid: str, started: str) -> str:
    try:
        process = psutil.Process(int(pid))
        same = abs(process.create_time() - int(started) / 1_000_000) < 0.01
        return "alive" if same and process.status() != psutil.STATUS_ZOMBIE else "dead"
    except (ValueError, psutil.NoSuchProcess):
        return "dead"
    except psutil.AccessDenied:
        return "alive"


def leaks(
    before: dict[str, bytes], after: dict[str, bytes], now: float | None = None
) -> list[str]:
    """Gained reporting content, minus live-sender claim churn."""
    now = time.time() if now is None else now
    held = {(Path(p).name.split(".", 1)[0], content) for p, content in before.items()}
    found = []
    for name, content in after.items():
        if before.get(name) == content:
            continue
        path = Path(name)
        fingerprint = path.name.split(".", 1)[0]
        if path.suffix == ".json" and (fingerprint, content) in held:
            continue
        if path.suffix != ".sending":
            found.append(name)
            continue
        try:
            _, pid, started, claimed_at = report_outbox._claim_parts(path)
        except (OSError, ValueError):
            found.append(name + " (unparseable claim)")
            continue
        age = now - claimed_at
        if age < SENDING_GRACE_SECONDS:
            continue
        found.append(f"{name} (pid {pid} {_pid_state(pid, started)}, {age:.0f}s old)")
    return found
