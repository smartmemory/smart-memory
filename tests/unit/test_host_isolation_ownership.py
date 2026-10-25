"""Host-isolation teardown signals only what this session started, and never hides a
registry failure (HOOK-WRITE-QUEUE-HOST-ISOLATION-LISTENER-OWNERSHIP-FU-1 and
HOOK-WRITE-QUEUE-HOST-ISOLATION-REGISTRY-FU-1).

Every listener here is a disposable child this test creates, on a port in the registry
test range (19000-19999). The registry tool is stubbed: no ~/.ports row is touched.
"""

import logging
import subprocess
import sys

import pytest

from tests import _host_isolation as hi

LISTENER = (
    "import socket,sys,time\n"
    "for port in range(19000, 20000):\n"
    "    s = socket.socket()\n"
    "    try:\n"
    "        s.bind(('127.0.0.1', port))\n"
    "    except OSError:\n"
    "        s.close()\n"
    "        continue\n"
    "    s.listen()\n"
    "    print(port, flush=True)\n"
    "    time.sleep(60)\n"
)

# case | registered | started by this session | listener survives teardown
LISTENER_ROWS = [
    ("unrelated-unregistered", False, False, True),
    ("unrelated-registered", True, False, True),
    ("owned-unregistered", False, True, False),
    ("owned-registered", True, True, False),
]


@pytest.mark.parametrize(
    "registered,tracked,survives",
    [r[1:] for r in LISTENER_ROWS],
    ids=[r[0] for r in LISTENER_ROWS],
)
def test_teardown_signals_only_listeners_this_session_started(
    host_isolation_guard, monkeypatch, registered, tracked, survives
):
    registry_calls: list[tuple] = []
    monkeypatch.setattr(
        hi, "_port_check", lambda *a: registry_calls.append(a) or 0
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(child.stdout.readline())
        assert 19000 <= port < 20000
        if not tracked:  # the Popen guard tracked it: model a process we did not start
            hi._SESSION_SPAWNED[:] = [p for p in hi._SESSION_SPAWNED if p is not child]
        assert hi.listener_pids(port) == [child.pid]

        problems = hi.release_test_port(port, registered)

        if survives:
            assert child.poll() is None, "an unrelated listener was signalled"
            assert any(
                f"pid {child.pid}" in p and "not signalled" in p for p in problems
            ), problems
        else:
            assert child.wait(timeout=5) is not None
            assert any(f"pid {child.pid} was still listening" in p for p in problems)
        released = [c for c in registry_calls if c[0] == "--release"]
        assert bool(released) == registered
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()


def _raise(exc):
    def run(*args, **kwargs):
        raise exc

    return run


# case | tool installed | error raised by the launch | expected outcome
REGISTRY_ROWS = [
    ("absent-tool-is-a-quiet-skip", False, None, "skip"),
    ("launch-failure", True, OSError("exec failed"), "fail"),
    ("timeout", True, subprocess.TimeoutExpired("port_check", 30), "fail"),
]


@pytest.mark.parametrize(
    "installed,error,outcome",
    [r[1:] for r in REGISTRY_ROWS],
    ids=[r[0] for r in REGISTRY_ROWS],
)
def test_installed_registry_tool_failure_is_not_tool_absent(
    host_isolation_guard, monkeypatch, tmp_path, caplog, installed, error, outcome
):
    tool = tmp_path / "port_check.py"
    if installed:
        tool.write_text("")
    monkeypatch.setattr(hi, "_PORT_CHECK", tool)
    if error is not None:
        monkeypatch.setattr(hi.subprocess, "run", _raise(error))
    with caplog.at_level(logging.WARNING, logger=hi.log.name):
        if outcome == "skip":
            port, registered = hi.reserve_test_port()
            assert registered is False and port > 0
            assert not caplog.records
            return
        with pytest.raises(RuntimeError, match="registry tool is installed but failed"):
            hi.reserve_test_port()
        assert any("was not reserved" in r.getMessage() for r in caplog.records)
        # release after a tool failure: WARNING plus a reported problem, no exception
        caplog.clear()
        problems = hi.release_test_port(12345, True, owned=set())
        assert any("not released" in p for p in problems)
        assert any("was not released" in r.getMessage() for r in caplog.records)
