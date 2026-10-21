"""LOCAL-DAEMON-1: failure classes get a one-line fix, stale markers are cleaned by exact pid."""

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import click
import pytest

from smartmemory_app import daemon, daemon_diagnosis as dd
from smartmemory_app import setup as setup_mod

LOG = Path("/data/daemon.log")
CFG = Path("/cfg/config.toml")


def facts(**kw):
    return dd.DaemonFacts(port=9014, log_path=LOG, config_path=CFG, **kw)


# One row per failure class. `bug` names the demo or repro that needed the row.
ROWS = [
    (
        "happy",
        dict(health={"status": "ok"}),
        "ok",
        True,
        "daemon running at http://127.0.0.1:9014",
        None,
    ),
    (
        "warming",
        dict(health={"status": "warming"}),
        "warming",
        False,
        "still warming",
        "sm status",
    ),
    (
        "missing_spacy",
        dict(
            health={
                "status": "degraded",
                "degraded_reason": "MissingModelError: spaCy model 'en_core_web_md' is required.",
            }
        ),
        "missing_models",
        False,
        "model files are missing",
        "spacy download en_core_web_md",
    ),
    (
        "missing_embedding",
        dict(
            health={
                "status": "degraded",
                "degraded_reason": "MissingModelError: Embedding model 'x' is unavailable.",
            }
        ),
        "missing_models",
        False,
        "model files are missing",
        "sm warm",
    ),
    (
        "missing_both",
        dict(
            health={
                "status": "degraded",
                "degraded_reason": "MissingModelError: spaCy model 'a' is required.; MissingModelError: Embedding model 'b' is unavailable.",
            }
        ),
        "missing_models",
        False,
        "model files are missing",
        "sm warm",
    ),
    (
        "degraded_other",
        dict(
            health={"status": "degraded", "degraded_reason": "StoreBusyError: locked"}
        ),
        "degraded",
        False,
        "StoreBusyError",
        "sm restart",
    ),
    (
        "degraded_no_reason",
        dict(health={"status": "degraded"}),
        "degraded",
        False,
        "no reason given",
        "sm restart",
    ),
    (
        "port_in_use",
        dict(port_taken=True),
        "port_in_use",
        False,
        "port 9014 is used by another program",
        "daemon_port",
    ),
    (
        "port_in_use_pid",
        dict(port_taken=True, port_holder_pid=4242),
        "port_in_use",
        False,
        "(process 4242)",
        str(CFG),
    ),
    (
        "wrong_python",
        dict(plist_python="/gone/venv/bin/python", plist_python_ok=False),
        "wrong_python",
        False,
        "/gone/venv/bin/python, which no longer exists",
        "sm setup",
    ),
    (
        "garbled_marker",
        dict(marker_garbled=True),
        "stale_marker",
        False,
        "unreadable",
        "sm start",
    ),
    (
        "crashed_dead_pid",
        dict(marker_pid=777, marker_alive=False, log_error="ERROR boom"),
        "crashed",
        False,
        "process 777 is gone",
        "sm start",
    ),
    (
        "crashed_log_tail",
        dict(marker_pid=777, marker_alive=False, log_error="ERROR boom"),
        "crashed",
        False,
        "Last log error: ERROR boom",
        str(LOG),
    ),
    (
        "pid_reused",
        dict(marker_pid=777, marker_alive=True, marker_is_daemon=False),
        "stale_marker",
        False,
        "not SmartMemory",
        "sm start",
    ),
    (
        "hung_daemon",
        dict(marker_pid=777, marker_alive=True, marker_is_daemon=True),
        "unresponsive",
        False,
        "not answering on port 9014",
        "sm restart",
    ),
    (
        "hung_unknown_cmdline",
        dict(marker_pid=777, marker_alive=True, marker_is_daemon=None),
        "unresponsive",
        False,
        "process 777",
        "sm restart",
    ),
    ("never_started", dict(), "not_running", False, "not running", "sm start"),
    (
        "never_started_log_error",
        dict(log_error="Traceback (most recent call last):"),
        "not_running",
        False,
        "Last log error: Traceback",
        "sm start",
    ),
    # Order: a live answer beats every marker, a foreign listener beats a stale marker.
    (
        "health_beats_marker",
        dict(health={"status": "ok"}, marker_pid=1, marker_alive=False),
        "ok",
        True,
        "running",
        None,
    ),
    (
        "port_beats_marker",
        dict(port_taken=True, marker_pid=1, marker_alive=False),
        "port_in_use",
        False,
        "another program",
        "daemon_port",
    ),
]


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_every_failure_class_has_a_one_line_fix(row):
    _, kw, klass, ok, detail, fix = row
    got = dd.diagnose(facts(**kw))
    assert (got.klass, got.ok) == (klass, ok)
    assert detail in got.detail
    if fix is None:
        assert got.fix is None
    else:
        assert fix in got.fix
    for text in (got.detail, got.fix or ""):
        assert "\n" not in text
        assert "—" not in text and ";" not in text, text
    assert ok or got.fix, "a failing row must say how to fix it"


def test_daemon_health_signature_and_never_raises(monkeypatch):
    monkeypatch.setattr(
        dd, "collect_facts", lambda status=None: facts(health={"status": "ok"})
    )
    assert daemon.daemon_health() == (
        True,
        "daemon running at http://127.0.0.1:9014",
        None,
    )
    monkeypatch.setattr(dd, "collect_facts", lambda status=None: facts())
    ok, detail, fix = daemon.daemon_health()
    assert (ok, fix) == (False, "Run: sm start") and "not running" in detail

    def boom(status=None):
        raise OSError("probe exploded")

    monkeypatch.setattr(dd, "collect_facts", boom)
    ok, detail, fix = daemon.daemon_health()
    assert ok is False and "probe exploded" in detail and fix


# ── stale marker cleanup: exact pid check only ────────────────────────────────


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(path))
    monkeypatch.setattr(daemon, "is_running", lambda require_healthy=True: False)
    return path


def test_clean_stale_markers_rows(data_dir):
    dead, live = _dead_pid(), os.getpid()
    rows = [
        # (name, marker content, stop-request pid, marker kept, stop request kept)
        ("dead_pid", str(dead), dead, False, False),
        ("garbage", "not-a-pid", dead, False, False),
        ("zero", "0", None, False, True),
        ("live_pid", str(live), live, True, True),
        ("empty", "", None, False, True),
    ]
    for name, content, stop_pid, marker_kept, stop_kept in rows:
        marker = data_dir / "daemon.pid"
        marker.write_text(content)
        request = data_dir / f".daemon.stop-{stop_pid}" if stop_pid else None
        if request:
            request.write_text("stop")
        removed = daemon.clean_stale_markers()
        assert marker.exists() is marker_kept, name
        if request:
            assert request.exists() is stop_kept, name
            request.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        assert ("daemon.pid" in removed) is (not marker_kept), name


def test_clean_stale_markers_never_touches_a_healthy_daemon(data_dir, monkeypatch):
    monkeypatch.setattr(daemon, "is_running", lambda require_healthy=True: True)
    dead = _dead_pid()
    (data_dir / "daemon.pid").write_text(str(dead))
    assert daemon.clean_stale_markers() == []
    assert (data_dir / "daemon.pid").exists()


def test_clean_stale_markers_ignores_lock_files_and_odd_names(data_dir):
    keep = [
        data_dir / ".write.lock",
        data_dir / ".worker.lock",
        data_dir / ".daemon.stop-abc",
    ]
    for path in keep:
        path.write_text("")
    assert daemon.clean_stale_markers() == []
    assert all(path.exists() for path in keep)


def test_start_daemon_cleans_dead_marker_before_launch(data_dir, monkeypatch):
    marker = data_dir / "daemon.pid"
    marker.write_text(str(_dead_pid()))
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda **kw: False)
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: False)

    class Stop(Exception):
        pass

    def launch(*args, **kwargs):
        assert not marker.exists(), "marker must be gone before the new daemon launches"
        raise Stop

    monkeypatch.setattr(daemon, "spawn_detached", launch)
    with pytest.raises(Stop):
        daemon.start_daemon()


# ── real sockets and processes ────────────────────────────────────────────────


@pytest.fixture
def foreign_listener():
    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"hello": "other app"}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Other)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()
    server.server_close()


def test_collect_facts_sees_a_foreign_listener(foreign_listener, monkeypatch):
    monkeypatch.setattr(daemon, "_port", lambda: foreign_listener)
    got = dd.diagnose(dd.collect_facts())
    assert got.klass == "port_in_use"
    if shutil.which("lsof"):
        assert f"process {os.getpid()}" in got.detail


def test_collect_facts_free_port_and_dead_marker(data_dir, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    monkeypatch.setattr(daemon, "_port", lambda: free)
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    assert dd.diagnose(dd.collect_facts()).klass == "not_running"
    dead = _dead_pid()
    (data_dir / "daemon.pid").write_text(str(dead))
    (data_dir / "daemon.log").write_text("ok\nERROR: model load failed\n")
    got = dd.diagnose(dd.collect_facts())
    assert got.klass == "crashed" and f"process {dead} is gone" in got.detail
    assert "model load failed" in got.detail
    (data_dir / "daemon.pid").write_text("junk")
    assert dd.diagnose(dd.collect_facts()).klass == "stale_marker"
    (data_dir / "daemon.pid").write_text(
        str(os.getpid())
    )  # live, but pytest is not the daemon
    assert dd.diagnose(dd.collect_facts()).klass == "stale_marker"


# ── setup surface ─────────────────────────────────────────────────────────────


def _setup_step(monkeypatch, *, start, probe=None):
    monkeypatch.setattr(setup_mod, "_install_launchd_plist", lambda: False)
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda **kw: False)
    monkeypatch.setattr(daemon, "start_daemon", start)
    monkeypatch.setattr(daemon, "_port", lambda: 9014)
    monkeypatch.setattr(
        dd, "collect_facts", probe or (lambda status=None: facts(health=status))
    )


def test_setup_prints_the_address_when_healthy(monkeypatch, capsys):
    _setup_step(monkeypatch, start=lambda **kw: {"status": "ok"})
    assert setup_mod._start_daemon_local() == {"status": "ok"}
    assert "daemon running at http://127.0.0.1:9014" in capsys.readouterr().out


SETUP_FAILURES = [
    (
        "port_in_use",
        RuntimeError("SmartMemory could not start because port 9014 is in use."),
        None,
        facts(port_taken=True),
        "port 9014 is used by another program",
        "daemon_port",
    ),
    (
        "never_started",
        RuntimeError("SmartMemory did not respond within 60 seconds."),
        None,
        facts(),
        "daemon is not running",
        "sm start",
    ),
    (
        "crashed",
        RuntimeError("SmartMemory stopped during startup (code 1)."),
        None,
        facts(marker_pid=5, marker_alive=False),
        "process 5 is gone",
        "sm start",
    ),
    (
        "missing_models",
        None,
        {
            "status": "degraded",
            "degraded_reason": "MissingModelError: spaCy model 'en_core_web_sm' is required.",
        },
        None,
        "model files are missing",
        "spacy download en_core_web_sm",
    ),
    (
        "wrong_python",
        RuntimeError("did not respond"),
        None,
        facts(plist_python="/x/python", plist_python_ok=False),
        "/x/python",
        "sm setup",
    ),
]


@pytest.mark.parametrize("row", SETUP_FAILURES, ids=[r[0] for r in SETUP_FAILURES])
def test_setup_fails_with_problem_fix_and_raw_detail(monkeypatch, capsys, row):
    _, exc, status, observed, problem, fix = row

    def start(**kw):
        if exc is not None:
            raise exc
        return status

    probe = None if observed is None else (lambda status=None: observed)
    _setup_step(monkeypatch, start=start, probe=probe)
    with pytest.raises(click.ClickException) as caught:
        setup_mod._start_daemon_local()
    message = caught.value.format_message()
    assert problem in message
    assert any(
        line.startswith("Fix: ") and fix in line for line in message.splitlines()
    )
    assert "Detail: " in message and "Log: " in message
    assert "running at" not in capsys.readouterr().out, (
        "never claim success on a failed health check"
    )


def test_setup_keeps_raw_reason_when_diagnosis_breaks_or_contradicts(monkeypatch):
    def boom(status=None):
        raise OSError("probe exploded")

    _setup_step(monkeypatch, start=lambda **kw: None, probe=boom)
    with pytest.raises(click.ClickException) as caught:
        setup_mod._start_daemon_local()
    message = caught.value.format_message()
    assert "No health response was received after startup." in message
    assert "Fix:" not in message

    _setup_step(
        monkeypatch,
        start=lambda **kw: None,
        probe=lambda status=None: facts(health={"status": "ok"}),
    )
    with pytest.raises(click.ClickException) as caught:
        setup_mod._start_daemon_local()
    assert "Fix:" not in caught.value.format_message()
