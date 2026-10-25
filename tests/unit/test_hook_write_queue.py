"""HOOK-WRITE-QUEUE-1: the observe, learn and distill hooks enqueue and return; the daemon consumes.

Probes (units/HOOK-WRITE-QUEUE-1/filed.md): enqueue shape per phase, per-session order across
interleaved sessions, retry then dead-letter, 20 concurrent hooks with nothing dropped, remote mode
through the same queue, hook wall time under 50 ms, and no drop branch left in the hook scripts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartmemory_app import capture_queue, cli, hook_consumer, lifecycle_api, storage
from smartmemory_app.lifecycle import STRICT_KEY, USER_TURN_KEY, WORKSPACE_KEY
from smartmemory_app.lifecycle import MemoryLifecycle

HOOKS = Path(__file__).resolve().parents[2] / "smartmemory_app" / "hooks"
WRITE_PHASES = ("observe", "learn", "distill")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A hook environment rooted in tmp_path, with a `smartmemory` stub that only records argv."""
    data = tmp_path / "data"
    home = tmp_path / "home"
    binary = tmp_path / "bin"
    for path in (data, home, binary):
        path.mkdir()
    stub = binary / "smartmemory"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/stub-calls"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data))
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    hook_env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", HOME=str(home))
    hook_env.pop("SMARTMEMORY_WORKSPACE_ID", None)
    return hook_env


def queue_root(env: dict) -> Path:
    return Path(env["SMARTMEMORY_DATA_DIR"]) / "hook-queue"


def run_hook(
    phase: str, payload: dict, env: dict, **extra_env: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOKS / f"{phase}.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**env, **extra_env},
        timeout=10,
    )


def records(env: dict) -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted((queue_root(env) / "queue").glob("*.json"))
    ]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    """Replace the write with a recorder; ``fail`` maps a marker to how many calls should raise."""
    calls: list[dict] = []
    fail: dict[str, int] = {}
    lock = threading.Lock()
    active: dict[str, int] = {}
    overlap: list[str] = []

    def fake(lc, phase, body):
        strict = body.get(STRICT_KEY) is True
        workspace_id = body.get(WORKSPACE_KEY)
        user_turn = body.get(USER_TURN_KEY)
        session = body.get("session_id")
        with lock:
            active[session] = active.get(session, 0) + 1
            if active[session] > 1:
                overlap.append(session)
        try:
            time.sleep(0.005)
            marker = (body.get("tool_input") or {}).get("n")
            with lock:
                if fail.get(marker, 0) > 0:
                    fail[marker] -= 1
                    raise RuntimeError(f"store down for {marker}")
                calls.append(
                    {
                        "phase": phase,
                        "session": session,
                        "n": marker,
                        "strict": strict,
                        "workspace_id": workspace_id,
                        "user_turn": user_turn,
                    }
                )
        finally:
            with lock:
                active[session] -= 1

    # Patch both bindings: lifecycle_api imports run_phase by name, so a fake set only on
    # lifecycle would freeze into lifecycle_api if it were first imported under the patch.
    monkeypatch.setattr("smartmemory_app.lifecycle.run_phase", fake)
    monkeypatch.setattr(lifecycle_api, "run_phase", fake)
    monkeypatch.setattr(
        "smartmemory_app.lifecycle_api._get_lifecycle", lambda body: None
    )
    return SimpleNamespace(calls=calls, fail=fail, overlap=overlap)


def drain(env: dict, **kwargs) -> int:
    kwargs.setdefault("idle_exit", 0.0)
    kwargs.setdefault("poll", 0.01)
    return hook_consumer.consume(queue_root(env), **kwargs)


# ── Probe 1: enqueue shape per phase (persist row: capture_queue.enqueue) ─────────────

SHAPE_ROWS = [
    # phase, payload, expected record fields beyond phase/session/cwd/workspace
    (
        "observe",
        {"tool_name": "Bash", "tool_input": {"n": "o"}},
        {"state_snapshot": None},
    ),
    ("learn", {"tool_name": "Bash", "error": "boom"}, {"state_snapshot": None}),
    ("distill", {"last_assistant_message": "answer"}, {"state_snapshot": "snapshot"}),
    ("persist", {"transcript_path": "transcript.jsonl"}, {}),
]


@pytest.mark.parametrize("phase,payload,expected", SHAPE_ROWS)
def test_enqueue_shape(phase, payload, expected, env, tmp_path, monkeypatch):
    body = {"session_id": "test_hwq_shape-1", "cwd": "/work/a b", **payload}
    if phase == "persist":
        # SessionEnd keeps its own durable queue: assert capture_queue.enqueue's record.
        monkeypatch.setattr(capture_queue, "spawn_worker", Mock())
        monkeypatch.setattr(
            "smartmemory_app.lifecycle.derive_workspace_id", lambda cwd: "test_hwq_ws"
        )
        transcript = tmp_path / body["transcript_path"]
        transcript.write_text("{}\n")
        MemoryLifecycle(body["session_id"]).persist(
            cwd=body["cwd"], transcript_path=str(transcript)
        )
        (job,) = capture_queue.jobs()
        assert {
            k: job[k]
            for k in ("session_id", "transcript_path", "workspace_id", "cwd", "status")
        } == {
            "session_id": "test_hwq_shape-1",
            "transcript_path": str(transcript.resolve()),
            "workspace_id": "test_hwq_ws",
            "cwd": "/work/a b",
            "status": "queued",
        }
        capture_queue.spawn_worker.assert_called_once_with()
        return
    sessions = Path(env["SMARTMEMORY_DATA_DIR"]) / "sessions"
    sessions.mkdir()
    (sessions / "test_hwq_shape-1.json").write_text(
        json.dumps({"current_user_turn": "the prompt"})
    )
    result = run_hook(phase, body, env, SMARTMEMORY_WORKSPACE_ID="test_hwq_ws")
    assert result.returncode == 0 and result.stdout == ""
    (record,) = records(env)
    assert {
        k: record[k] for k in ("v", "phase", "session_id", "cwd", "workspace_id")
    } == {
        "v": 1,
        "phase": phase,
        "session_id": "test_hwq_shape-1",
        "cwd": "/work/a b",
        "workspace_id": "test_hwq_ws",
    }
    assert json.loads(Path(record["payload_path"]).read_text()) == body
    if expected["state_snapshot"]:
        assert json.loads(Path(record["state_snapshot"]).read_text()) == {
            "current_user_turn": "the prompt"
        }
    else:
        assert record["state_snapshot"] is None
    # Local mode: the daemon is the one consumer, so the hook started no process at all.
    time.sleep(0.2)
    assert not (tmp_path / "stub-calls").exists()


def test_drained_job_runs_the_lifecycle_phase_strict(env, monkeypatch):
    """The consumer calls the MemoryLifecycle methods in-process, strict, with the job's pin: no HTTP hop."""
    http_hop = Mock()
    monkeypatch.setattr(cli, "_lifecycle_request", http_hop)
    monkeypatch.setattr(
        "smartmemory_app.lifecycle_api._load_lifecycle_config", lambda: {}
    )
    methods = {phase: Mock() for phase in WRITE_PHASES}
    for phase, method in methods.items():
        monkeypatch.setattr(MemoryLifecycle, phase, method)
    sessions = Path(env["SMARTMEMORY_DATA_DIR"]) / "sessions"
    sessions.mkdir()
    (sessions / "test_hwq_run.json").write_text(
        json.dumps({"current_user_turn": "the prompt"})
    )
    for phase in WRITE_PHASES:
        payload = {
            "session_id": "test_hwq_run",
            "cwd": "/w",
            "tool_name": "Bash",
            "last_assistant_message": "r",
        }
        assert (
            run_hook(
                phase, payload, env, SMARTMEMORY_WORKSPACE_ID="test_hwq_pin"
            ).returncode
            == 0
        )
    # A later prompt replaces the live state before the drainer runs: distill keeps the snapshot.
    (sessions / "test_hwq_run.json").write_text(
        json.dumps({"current_user_turn": "a later prompt"})
    )
    assert drain(env) == 3
    for phase, method in methods.items():
        kwargs = method.call_args.kwargs
        assert (kwargs["cwd"], kwargs["workspace_id"], kwargs["strict"]) == (
            "/w",
            "test_hwq_pin",
            True,
        ), phase
    assert methods["distill"].call_args.kwargs["user_turn"] == "the prompt"
    assert capture_queue.hook_counts(queue_root(env)) == {
        "queue": 0,
        "running": 0,
        "dead": 0,
    }
    assert list((queue_root(env) / "payloads").iterdir()) == []
    http_hop.assert_not_called()


STRICT_ROWS = [
    ("observe", ("Bash", {}, "done")),
    ("distill", ("A response.",)),
    ("learn", ("Bash", "error")),
]


@pytest.mark.parametrize("phase,args", STRICT_ROWS)
def test_strict_reraises_store_failure(phase, args, monkeypatch, caplog):
    """strict (the queue path) re-raises so the job retries; the direct path still only warns."""
    monkeypatch.setattr(storage, "ingest", Mock(side_effect=RuntimeError("disk full")))
    lc = MemoryLifecycle("test_hwq_strict")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="disk full"):
            getattr(lc, phase)(*args, strict=True)
    assert "will retry" in caplog.text


# ── Probe 2: order per session across interleaved sessions ───────────────────────────


def test_order_per_session_across_interleaved_sessions(env, recorder):
    for n in range(6):
        for session in ("test_hwq_a", "test_hwq_b", "test_hwq_c"):
            payload = {
                "session_id": session,
                "tool_name": "Bash",
                "tool_input": {"n": f"{session}:{n}"},
            }
            assert run_hook("observe", payload, env).returncode == 0
    assert drain(env, parallel=3) == 18
    for session in ("test_hwq_a", "test_hwq_b", "test_hwq_c"):
        seen = [c["n"] for c in recorder.calls if c["session"] == session]
        assert seen == [f"{session}:{n}" for n in range(6)]
    assert recorder.overlap == []  # never two jobs of one session at once


# ── Probe 3: failure requeues, bounded retry, dead-letter after N, logged ─────────────


def test_failure_requeues_then_dead_letters(env, recorder, caplog):
    jobs = {"test_hwq_flaky": "flaky", "test_hwq_down": "down"}
    for session, marker in jobs.items():
        payload = {
            "session_id": session,
            "tool_name": "Bash",
            "tool_input": {"n": marker},
        }
        assert run_hook("observe", payload, env).returncode == 0
    recorder.fail.update(flaky=1, down=99)
    with caplog.at_level(logging.WARNING):
        drain(env, max_attempts=2)
    assert [c["n"] for c in recorder.calls] == ["flaky"]  # retried once, then written
    root = queue_root(env)
    (dead,) = [json.loads(p.read_text()) for p in (root / "dead").glob("*.json")]
    assert (dead["session_id"], dead["attempts"]) == ("test_hwq_down", 2)
    assert "store down for down" in dead["last_error"]
    assert Path(dead["payload_path"]).exists()  # kept for a manual requeue
    assert "failed (attempt 1 of 2), requeued" in caplog.text
    assert "dead-lettered after 2 attempt(s)" in caplog.text
    assert capture_queue.hook_counts(root) == {"queue": 0, "running": 0, "dead": 1}


def test_bad_payload_dead_letters_at_once_and_orphans_are_recovered(
    env, recorder, caplog
):
    root = capture_queue.hook_queue_dir(queue_root(env))
    bad = root / "payloads" / "1-1.observe.json"
    bad.write_text("not json")
    (root / "queue" / "1-1.observe.json").write_text(
        json.dumps(
            {
                "v": 1,
                "phase": "observe",
                "session_id": "test_hwq_bad",
                "payload_path": str(bad),
            }
        )
    )
    orphan = {
        "session_id": "test_hwq_orphan",
        "tool_name": "Bash",
        "tool_input": {"n": "orphan"},
    }
    assert run_hook("observe", orphan, env).returncode == 0
    for path in (root / "queue").glob("*.json"):
        if path != root / "queue" / "1-1.observe.json":
            path.rename(root / "running" / path.name)  # a worker died mid-job
    with caplog.at_level(logging.WARNING):
        drain(env, max_attempts=5)
    assert [c["n"] for c in recorder.calls] == ["orphan"]
    (dead,) = [json.loads(p.read_text()) for p in (root / "dead").glob("*.json")]
    assert dead["attempts"] == 1 and "not JSON" in dead["last_error"]
    assert "worker exited while the job was running" in caplog.text


# ── Probe 4: 20 concurrent observe hooks, 0 dropped, all drained ─────────────────────


def test_twenty_concurrent_observes_none_dropped(env, recorder):
    procs = [
        subprocess.Popen(
            ["bash", str(HOOKS / "observe.sh")],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(20)
    ]
    for n, proc in enumerate(procs):
        payload = {
            "session_id": f"test_hwq_c{n % 4}",
            "tool_name": "Bash",
            "tool_input": {"n": str(n)},
        }
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
    assert [proc.wait(timeout=10) for proc in procs] == [0] * 20
    assert len(records(env)) == 20
    hooks_log = Path(env["SMARTMEMORY_DATA_DIR"]) / "hooks.log"
    assert "lost" not in (hooks_log.read_text() if hooks_log.exists() else "")
    assert drain(env) == 20
    assert sorted(int(c["n"]) for c in recorder.calls) == list(range(20))
    assert capture_queue.hook_counts(queue_root(env)) == {
        "queue": 0,
        "running": 0,
        "dead": 0,
    }


# ── Probe 5: remote mode goes through the same queue ─────────────────────────────────


class _FakeService(BaseHTTPRequestHandler):
    ingests: list[dict] = []
    fail_first = 0

    def log_message(self, *args):  # keep test output small
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"default_team_id": "test_hwq_team"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self._reply(503, {"detail": "test_hwq unavailable"})
            return
        type(self).ingests.append({"path": self.path, "body": body})
        self._reply(200, {"item_id": "test_hwq_item"})


def test_remote_mode_goes_through_the_queue(env, monkeypatch):
    from smartmemory_app.remote_backend import RemoteMemory

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeService)
    _FakeService.ingests, _FakeService.fail_first = [], 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test_hwq")
        remote = RemoteMemory(
            api_url=f"http://127.0.0.1:{server.server_port}", team_id="test_hwq_team"
        )
        monkeypatch.setattr(storage, "get_memory", lambda *a, **k: remote)
        monkeypatch.setattr(
            "smartmemory_app.config.load_config", lambda: SimpleNamespace(mode="remote")
        )
        daemon = Mock()
        monkeypatch.setattr(cli, "_lifecycle_via_daemon", daemon)
        monkeypatch.setattr(
            "smartmemory_app.lifecycle_api._load_lifecycle_config", lambda: {}
        )
        hook_consumer.set_remote_drain(True, queue_root(env))
        payload = {
            "session_id": "test_hwq_remote",
            "cwd": "/w",
            "tool_name": "Bash",
            "tool_input": {"n": "r"},
        }
        assert (
            run_hook(
                "observe", payload, env, SMARTMEMORY_WORKSPACE_ID="test_hwq_pin"
            ).returncode
            == 0
        )
        assert _FakeService.ingests == []  # the hook itself wrote nothing
        stub_calls = Path(env["HOME"]).parent / "stub-calls"
        deadline = time.monotonic() + 5
        while (
            not stub_calls.exists()
        ):  # remote mode: the hook starts the remote drainer
            assert time.monotonic() < deadline, "hook did not start the remote drainer"
            time.sleep(0.01)
        assert (
            stub_calls.read_text() == f"lifecycle drain-hooks --dir {queue_root(env)}\n"
        )
        hook_consumer.run_remote_drainer(
            queue_root(env), max_attempts=3, idle_exit=0.0, poll=0.01
        )
    finally:
        server.shutdown()
        server.server_close()
    daemon.assert_not_called()  # remote mode never uses the local daemon
    (ingest,) = _FakeService.ingests  # the 503 was retried, then written once
    assert ingest["path"] == "/memory/ingest"
    assert ingest["body"]["context"]["origin"] == "hook:observe"
    assert ingest["body"]["context"]["workspace_id"] == "test_hwq_pin"
    assert capture_queue.hook_counts(queue_root(env)) == {
        "queue": 0,
        "running": 0,
        "dead": 0,
    }


# ── Probe 5b: the drainer exits while the hook reads stdin (REMOTE-EXIT-FU-1) ───────

# A real consume() loop stands in for the drainer. It ends when its stdin closes (the stop
# path clears worker.pid exactly as the idle exit does) and records the jobs it ran. The two
# trailing argv words make `ps` name it as the remote drainer, as the hooks check.
DRAINER = (
    "import sys, threading; from smartmemory_app.hook_consumer import consume; "
    "stop = threading.Event(); "
    "threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start(); "
    "consume(sys.argv[1], stop=stop, poll=0.01, "
    "execute=lambda job: open(sys.argv[2], 'a').write(job['phase'] + '\\n'))"
)

# phase, remote marker, drainer exits during stdin, replacement drainer started, jobs the old drainer ran
DRAINER_EXIT_ROWS = [
    ("observe", True, True, True, ()),
    ("learn", True, True, True, ()),
    ("distill", True, True, True, ()),
    ("observe", True, False, False, ("observe",)),  # still alive at publication: it takes the job
    ("observe", False, True, False, ()),  # local mode: the daemon is the one consumer, never a spawn
]


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


@pytest.mark.parametrize("phase,remote,exits,spawned,ran", DRAINER_EXIT_ROWS)
def test_drainer_exit_during_stdin_starts_a_replacement(
    phase, remote, exits, spawned, ran, env, tmp_path
):
    root = capture_queue.hook_queue_dir(queue_root(env))
    if remote:
        hook_consumer.set_remote_drain(True, root)
    ran_file = tmp_path / "drainer-ran"
    stub_calls = tmp_path / "stub-calls"
    app_root = str(HOOKS.parents[1])
    drainer_env = {
        **env,
        "PYTHONPATH": os.pathsep.join(filter(None, (app_root, env.get("PYTHONPATH")))),
    }
    drainer = subprocess.Popen(
        [sys.executable, "-c", DRAINER, str(root), str(ran_file), "lifecycle", "drain-hooks"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=drainer_env,
    )
    hook = None
    try:
        _wait_for(
            lambda: (root / "worker.pid").is_file()
            and (root / "worker.pid").read_text().strip() == str(drainer.pid),
            "the drainer never wrote worker.pid",
        )
        hook = subprocess.Popen(
            ["bash", str(HOOKS / f"{phase}.sh")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # The payload file exists only after the hook's worker.pid check: it now waits on stdin.
        _wait_for(lambda: any((root / "payloads").glob("*.json")), "the hook never reached stdin")
        assert drainer.poll() is None, "the drainer must be alive at the hook's first pid check"
        if exits:
            _, err = drainer.communicate(b"", timeout=10)  # stdin EOF: the drainer exits
            assert drainer.returncode == 0, err
            assert not (root / "worker.pid").exists()
        _, err = hook.communicate(
            json.dumps({"session_id": "test_hwq_remote_exit", "tool_name": "Bash"}), timeout=10
        )
        assert hook.returncode == 0, err
        if spawned:
            _wait_for(stub_calls.exists, "the hook did not start a replacement drainer")
            assert stub_calls.read_text() == f"lifecycle drain-hooks --dir {root}\n"
            assert capture_queue.hook_counts(root) == {"queue": 1, "running": 0, "dead": 0}
        if not exits:
            _wait_for(lambda: ran_file.exists(), "the live drainer never ran the published job")
            _, err = drainer.communicate(b"", timeout=10)  # stdin EOF: the drainer exits
            assert drainer.returncode == 0, err
            assert capture_queue.hook_counts(root) == {"queue": 0, "running": 0, "dead": 0}
        time.sleep(0.2)  # a background spawn the hook should not have made has landed by now
        assert stub_calls.exists() is spawned
        assert tuple(ran_file.read_text().splitlines() if ran_file.exists() else ()) == ran
    finally:
        for process in (hook, drainer):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


# ── Probe 6: hook wall time under 50 ms per phase (median of 20) ─────────────────────


@pytest.mark.parametrize("phase", WRITE_PHASES)
def test_hook_wall_time_under_50ms(phase, env):
    capture_queue.hook_queue_dir(queue_root(env))  # steady state: the dirs exist
    payload = json.dumps(
        {
            "session_id": "test_hwq_time",
            "tool_name": "Bash",
            "tool_response": "x" * 4000,
        }
    )
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        subprocess.run(
            ["bash", str(HOOKS / f"{phase}.sh")],
            input=payload,
            text=True,
            env=env,
            check=True,
        )
        samples.append((time.perf_counter() - start) * 1000)
    assert statistics.median(samples) < 50, samples


# ── Probe 7: no drop branch; one shared body ─────────────────────────────────────────


def test_no_drop_branch_and_one_shared_body():
    bodies = {}
    for phase in WRITE_PHASES:
        text = (HOOKS / f"{phase}.sh").read_text()
        assert not re.search(r"skipped|drop", text), phase
        lines = text.splitlines()
        assert lines[4] == f"PHASE={phase}"
        bodies[phase] = lines[:3] + lines[5:]
    assert bodies["observe"] == bodies["learn"] == bodies["distill"]


def test_drain_hooks_command_refuses_in_local_mode(env, recorder, monkeypatch, caplog):
    """Local mode: the daemon is the one consumer, so drain-hooks writes nothing and says why."""
    from click.testing import CliRunner

    monkeypatch.setattr(
        "smartmemory_app.config.load_config", lambda: SimpleNamespace(mode="local")
    )
    payload = {
        "session_id": "test_hwq_cli",
        "tool_name": "Bash",
        "tool_input": {"n": "cli"},
    }
    assert run_hook("learn", payload, env).returncode == 0
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(
            cli.cli, ["lifecycle", "drain-hooks", "--dir", str(queue_root(env))]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"queue": 1, "running": 0, "dead": 0}
    assert recorder.calls == []
    assert "remote mode only" in caplog.text
