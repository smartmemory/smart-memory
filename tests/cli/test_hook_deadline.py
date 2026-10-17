"""Hook deadline for `lifecycle recall` / `lifecycle orient` (HOOK-DEADLINE hotfix).

A beta tester on Windows saw UserPromptSubmit killed at 30 s: every prompt
cold-starts the engine in a fresh process and nothing inside the CLI bounded it.
These tests pin the internal deadline with a deliberately slow fake recall; no
models, no network (the capture transport is an httpx MockTransport or a stub).
"""

import inspect
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, crash_reporter
from smartmemory_app.lifecycle import MemoryLifecycle

SESSION = "test_hook_deadline_session"
PROMPT = "test_hook_deadline private prompt text"
WRAPPER_ROOT = Path(__file__).resolve().parents[2]


class _HardExit(SystemExit):
    """Stands in for os._exit inside CliRunner so the test process survives."""


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    monkeypatch.delenv("SMARTMEMORY_HOOK_DEADLINE", raising=False)
    monkeypatch.setattr(cli_module, "_lifecycle_via_daemon", lambda *a, **k: None)
    exits = []

    def hard_exit(code):
        exits.append(code)
        raise _HardExit(code)

    monkeypatch.setattr(cli_module, "_hard_exit", hard_exit)
    return {"tmp": tmp_path, "exits": exits}


@pytest.fixture
def release():
    """Unblock any fake recall still sleeping in its worker thread at teardown."""
    event = threading.Event()
    yield event
    event.set()


def invoke(phase, body):
    # click < 8.2 mixes stderr into result.stdout unless mix_stderr=False.
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        runner = CliRunner(mix_stderr=False)
    else:
        runner = CliRunner()
    return runner.invoke(cli_module.cli, ["lifecycle", phase], input=json.dumps(body))


def body(tmp):
    return {"session_id": SESSION, "cwd": str(tmp), "prompt": PROMPT}


def state_file(tmp):
    return tmp / "data" / "sessions" / f"{SESSION}.json"


# ── deadline parsing ──────────────────────────────────────────────────────────


def test_deadline_defaults_to_eight_seconds(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_HOOK_DEADLINE", raising=False)
    assert cli_module._hook_deadline() == 8.0


def test_deadline_env_override(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", "2.5")
    assert cli_module._hook_deadline() == 2.5


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_invalid_deadline_warns_and_uses_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", raw)
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.cli"):
        assert cli_module._hook_deadline() == 8.0
    assert "SMARTMEMORY_HOOK_DEADLINE" in caplog.text


# ── normal path ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["recall", "orient"])
def test_within_deadline_output_passes_through(isolated, monkeypatch, phase):
    monkeypatch.setattr(
        MemoryLifecycle, phase, lambda self, *a, **k: f"test_hook_deadline_{phase}"
    )
    result = invoke(phase, body(isolated["tmp"]))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"test_hook_deadline_{phase}\n"
    assert isolated["exits"] == []


def test_daemon_answer_passes_through(isolated, monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "_lifecycle_via_daemon",
        lambda path, received: {"context": "test_hook_deadline_daemon"},
    )
    monkeypatch.setattr(
        MemoryLifecycle,
        "recall",
        lambda *a, **k: pytest.fail("engine ran after a daemon answer"),
    )
    result = invoke("recall", body(isolated["tmp"]))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "test_hook_deadline_daemon\n"


def test_worker_exception_still_reaches_cli_error_handling(isolated, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError("test_hook_deadline boom")

    monkeypatch.setattr(MemoryLifecycle, "orient", broken)
    result = invoke("orient", body(isolated["tmp"]))
    assert result.exit_code != 0
    assert result.stdout == ""
    assert isolated["exits"] == []


# ── deadline exceeded ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["recall", "orient"])
def test_deadline_exceeded_is_silent_logged_reported_and_prompt(
    isolated, monkeypatch, caplog, release, phase
):
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", "0.5")
    events = []
    monkeypatch.setattr(
        crash_reporter,
        "report_event",
        lambda event, properties, **kw: events.append((event, properties, kw)),
    )

    def slow(self, *a, **k):
        release.wait(30)
        return "test_hook_deadline late context"

    monkeypatch.setattr(MemoryLifecycle, phase, slow)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.cli"):
        result = invoke(phase, body(isolated["tmp"]))
    elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert result.stdout == ""
    assert isolated["exits"] == [0]
    assert elapsed < 0.5 + 1.0, elapsed
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("hook deadline" in r.getMessage() for r in warnings), caplog.text
    message = next(
        r.getMessage() for r in warnings if "hook deadline" in r.getMessage()
    )
    assert f"lifecycle {phase}" in message
    assert "engine" in message  # the phase that overran is named
    assert PROMPT not in caplog.text

    assert len(events) == 1
    event, properties, kw = events[0]
    assert event == "hook_deadline_exceeded"
    assert properties["command"] == f"lifecycle {phase}"
    assert properties["phase"] == "engine"
    assert properties["deadline_s"] == 0.5
    assert properties["elapsed_s"] >= 0.5
    assert isinstance(properties["phase_timings"], dict)
    serialized = json.dumps(properties)
    assert PROMPT not in serialized
    assert str(isolated["tmp"]) not in serialized


def test_recall_persists_prompt_before_slow_recall(isolated, monkeypatch, release):
    """Distill pairs the Stop response with this prompt, even when recall overruns."""
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", "0.5")
    monkeypatch.setattr(crash_reporter, "report_event", lambda *a, **k: None)
    seen = {}

    def slow(self, prompt, cwd=None):
        seen["state"] = json.loads(state_file(isolated["tmp"]).read_text())
        release.wait(30)
        return ""

    monkeypatch.setattr(MemoryLifecycle, "recall", slow)
    result = invoke("recall", body(isolated["tmp"]))
    assert result.exit_code == 0
    assert seen["state"]["current_user_turn"] == PROMPT
    on_disk = json.loads(state_file(isolated["tmp"]).read_text())
    assert on_disk["current_user_turn"] == PROMPT


def test_daemon_phase_is_named_when_the_daemon_hangs(
    isolated, monkeypatch, caplog, release
):
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", "0.4")
    events = []
    monkeypatch.setattr(
        crash_reporter,
        "report_event",
        lambda event, properties, **kw: events.append(properties),
    )
    monkeypatch.setattr(
        cli_module, "_lifecycle_via_daemon", lambda *a, **k: release.wait(30)
    )
    result = invoke("recall", body(isolated["tmp"]))
    assert result.exit_code == 0
    assert result.stdout == ""
    assert events[0]["phase"] == "daemon"


def test_deadline_event_reaches_transport(isolated, monkeypatch, release):
    """The real report path enqueues and flushes before the hard exit."""
    monkeypatch.setenv("SMARTMEMORY_HOOK_DEADLINE", "0.4")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_HOST", "https://capture.test")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_KEY", "phc_test_public")
    rows = []
    original = httpx.Client

    def handler(request):
        rows.append(json.loads(request.content))
        return httpx.Response(200, request=request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(MemoryLifecycle, "recall", lambda *a, **k: release.wait(30))
    result = invoke("recall", body(isolated["tmp"]))
    assert result.exit_code == 0
    assert result.stdout == ""
    assert [row["event"] for row in rows] == ["hook_deadline_exceeded"]
    props = rows[0]["properties"]
    assert props["command"] == "lifecycle recall"
    assert props["phase"] == "engine"
    assert "log_tail_cli" not in props and "log_tail_daemon" not in props
    assert PROMPT not in json.dumps(rows)


# ── real process: os._exit honours the deadline ──────────────────────────────

_SLOW_HOOK = textwrap.dedent(
    """
    import json, sys, time
    from smartmemory_app import cli, crash_reporter
    from smartmemory_app.lifecycle import MemoryLifecycle

    sent_path = sys.argv[1]

    def post(payload):
        with open(sent_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\\n")
        return True

    crash_reporter._post = post
    cli._lifecycle_via_daemon = lambda *a, **k: None
    MemoryLifecycle.recall = lambda self, *a, **k: time.sleep(60) or "late"
    sys.stderr.write("T0=%r\\n" % time.time())
    sys.stderr.flush()
    cli.cli(["lifecycle", "recall"], prog_name="smartmemory")
    """
)


def test_real_process_exits_zero_within_deadline_plus_one(tmp_path):
    deadline = 1.0
    sent = tmp_path / "sent.jsonl"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SMARTMEMORY_")
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
            "SMARTMEMORY_HOOK_TRACE": str(tmp_path / "trace.jsonl"),
            "SMARTMEMORY_MODE": "local",
            "SMARTMEMORY_HOOK_DEADLINE": str(deadline),
            "SMARTMEMORY_CRASH_REPORTS": "1",
            "SMARTMEMORY_CRASH_REPORT_KEY": "phc_test_public",
            "SMARTMEMORY_CRASH_REPORT_HOST": "https://capture.invalid",
            "SMARTMEMORY_UPDATE_CHECK": "0",
            "PYTHONPATH": str(WRAPPER_ROOT),
            "OPENAI_API_KEY": "",
            "GROQ_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        }
    )
    (tmp_path / "home").mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _SLOW_HOOK, str(sent)],
        input=json.dumps(body(tmp_path)),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    ended = time.time()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    t0 = float(
        next(line for line in proc.stderr.splitlines() if line.startswith("T0="))[3:]
    )
    assert ended - t0 < deadline + 1.0, (ended - t0, proc.stderr)
    assert "hook deadline" in proc.stderr
    assert PROMPT not in proc.stderr
    rows = [json.loads(line) for line in sent.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["hook_deadline_exceeded"]
    assert PROMPT not in sent.read_text()


# ── daemon probe ──────────────────────────────────────────────────────────────


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    calls = []
    original = httpx.Client

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"context": "test_probe_ctx"}, request=request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    timeouts = []
    original_post = original.post

    def post(self, *args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return original_post(self, *args, **kwargs)

    monkeypatch.setattr(original, "post", post)
    return {"tmp": tmp_path, "calls": calls, "timeouts": timeouts}


def test_probe_skipped_without_pid_file(probe):
    assert cli_module._lifecycle_via_daemon("/lifecycle/recall", {}) is None
    assert probe["calls"] == []


def test_probe_skipped_when_pid_is_dead(probe, monkeypatch):
    (probe["tmp"] / "daemon.pid").write_text("424242")
    monkeypatch.setattr("smartmemory_app.daemon._pid_alive", lambda pid: False)
    assert cli_module._lifecycle_via_daemon("/lifecycle/recall", {}) is None
    assert probe["calls"] == []


def test_probe_skipped_on_garbage_pid_file(probe, caplog):
    (probe["tmp"] / "daemon.pid").write_text("not-a-pid")
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.cli"):
        assert cli_module._lifecycle_via_daemon("/lifecycle/recall", {}) is None
    assert probe["calls"] == []
    assert "daemon.pid" in caplog.text


def test_probe_uses_live_daemon_with_short_connect_timeout(probe):
    (probe["tmp"] / "daemon.pid").write_text(str(os.getpid()))
    out = cli_module._lifecycle_via_daemon("/lifecycle/recall", {"a": 1})
    assert out == {"context": "test_probe_ctx"}
    assert len(probe["calls"]) == 1
    timeout = probe["timeouts"][0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 1.5
