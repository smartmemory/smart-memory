"""HOOK-WRITE-QUEUE-1 daemon-sole-consumer: one table of drain-policy cases.

The hooks only append; the daemon's consumer thread is the one reader. Each row
builds a queue (hook runs or hand-written records in the live backlog's v1 shape),
runs one action, and checks what was written, what is left, and what was logged.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
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


def _spill(
    root: Path, second: int, phase: str, session: str, n: str, body=None
) -> Path:
    """A HOOK-SPILL-INTERIM-1 spill file as the pre-queue hook writes it (its report's contract)."""
    spill = capture_queue.hook_spill_dir(root)
    spill.mkdir(exist_ok=True)
    name = f"20261025T1000{second:02d}000000Z-{100 + second}-{phase}.json"
    if body is None:
        payload = {
            "session_id": session,
            "cwd": "/w",
            "tool_name": "Bash",
            "tool_input": {"n": n},
        }
        body = json.dumps(
            {
                "v": 1,
                "phase": phase,
                "ts": "2026-10-25T10:00:00.000000Z",
                "pid": 1,
                "payload": payload,
            }
        )
    (spill / name).write_text(body + "\n")
    return spill / name


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []
        logging.getLogger("smartmemory_app.capture_queue").addHandler(self)

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())

    def close(self) -> None:
        logging.getLogger("smartmemory_app.capture_queue").removeHandler(self)
        super().close()


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
        "calls": sorted(
            writer.calls
        ),  # sessions run in parallel: only per-session order holds
        "a_order": [c for c in writer.calls if c[0] == "a"],
        "b_order": [c for c in writer.calls if c[0] == "b"],
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
        "shed_names": "session test_hc_hook tool Bash;" in log,  # what was lost (L4)
        "payloads_left": len(list((root / "payloads").iterdir())),
    }


def cap_ignores_a_pid_that_is_no_consumer(root, env, writer):
    # A worker.pid reused by another process after a daemon crash must not switch the cap off (L6);
    # a live smartmemory consumer pid does.
    _record(root, "8000-1", "observe", "s", "q1")
    _record(root, "8000-2", "observe", "s", "q2")
    (root / "worker.pid").write_text(f"{os.getpid()}\n")  # alive, but pytest
    _hook(env, "observe", "shed-by-cap", SMARTMEMORY_HOOK_QUEUE_MAX="2")
    shed = len(list((root / "queue").glob("*.json")))
    consumer_like = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "smartmemory_app.viewer_server",
        ]
    )
    try:
        (root / "worker.pid").write_text(f"{consumer_like.pid}\n")
        _hook(env, "observe", "queued-past-cap", SMARTMEMORY_HOOK_QUEUE_MAX="2")
        queued = len(list((root / "queue").glob("*.json")))
    finally:
        consumer_like.kill()
        consumer_like.wait()
    (root / "worker.pid").unlink()
    log = (Path(env["SMARTMEMORY_DATA_DIR"]) / "hooks.log").read_text()
    _run_daemon(root, writer, lambda: len(writer.calls) == 3).stop()
    return {
        "queue_after_foreign_pid": shed,
        "queue_after_consumer_pid": queued,
        "shed_lines": log.count("payload shed"),
        "calls": sorted(writer.calls),
    }


def poison_spill_name_is_dead_lettered_and_the_consumer_lives(root, env, writer):
    # M2: a name that matches the pattern but holds no valid date used to raise out of consume.
    warnings = _Warnings()
    spill = capture_queue.hook_spill_dir(root)
    spill.mkdir()
    (spill / "20261399T999999000000Z-1-observe.json").write_text("{}")
    _spill(root, 1, "observe", "s", "good")
    _record(root, "5500-1", "observe", "t", "after")
    consumer = _run_daemon(root, writer, lambda: len(writer.calls) == 2)
    alive = consumer.thread.is_alive()
    consumer.stop()
    warnings.close()
    return {
        "calls": sorted(writer.calls),
        "alive": alive,
        "dead": os.listdir(root / "dead"),
        "warned": sum("is malformed" in line for line in warnings.lines),
    }


def _failing_writes(match):
    """Patch the queue's record writer to raise for the paths ``match`` picks; returns the undo."""
    original = capture_queue._write_json_atomic

    def write(path, record):
        error = match(Path(path), record)
        if error:
            raise OSError(error, os.strerror(error), str(path))
        original(path, record)

    capture_queue._write_json_atomic = write
    return lambda: setattr(capture_queue, "_write_json_atomic", original)


def enospc_on_claim_dead_letters_the_job_and_the_consumer_lives(root, env, writer):
    # M2: claim's record write on a full disk used to kill the thread. Now: retried with backoff,
    # dead-lettered by rename after max_attempts (3), other sessions keep draining.
    warnings = _Warnings()
    _record(root, "6000-1", "observe", "s", "full")
    _record(root, "6000-2", "observe", "t", "other")
    undo = _failing_writes(
        lambda p, record: errno.ENOSPC if p.name == "6000-1.observe.json" else 0
    )
    try:
        consumer = _run_daemon(
            root,
            writer,
            lambda: writer.calls == ["other"]
            and (root / "dead" / "6000-1.observe.json").exists(),
        )
        alive = consumer.thread.is_alive()
        consumer.stop()
    finally:
        undo()
        warnings.close()
    return {
        "calls": writer.calls,
        "alive": alive,
        "dead": os.listdir(root / "dead"),
        "warned_first": sum("claim failed" in line for line in warnings.lines),
        "warned_dead": sum(
            "dead-lettered after 3 failed claim" in line for line in warnings.lines
        ),
    }


class _FailsFor(Writer):
    def __init__(self, bad: str) -> None:
        super().__init__()
        self.bad = bad

    def __call__(self, job: dict) -> None:
        if (
            json.loads(Path(job["payload_path"]).read_text())["tool_input"]["n"]
            == self.bad
        ):
            raise RuntimeError("Store down")
        super().__call__(job)


def eacces_on_settle_dead_letters_the_job_and_the_consumer_lives(root, env, writer):
    # M2: a failed job whose requeue write is refused (EACCES) used to kill the thread in _settle.
    warnings = _Warnings()
    writer = _FailsFor("bad")
    _record(root, "7000-1", "observe", "s", "bad")
    _record(root, "7000-2", "observe", "t", "other")
    undo = _failing_writes(  # the requeue write (it carries last_error), not the claim
        lambda p, record: errno.EACCES
        if p.name == "7000-1.observe.json" and "last_error" in record
        else 0
    )
    try:
        consumer = _run_daemon(
            root,
            writer,
            lambda: writer.calls == ["other"]
            and (root / "dead" / "7000-1.observe.json").exists(),
        )
        alive = consumer.thread.is_alive()
        consumer.stop()
    finally:
        undo()
        warnings.close()
    return {
        "calls": writer.calls,
        "alive": alive,
        "dead": os.listdir(root / "dead"),
        "running": os.listdir(root / "running"),
        "warned_first": sum("requeue failed" in line for line in warnings.lines),
        "warned_dead": sum(
            "dead-lettered after 3 failed requeue" in line for line in warnings.lines
        ),
    }


def persistent_spill_failure_warns_once(root, env, writer):
    # L1: a spill that cannot be written warns once and backs off, not once per 0.01 s pass.
    warnings = _Warnings()
    _spill(root, 1, "observe", "s", "late")
    (root / "payloads").chmod(0o500)
    try:
        consumer = _run_daemon(root, writer, lambda: True)
        time.sleep(1.5)
    finally:
        (root / "payloads").chmod(0o700)
    deadline = time.monotonic() + 10
    while writer.calls != ["late"] and time.monotonic() < deadline:
        time.sleep(0.05)
    consumer.stop()
    warnings.close()
    return {
        "calls": writer.calls,
        "warned": sum("not adopted" in line for line in warnings.lines),
    }


def spill_is_adopted_and_drained(root, env, writer):
    # Spilled payloads join the queue on start; each spill file is gone once queued.
    _spill(root, 1, "observe", "s", "spill-1")
    _spill(root, 2, "learn", "s", "spill-2")
    _run_daemon(root, writer, lambda: len(writer.calls) == 2).stop()
    return {
        "calls": writer.calls,
        "spill_left": os.listdir(capture_queue.hook_spill_dir(root)),
    }


def spill_keeps_time_order_with_queued_jobs(root, env, writer):
    # A session's spilled and queued jobs run in arrival order (spill name ts vs record mtime).
    _spill(root, 1, "observe", "a", "a1")
    _record(root, "5000-1", "observe", "a", "a2")
    middle = capture_queue._spill_ns("20261025T100002000000Z")
    os.utime(root / "queue" / "5000-1.observe.json", ns=(middle, middle))
    _spill(root, 3, "observe", "a", "a3")
    _run_daemon(root, writer, lambda: len(writer.calls) == 3).stop()
    return {"calls": writer.calls}


def malformed_spill_is_dead_lettered_with_a_warning(root, env, writer):
    warnings = _Warnings()
    _spill(root, 1, "observe", "s", "", body="{not json")
    (capture_queue.hook_spill_dir(root) / "stray.json").write_text("{}")
    _spill(root, 2, "observe", "s", "good")
    _run_daemon(root, writer, lambda: writer.calls == ["good"]).stop()
    warnings.close()
    return {
        "calls": writer.calls,
        "dead": sorted(os.listdir(root / "dead")),
        "warned": sum("is malformed" in line for line in warnings.lines),
    }


def spill_tmp_file_is_ignored(root, env, writer):
    tmp = capture_queue.hook_spill_dir(root) / ".tmp-77-observe"
    _spill(root, 1, "observe", "s", "done")
    tmp.write_text('{"v":1')
    _run_daemon(root, writer, lambda: writer.calls == ["done"]).stop()
    return {
        "calls": writer.calls,
        "spill_left": os.listdir(capture_queue.hook_spill_dir(root)),
    }


def spill_crash_between_adopt_and_delete_writes_once(root, env, writer):
    # A consumer killed after queueing a spill file but before deleting it: the next start
    # finds the job named after the file, deletes the file, and writes it once.
    path = _spill(root, 1, "observe", "s", "once")
    body = path.read_text()
    capture_queue.adopt_spill(root)
    path.write_text(body)  # the delete never happened
    consumer = _run_daemon(root, writer, lambda: writer.calls == ["once"])
    time.sleep(0.3)  # room for a second write, if the file were queued again
    consumer.stop()
    consumer_left = os.listdir(capture_queue.hook_spill_dir(root))
    return {"calls": writer.calls, "spill_left": consumer_left}


def spill_stays_when_the_queue_write_fails(root, env, writer):
    # The spill file is deleted only after its job is written: a failed write keeps it.
    path = _spill(root, 1, "observe", "s", "kept")
    (root / "payloads").chmod(0o500)
    try:
        adopted = capture_queue.adopt_spill(root)
    finally:
        (root / "payloads").chmod(0o700)
    kept = path.exists()
    _run_daemon(root, writer, lambda: writer.calls == ["kept"]).stop()
    return {"adopted": adopted, "kept": kept, "calls": writer.calls}


ZERO = {"queue": 0, "running": 0, "dead": 0}

ROWS = [
    (
        backlog_drains_on_start,
        {
            "calls": ["a1", "a2", "b1", "b2"],
            "a_order": ["a1", "a2"],
            "b_order": ["b1", "b2"],
        },
    ),
    (orphan_in_running_is_retried, {"calls": ["orphan"]}),
    (store_busy_is_retried_not_lost, {"calls": ["busy"]}),
    (stop_waits_for_the_inflight_write, {"calls": ["inflight"], "alive": False}),
    (hook_starts_no_process_in_local_mode, {"spawned": False, "calls": ["local"]}),
    (
        hook_sheds_past_the_cap,
        {
            "calls": ["kept-1", "kept-2"],
            "shed_warning": True,
            "shed_names": True,
            "payloads_left": 0,
        },
    ),
    (
        cap_ignores_a_pid_that_is_no_consumer,
        {
            "queue_after_foreign_pid": 2,
            "queue_after_consumer_pid": 3,
            "shed_lines": 1,
            "calls": ["q1", "q2", "queued-past-cap"],
        },
    ),
    (
        poison_spill_name_is_dead_lettered_and_the_consumer_lives,
        {
            "calls": ["after", "good"],
            "alive": True,
            "dead": ["spill-20261399T999999000000Z-1-observe.json"],
            "warned": 1,
            "_left": {"queue": 0, "running": 0, "dead": 1},
        },
    ),
    (
        enospc_on_claim_dead_letters_the_job_and_the_consumer_lives,
        {
            "calls": ["other"],
            "alive": True,
            "dead": ["6000-1.observe.json"],
            "warned_first": 1,
            "warned_dead": 1,
            "_left": {"queue": 0, "running": 0, "dead": 1},
        },
    ),
    (
        eacces_on_settle_dead_letters_the_job_and_the_consumer_lives,
        {
            "calls": ["other"],
            "alive": True,
            "dead": ["7000-1.observe.json"],
            "running": [],
            "warned_first": 1,
            "warned_dead": 1,
            "_left": {"queue": 0, "running": 0, "dead": 1},
        },
    ),
    (persistent_spill_failure_warns_once, {"calls": ["late"], "warned": 1}),
    (spill_is_adopted_and_drained, {"calls": ["spill-1", "spill-2"], "spill_left": []}),
    (spill_keeps_time_order_with_queued_jobs, {"calls": ["a1", "a2", "a3"]}),
    (
        malformed_spill_is_dead_lettered_with_a_warning,
        {
            "calls": ["good"],
            "dead": [
                "spill-20261025T100001000000Z-101-observe.json",
                "spill-stray.json",
            ],
            "warned": 2,
            "_left": {"queue": 0, "running": 0, "dead": 2},
        },
    ),
    (spill_tmp_file_is_ignored, {"calls": ["done"], "spill_left": [".tmp-77-observe"]}),
    (
        spill_stays_when_the_queue_write_fails,
        {"adopted": 0, "kept": True, "calls": ["kept"]},
    ),
    (
        spill_crash_between_adopt_and_delete_writes_once,
        {"calls": ["once"], "spill_left": []},
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
        expected = dict(expected)
        expected_left = expected.pop("_left", ZERO)
        if got != expected or left != expected_left:
            failures.append((scenario.__name__, got, left))
    assert failures == []
