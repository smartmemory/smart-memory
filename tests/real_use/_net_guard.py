"""Offline guard for a real-use run (OPS-REAL-USE-HOSTED-LITE-1).

install(log_path) patches socket connects so any connection to a non-loopback address is refused and written, one line each,
to log_path. The scenario reads that file for its "no network left the laptop" row; the refusal also stops a stray model or
telemetry call from reaching anyone.

    python -m tests.real_use._net_guard <log_path> <module> [args...]   # run a module (the Lite CLI) under the guard
"""

from __future__ import annotations

import ipaddress
import runpy
import socket
import sys

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", ""}


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host or "")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def install(log_path: str) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _note(address):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{address!r}\n")

    def _check(sock, address):
        if (
            sock.family in (socket.AF_INET, socket.AF_INET6)
            and isinstance(address, tuple)
            and not _is_local(address[0])
        ):
            _note(address)
            raise ConnectionRefusedError(
                f"real-use offline guard: no network from a Lite run ({address[0]})"
            )

    def connect(self, address):
        _check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(self, address)
        return real_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


if __name__ == "__main__":
    install(sys.argv[1])
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
