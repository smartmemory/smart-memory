"""DAEMON-WARMUP-SHUTDOWN-1: SIGTERM during warmup ends the daemon inside launchd's 5 s window.

Each row starts the real ``viewer_server.main()`` (real uvicorn, the reserved test port)
in a child process, holds one request open the way a hook call did during the incident,
and sends SIGTERM at one warmup stage. Then it releases warmup and checks what the
daemon left behind: hooks published only if they were before the signal, a consumer
only if it was already running (and then stopped, worker.pid gone), the store closed,
daemon.pid gone, and the whole shutdown under 5 s.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHD_SIGKILL_AFTER_S = 5.0

CHILD = r'''
import asyncio, os, sys, time
from pathlib import Path

stage, root, port = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
data = root / "data"
queue_dir = data / "hook-queue"
queue_dir.mkdir(parents=True)


def mark(name):
    (root / name).write_text(str(time.monotonic()))


def wait_release():
    while not (root / "release").exists():
        time.sleep(0.01)


import smartmemory_app.config as config
import smartmemory_app.crash_reporter as crash_reporter
import smartmemory_app.events_server as events_server
import smartmemory_app.hook_consumer as hook_consumer
import smartmemory_app.hook_failures as hook_failures
import smartmemory_app.runtime_diagnostics as runtime_diagnostics
import smartmemory_app.setup as setup
import smartmemory_app.storage as storage
import smartmemory_app.work_graph as work_graph
from smartmemory_app import viewer_server as vs

storage._resolve_data_dir = lambda: data
storage._shutdown = lambda: mark("store-closed")
runtime_diagnostics.install_daemon_diagnostics = lambda *a, **k: None
crash_reporter.flush_in_background = lambda: None
crash_reporter.report_in_background = lambda *a, **k: None
hook_failures.consume_in_background = lambda *a: None
events_server.start_background = lambda: None
if hasattr(config, "load_llm_keys_from_stores"):
    config.load_llm_keys_from_stores = lambda: None
vs._print_llm_extraction_status = lambda: None
vs._print_enrichment_status = lambda: None
work_graph.show_reextract_offer = lambda log: None


def warm_backend():
    mark("warm-entered")
    if stage == "warming":
        wait_release()
    return True


def copy_hooks():
    mark("hooks-published")
    if stage == "hooks-synced":
        wait_release()


class Consumer(hook_consumer.DaemonConsumer):
    def start(self):
        consumer = super().start()
        mark("consumer-started")
        return consumer

    def stop(self, timeout=30.0):
        super().stop(timeout)
        mark("consumer-stopped")


vs._warm_backend = warm_backend
setup._copy_hooks = copy_hooks
hook_consumer.start_in_daemon = lambda **kw: Consumer(root=queue_dir).start()


async def hold():
    """A request still in flight at SIGTERM: it only ends when shutdown cancels it."""
    mark("hold-entered")
    while True:
        await asyncio.sleep(0.05)


vs.app.add_api_route("/__hold", hold)
vs.app.router.routes.insert(0, vs.app.router.routes.pop())  # ahead of the "/" static mount
vs.main(port=port, open_browser=False)
'''


@dataclass(frozen=True)
class Row:
    stage: str  # where warmup is parked (or has reached) when SIGTERM arrives
    ready: str  # marker that says warmup got there
    hooks_published: bool
    consumer_started: bool


ROWS = [
    Row("warming", "warm-entered", hooks_published=False, consumer_started=False),
    Row("hooks-synced", "hooks-published", hooks_published=True, consumer_started=False),
    Row("consumer-running", "consumer-started", hooks_published=True, consumer_started=True),
]


def _wait_for(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _hold_request(port: int, root: Path) -> socket.socket:
    deadline = time.monotonic() + 30
    while True:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=1)
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    conn.sendall(b"GET /__hold HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    _wait_for((root / "hold-entered").exists, "the held request to reach the app")
    return conn


@pytest.mark.parametrize("row", ROWS, ids=[r.stage for r in ROWS])
def test_sigterm_during_warmup_publishes_nothing_new_and_cleans_up(
    row, host_isolation_guard, tmp_path
):
    root = tmp_path / "daemon"
    root.mkdir()
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    log_path = tmp_path / "child.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(child), row.stage, str(root), str(host_isolation_guard.port)],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    conn = None
    try:
        _wait_for(
            lambda: (root / row.ready).exists() or proc.poll() is not None,
            f"warmup to reach {row.stage}",
        )
        assert proc.poll() is None, log_path.read_text()
        if row.consumer_started:
            _wait_for((root / "data/hook-queue/worker.pid").exists, "worker.pid")
        conn = _hold_request(host_isolation_guard.port, root)

        proc.send_signal(signal.SIGTERM)
        sent = time.monotonic()
        time.sleep(0.3)
        (root / "release").write_text("go")  # let a parked warmup carry on
        try:
            proc.wait(timeout=LAUNCHD_SIGKILL_AFTER_S + 5)
        except subprocess.TimeoutExpired:
            proc.kill()  # what launchd does at 5 s
            proc.wait()
        took = time.monotonic() - sent
    finally:
        if conn is not None:
            conn.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    print(f"{row.stage}: SIGTERM to exit {took:.2f}s")
    output = log_path.read_text()
    outcome = {
        "under 5 s": took < LAUNCHD_SIGKILL_AFTER_S,
        "exit signal": proc.returncode,
        "store closed": (root / "store-closed").exists(),
        "daemon.pid left": (root / "data/daemon.pid").exists(),
        "hooks published": (root / "hooks-published").exists(),
        "consumer started": (root / "consumer-started").exists(),
        "consumer stopped": (root / "consumer-stopped").exists(),
        "worker.pid left": (root / "data/hook-queue/worker.pid").exists(),
    }
    assert outcome == {
        "under 5 s": True,
        "exit signal": -signal.SIGTERM,
        "store closed": True,
        "daemon.pid left": False,
        "hooks published": row.hooks_published,
        "consumer started": row.consumer_started,
        "consumer stopped": row.consumer_started,
        "worker.pid left": False,
    }, f"took {took:.2f}s\n{output}"
