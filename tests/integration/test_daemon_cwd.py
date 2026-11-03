"""SM-DAEMON-CWD-1: the daemon never depends on its launch directory.

2026-10-30 incident: the daemon inherited a worktree as its cwd, the worktree was
deleted, and every core ``get_config()`` (``os.getcwd()``) raised from then on.
"""

import os
import shutil
import signal
import socket
import sys

import httpx
import pytest

from smartmemory_app import capture_queue, daemon


def _ephemeral_port() -> int:
    """A free port from the OS, so the test never collides with a fixed number."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


TEST_PORT = _ephemeral_port()


def _stub_start(monkeypatch, data):
    monkeypatch.setattr(daemon, "_data_dir", lambda: data)
    monkeypatch.setattr(daemon, "_port", lambda: TEST_PORT)
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda: False)
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: False)


class _Stop(Exception):
    pass


def _capture(monkeypatch, call, module=daemon):
    seen = {}

    def spawn(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        raise _Stop

    monkeypatch.setattr(module, "spawn_detached", spawn)
    with pytest.raises(_Stop):
        call()
    return seen


def test_spawn_argv_and_cwd_table(tmp_path, monkeypatch):
    """Both detached spawns: ``-P`` right after the interpreter and cwd == data dir."""
    data = tmp_path / "data"
    data.mkdir()
    _stub_start(monkeypatch, data)
    monkeypatch.setattr(
        "smartmemory.pipeline.work_graph.spawn.worker_is_running", lambda d: False
    )
    rows = [
        ("daemon", daemon.start_daemon),
        ("worker", daemon._start_workers),
    ]
    for name, call in rows:
        seen = _capture(monkeypatch, call)
        assert seen["argv"][:2] == [sys.executable, "-P"], name
        assert seen["cwd"] == str(data), name
    # The hook-queue capture worker spawn (capture_queue.spawn_successor) follows the same rule.
    captures = data / "captures"
    captures.mkdir()
    monkeypatch.setattr(capture_queue, "capture_dir", lambda: captures)
    seen = _capture(monkeypatch, capture_queue.spawn_successor, module=capture_queue)
    assert seen["argv"][:2] == [sys.executable, "-P"], "capture"
    assert seen["cwd"] == str(data), "capture"


def _free(port):
    s = socket.socket()
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def test_daemon_survives_deleted_launch_dir(tmp_path, monkeypatch):
    """Start from a temp dir, delete it, and the daemon still answers /memory/vector/stats."""
    assert _free(TEST_PORT), "reserved test port is in use"
    data = tmp_path / "data"
    data.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    for key in ("SMARTMEMORY_DATA_DIR", "SMARTMEMORY_DAEMON_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data))
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(TEST_PORT))
    monkeypatch.setenv("SMARTMEMORY_HOOK_SYNC", "0")
    start_cwd = os.getcwd()
    pid = None
    try:
        os.chdir(launch)
        status = daemon.start_daemon()
        pid = (status or {}).get("pid")
        os.chdir(start_cwd)
        shutil.rmtree(launch)
        r = httpx.get(
            f"http://127.0.0.1:{TEST_PORT}/memory/vector/stats",
            timeout=30,
            trust_env=False,
        )
        assert r.status_code == 200, r.text[:300]
    finally:
        os.chdir(start_cwd)
        if pid is None:
            try:
                pid = int((data / "daemon.pid").read_text().split()[0])
            except Exception:
                pid = None
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
