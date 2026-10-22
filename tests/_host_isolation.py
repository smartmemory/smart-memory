"""Host isolation for the CLI test suite (CLI-TEST-HOST-ISOLATION-1).

A test must never reach the machine owner's real daemon: its port (9014), its HOME,
or its launchd job (``ai.smartmemory.daemon``). The autouse fixtures in conftest.py use
this module to guarantee it:

* a port reserved in ``~/.ports`` (``port_check.py --reserve cliTest``) and exported as
  ``SMARTMEMORY_DAEMON_PORT``, released on teardown, failure path included;
* a ``launchctl`` shim first on PATH, so no process the test starts (in-process or a
  spawned daemon) can bootstrap or bootout a real job. Every call is logged and fails the test;
* a ``socket.bind`` guard that fails the test that binds a real daemon port;
* every ``subprocess.Popen`` the test creates is tracked and stopped at teardown.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 9014 is the sm local daemon, 9015 its retired events port (~/.ports, smartMemory row).
REAL_DAEMON_PORTS = frozenset({9014, 9015})
PORT_APP = "cliTest"
_PORT_CHECK = Path.home() / ".claude" / "scripts" / "port_check.py"

TEST_LABEL_PREFIX = "ai.smartmemory.test."

SHIM_BODY = """#!/bin/sh
# CLI-TEST-HOST-ISOLATION-1: tests never reach the real launchd. Every call is logged;
# the guard fails the test unless the call names only a scratch (ai.smartmemory.test.*) label.
# The log path is baked in, so a caller that strips the environment is still recorded.
printf '%s\\n' "launchctl $*" >> '@LOG@'
case "$*" in
  "bootout gui/"*"/ai.smartmemory.test."*) exit 0 ;;
  "print gui/"*"/ai.smartmemory.test."*)
    # @LOADED@=1: this scratch job answers "loaded" (to see which paths go on to bootout).
    [ '@LOADED@' = 1 ] && exit 0 ;;
esac
echo "Could not find service: launchctl blocked by test host isolation" >&2
exit 113
"""


@dataclass
class HostGuard:
    """What the guard fixture hands a test that asks for it by name."""

    port: int
    launchctl_log: Path
    binds: list[str]
    shim: Path

    def forgive(self) -> None:
        """For the guard's own tests: they provoke a violation on purpose."""
        self.launchctl_log.write_text("")
        self.binds.clear()


class HostIsolationViolation(OSError):
    """A test tried to bind a real daemon port."""


def _port_check(*args: str) -> int | None:
    """Run the registry tool. None when it is absent (CI), else its exit code."""
    if not _PORT_CHECK.is_file():
        return None
    try:
        return subprocess.run(
            [sys.executable, str(_PORT_CHECK), *args],
            capture_output=True,
            timeout=30,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def reserve_test_port() -> tuple[int, bool]:
    """A free port, registered in ~/.ports under cliTest when the registry tool exists.

    Returns (port, registered). A taken port is never used: try again.
    """
    for _ in range(8):
        port = _free_port()
        if port in REAL_DAEMON_PORTS:
            continue
        code = _port_check(str(port), "--reserve", PORT_APP)
        if code is None:
            return port, False
        if code == 0:
            return port, True
    raise RuntimeError("no free test port could be reserved in ~/.ports")


def release_test_port(port: int, registered: bool) -> list[str]:
    """Release the registration and stop anything still listening. Returns problems."""
    problems: list[str] = []
    if registered and _port_check("--release", str(port), "--app", PORT_APP) != 0:
        problems.append(f"port {port} was not released from ~/.ports ({PORT_APP})")
    for pid in listener_pids(port):
        problems.append(f"pid {pid} was still listening on test port {port}")
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    return problems


def listener_pids(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def install_launchctl_shim(directory: Path, log: Path, loaded: bool = False) -> Path:
    """Write the shim (log path baked in) and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "launchctl"
    shim.write_text(
        SHIM_BODY.replace("@LOG@", str(log)).replace("@LOADED@", "1" if loaded else "0")
    )
    shim.chmod(0o755)
    return shim


def bind_guard(violations: list[str]):
    """A ``socket.socket.bind`` that refuses the real daemon ports and records the try."""
    real_bind = socket.socket.bind

    def guarded(self, address, *args, **kwargs):
        if isinstance(address, tuple) and len(address) >= 2:
            port = address[1]
            if isinstance(port, int) and port in REAL_DAEMON_PORTS:
                violations.append(f"bind {address[0]!r}:{port}")
                raise HostIsolationViolation(
                    errno.EACCES,
                    f"test host isolation: port {port} is the owner's real daemon port",
                )
        return real_bind(self, address, *args, **kwargs)

    return guarded


_LAUNCHCTL_NAMES = ("launchctl", "/bin/launchctl", "/usr/bin/launchctl")


def tracking_popen_init(spawned: list, shim: Path):
    """A ``Popen.__init__`` that records every process the test starts and sends a
    ``launchctl`` argv to the shim, even when the test replaced PATH."""
    real_init = subprocess.Popen.__init__

    def init(self, args=None, *rest, **kwargs):
        if (
            isinstance(args, (list, tuple))
            and args
            and str(args[0]) in _LAUNCHCTL_NAMES
        ):
            args = [str(shim), *args[1:]]
        real_init(self, args, *rest, **kwargs)
        spawned.append(self)

    return init


def stop_spawned(spawned: list) -> list[str]:
    """Stop every still-running process the test started. Returns what was left."""
    left: list[str] = []
    for proc in spawned:
        try:
            if proc.poll() is not None:
                continue
            left.append(f"pid {proc.pid}")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # a fake Popen or an already-reaped process
            continue
    return left


_SCRATCH_CALL = re.compile(
    r"^launchctl (print|bootout) gui/\d+/" + re.escape(TEST_LABEL_PREFIX) + r"\S+$"
)


def real_launchctl_calls(log: Path) -> list[str]:
    """Logged launchctl calls that could touch a real job: anything but print/bootout of a scratch label.

    load/unload/bootstrap take a plist path whose Label cannot be vouched for, so they count.
    """
    calls = log.read_text().splitlines() if log.exists() else []
    return [c for c in calls if not _SCRATCH_CALL.match(c)]
