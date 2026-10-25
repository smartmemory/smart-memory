"""HOOK-WRITE-QUEUE-1 daemon-sole-consumer: one table of drain-policy cases.

The hooks only append; the daemon's consumer thread is the one reader. Each row
builds a queue (hook runs or hand-written records in the live backlog's v1 shape),
runs one action, and checks what was written, what is left, and what was logged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from smartmemory_app import capture_queue, hook_consumer

HOOKS = Path(__file__).resolve().parents[2] / "smartmemory_app" / "hooks"


def _record(root: Path, name: str, phase: str, session: str, n: str) -> None:
    """A queued job exactly as the bash hook publishes it (the stranded live backlog's shape)."""
    payload = root / "payloads" / f"{name}.{phase}.json"
    payload.write_text(
        json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {"n": n}})
    )
    record = {
        "v": 1,
        "phase": phase,
        "session_id": session,
        "cwd": "/w",
        "workspace_id": None,
        "payload_path": str(payload),
        "state_snapshot": None,
    }
    (root / "queue" / f"{name}.{phase}.json").write_text(json.dumps(record) + "\n")


def _hook(env: dict, phase: str, n: str, **extra: str) -> None:
    payload = {
        "session_id": "test_hc_hook",
        "tool_name": "Bash",
        "tool_input": {"n": n},
    }
    subprocess.run(
        ["bash", str(HOOKS / f"{phase}.sh")],
        input=json.dumps(payload),
        text=True,
        env={**env, **extra},
        check=True,
        timeout=10,
    )


class Writer:
    """Stands in for the in-process store write; ``slow`` holds a job until released."""

    def __init__(self, fail_first: int = 0, slow: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_first = fail_first
        self.release = threading.Event()
        self.started = threading.Event()
        self.slow = slow

    def __call__(self, job: dict) -> None:
        n = json.loads(Path(job["payload_path"]).read_text())["tool_input"]["n"]
        self.started.set()
        if self.slow:
            self.release.wait(5)
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("Store busy")
        self.calls.append(n)


def _run_daemon(root: Path, writer: Writer, until) -> hook_consumer.DaemonConsumer:
    consumer = hook_consumer.DaemonConsumer(
        root, execute=writer, poll=0.01, max_attempts=3
    ).start()
    deadline = time.monotonic() + 10
    while not until():
        assert time.monotonic() < deadline, (
            "daemon consumer did not reach the expected state"
        )
        time.sleep(0.01)
    return consumer


def backlog_drains_on_start(root, env, writer):
    # Jobs queued while the daemon was down (two sessions, live v1 shape) drain in order on start.
    for i, (session, n) in enumerate(
        [("a", "a1"), ("b", "b1"), ("a", "a2"), ("b", "b2")]
    ):
        _record(root, f"{1000 + i}-1", "observe", session, n)
        os.utime(
            root / "queue" / f"{1000 + i}-1.observe.json", ns=(i * 10**9, i * 10**9)
        )
    _run_daemon(root, writer, lambda: len(writer.calls) == 4).stop()
    return {
        "calls": sorted(writer.calls, key=lambda n: n[1]),
        "a_order": [c for c in writer.calls if c[0] == "a"],
    }


def orphan_in_running_is_retried(root, env, writer):
    # A daemon killed mid-job leaves running/; the next start retries it.
    _record(root, "2000-1", "learn", "s", "orphan")
    (root / "queue" / "2000-1.learn.json").rename(
        root / "running" / "2000-1.learn.json"
    )
    _run_daemon(root, writer, lambda: writer.calls == ["orphan"]).stop()
    return {"calls": writer.calls}


def store_busy_is_retried_not_lost(root, env, writer):
    writer.fail_first = 1
    _record(root, "3000-1", "observe", "s", "busy")
    _run_daemon(root, writer, lambda: writer.calls == ["busy"]).stop()
    return {"calls": writer.calls}


def stop_waits_for_the_inflight_write(root, env, writer):
    writer.slow = True
    _record(root, "4000-1", "observe", "s", "inflight")
    consumer = _run_daemon(root, writer, writer.started.is_set)
    threading.Timer(0.2, writer.release.set).start()
    consumer.stop()
    return {"calls": writer.calls, "alive": consumer.thread.is_alive()}


def hook_starts_no_process_in_local_mode(root, env, writer):
    _hook(env, "observe", "local")
    time.sleep(0.2)
    spawned = (Path(env["HOME"]).parent / "stub-calls").exists()
    _run_daemon(root, writer, lambda: writer.calls == ["local"]).stop()
    return {"spawned": spawned, "calls": writer.calls}


def hook_sheds_past_the_cap(root, env, writer):
    _hook(env, "observe", "kept-1", SMARTMEMORY_HOOK_QUEUE_MAX="2")
    _hook(env, "learn", "kept-2", SMARTMEMORY_HOOK_QUEUE_MAX="2")
    _hook(env, "distill", "shed", SMARTMEMORY_HOOK_QUEUE_MAX="2")
    log = (Path(env["SMARTMEMORY_DATA_DIR"]) / "hooks.log").read_text()
    _run_daemon(
        root, writer, lambda: len(writer.calls) == 2
    ).stop()  # drains on restart
    return {
        "calls": writer.calls,
        "shed_warning": "WARNING: lifecycle distill payload shed" in log,
        "payloads_left": len(list((root / "payloads").iterdir())),
    }


ROWS = [
    (
        backlog_drains_on_start,
        {"calls": ["a1", "b1", "a2", "b2"], "a_order": ["a1", "a2"]},
    ),
    (orphan_in_running_is_retried, {"calls": ["orphan"]}),
    (store_busy_is_retried_not_lost, {"calls": ["busy"]}),
    (stop_waits_for_the_inflight_write, {"calls": ["inflight"], "alive": False}),
    (hook_starts_no_process_in_local_mode, {"spawned": False, "calls": ["local"]}),
    (
        hook_sheds_past_the_cap,
        {"calls": ["kept-1", "kept-2"], "shed_warning": True, "payloads_left": 0},
    ),
]


def test_daemon_is_the_one_consumer(tmp_path, monkeypatch, caplog):
    failures = []
    for scenario, expected in ROWS:
        base = tmp_path / scenario.__name__
        data, home, binary = base / "data", base / "home", base / "bin"
        for path in (data, home, binary):
            path.mkdir(parents=True)
        stub = binary / "smartmemory"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{base}/stub-calls"\n')
        stub.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{binary}:{os.environ['PATH']}",
            HOME=str(home),
            SMARTMEMORY_DATA_DIR=str(data),
        )
        root = capture_queue.hook_queue_dir(data / "hook-queue")
        with caplog.at_level(logging.WARNING):
            got = scenario(root, env, Writer())
        left = capture_queue.hook_counts(root)
        if got != expected or left != {"queue": 0, "running": 0, "dead": 0}:
            failures.append((scenario.__name__, got, left))
    assert failures == []
