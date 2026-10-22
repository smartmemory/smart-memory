"""`sm clear` stops the daemon and its workers, clears in process, then restarts (CLI-CLEAR-DAEMON-1).

Before the fix `sm clear` asked the running daemon to clear its own store, and the
store reset refused the daemon's own ``daemon.pid`` and core worker as live owners
("Store reset refused: live owners: .worker.pid ..., daemon.pid ..."), so it failed
on every run while a daemon was up. The owners here are real subprocesses that write
their own pid marker (and hold ``.worker.lock``), and the real store reset runs
against a real data dir. Only process control is faked: `stop_daemon` kills the
owners it was told about, `_start_with_progress` records the restart.
"""

import os
import signal
import subprocess
import sys
import textwrap

import pytest
from click.testing import CliRunner

import smartmemory_app.cli as cli_mod
import smartmemory_app.daemon as daemon_mod

_OWNER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from filelock import FileLock
    marker, lock = Path(sys.argv[1]), sys.argv[2]
    held = FileLock(lock) if lock else None
    if held:
        held.acquire()
    marker.write_text(str(__import__("os").getpid()))
    print("ready", flush=True)
    time.sleep(120)
    """
)


class _Owners:
    """Real owner processes. `stop_all` is what a working `stop_daemon` does."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.procs: list[tuple[subprocess.Popen, str]] = []

    def spawn(self, marker: str, lock: bool = False) -> None:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OWNER,
                str(self.data_dir / marker),
                str(self.data_dir / ".worker.lock") if lock else "",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout.readline().strip() == "ready"
        self.procs.append((proc, marker))

    def stop_all(self, keep: tuple[str, ...] = ()) -> None:
        for proc, marker in list(self.procs):
            if marker in keep:
                continue
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
            (self.data_dir / marker).unlink(missing_ok=True)
            self.procs.remove((proc, marker))

    def cleanup(self) -> None:
        self.stop_all()
        for proc, _ in self.procs:
            proc.kill()
            proc.wait(timeout=10)


# Owners are (marker, holds .worker.lock). is_running / launchd are what the CLI sees before stopping.
# unstoppable: marker a failing stop leaves alive. stop_fails: stop_daemon raises. restart_fails: restart raises.
# Each row names the bug it pins.
ROWS = {
    # the tester's repro: daemon plus its own core worker, `sm clear --yes`
    "daemon_and_worker": dict(
        owners=[("daemon.pid", False), (".worker.pid", True)],
        running=True,
        launchd=False,
        restarted=True,
        exit=0,
        cleared=True,
    ),
    # a worker left behind with no daemon: still stopped, nothing to restart
    "stray_worker_only": dict(
        owners=[(".worker.pid", True)],
        running=False,
        launchd=False,
        restarted=False,
        exit=0,
        cleared=True,
    ),
    "nothing_running": dict(
        owners=[],
        running=False,
        launchd=False,
        restarted=False,
        exit=0,
        cleared=True,
    ),
    # launchd keeps the daemon down between restarts: it still has to come back
    "launchd_managed_daemon_down": dict(
        owners=[],
        running=False,
        launchd=True,
        restarted=True,
        exit=0,
        cleared=True,
    ),
    # an MCP client's worker the stop cannot reach: refuse, keep the data, still bring the daemon back
    "unstoppable_owner": dict(
        owners=[("daemon.pid", False), ("worker.mcp.pid", False)],
        running=True,
        launchd=False,
        unstoppable="worker.mcp.pid",
        restarted=True,
        exit=1,
        cleared=False,
        text="Store reset refused",
    ),
    # the clear already happened, only the restart failed: warn, exit 0
    "restart_fails": dict(
        owners=[("daemon.pid", False)],
        running=True,
        launchd=False,
        restart_fails=True,
        restarted=True,
        exit=0,
        cleared=True,
        text="did not restart",
    ),
    # stop failed: nothing touched, and no restart of what was never stopped
    "stop_fails": dict(
        owners=[("daemon.pid", False)],
        running=True,
        launchd=False,
        stop_fails=True,
        restarted=False,
        exit=1,
        cleared=False,
        text="did not stop",
    ),
}


@pytest.mark.parametrize("name", list(ROWS))
def test_clear_stops_then_restarts(name, tmp_path, monkeypatch):
    row = ROWS[name]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "memory.db").write_bytes(b"not empty")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")

    owners = _Owners(data_dir)
    calls = {"stop": 0, "restart": 0}
    try:
        for marker, lock in row["owners"]:
            owners.spawn(marker, lock)

        def fake_stop():
            calls["stop"] += 1
            if row.get("stop_fails"):
                raise RuntimeError("stop timed out")
            owners.stop_all(keep=(row["unstoppable"],) if "unstoppable" in row else ())

        def fake_restart(**kwargs):
            calls["restart"] += 1
            if row.get("restart_fails"):
                raise RuntimeError("spawn failed")

        monkeypatch.setattr(daemon_mod, "is_running", lambda *a, **k: row["running"])
        monkeypatch.setattr(
            daemon_mod, "_launchd_manages_daemon", lambda: row["launchd"]
        )
        monkeypatch.setattr(daemon_mod, "stop_daemon", fake_stop)
        monkeypatch.setattr(cli_mod, "_start_with_progress", fake_restart)

        result = CliRunner().invoke(cli_mod.cli, ["clear", "--yes"])
    finally:
        owners.cleanup()

    assert result.exit_code == row["exit"], result.output
    assert calls["stop"] == 1, (
        "sm clear must always stop first (a stray worker has no daemon)"
    )
    assert calls["restart"] == (1 if row["restarted"] else 0), result.output
    assert (not (data_dir / "memory.db").exists()) == row["cleared"], result.output
    if "text" in row:
        assert row["text"] in result.output
    if row["cleared"]:
        assert "Cleared" in result.output
    assert os.path.isdir(data_dir)
