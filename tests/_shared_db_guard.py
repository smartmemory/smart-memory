"""A test never connects to the shared local databases (TEST-HOLD-RECEIPT-OFF-9010-1).

Same shape and kill switch as CORE-TEST-SHARED-DB-GUARD-1 in smart-memory-core/service/common
(``.claude/rules/test-backends.md``): the dev box has one FalkorDB (9010), one cache Redis (9012) and one MongoDB
(9013) that other work shares, and a test that reaches them leaks graphs and keys into everyone's data.

* Collection: a selected ``live_db`` test whose environment names a localhost endpoint on a shared port exits the
  session with one WARNING (``pytest.ExitCode.USAGE_ERROR``). Only host, port and variable names are printed.
* Runtime backstop: every test runs with ``socket.connect``/``connect_ex`` refusing a localhost shared port, so a
  hardcoded literal anywhere under ``tests/`` fails loudly instead of writing to the shared server.
* ``SMARTMEMORY_TEST_ALLOW_SHARED_DB=1`` runs anyway, with one WARNING saying so.
* Point a ``live_db`` test at a throwaway container on a registered port (``FALKORDB_PORT`` or ``GRAPH_DB_PORT``).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket

import pytest

LOG = logging.getLogger("smartmemory.tests.shared_db_guard")
KILL_SWITCH = "SMARTMEMORY_TEST_ALLOW_SHARED_DB"
# One row per forbidden port.
SHARED_PORTS = {9010: "FalkorDB", 9012: "Redis cache", 9013: "MongoDB"}
# (service, host variables, port variables): what a live_db test can be pointed at. Unset means "not named".
ENV_ENDPOINTS = (
    (
        "FalkorDB",
        ("FALKORDB_HOST", "GRAPH_DB_HOST"),
        ("FALKORDB_PORT", "GRAPH_DB_PORT"),
    ),
    ("Redis", ("REDIS_HOST", "CACHE_REDIS_HOST"), ("REDIS_PORT", "CACHE_REDIS_PORT")),
    ("MongoDB", ("MONGODB_HOST",), ("MONGODB_PORT",)),
)
_LOCAL_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "host.docker.internal", ""}
)


class SharedDBRefused(RuntimeError):
    """A test tried to connect to a shared local database port."""


def is_local(host: str) -> bool:
    name = host.strip().strip("[]").lower()
    if name in _LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _first(env, names):
    return next(
        ((n, env[n].strip()) for n in names if (env.get(n) or "").strip()), (None, None)
    )


def named_endpoints(env=None):
    """``(service, host, port, variable)`` for every endpoint the environment names; the unset half takes localhost."""
    env = os.environ if env is None else env
    found = []
    for service, host_vars, port_vars in ENV_ENDPOINTS:
        port_var, port = _first(env, port_vars)
        host_var, host = _first(env, host_vars)
        if port is None and host is None:
            continue
        if port is not None and not port.isdigit():
            raise ValueError(f"{port_var} is not a port number")
        found.append(
            (
                service,
                host or "localhost",
                int(port) if port else None,
                port_var or host_var,
            )
        )
    return found


def shared_refusals(env=None):
    return [e for e in named_endpoints(env) if e[2] in SHARED_PORTS and is_local(e[1])]


def refuses(address) -> bool:
    """True when a socket address is a localhost shared port."""
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    return (
        address[1] in SHARED_PORTS
        and isinstance(address[0], str)
        and is_local(address[0])
    )


def _stop_or_warn(detail: str) -> None:
    if os.environ.get(KILL_SWITCH) == "1":
        LOG.warning(
            "%s=1: live_db tests run against the shared local databases: %s",
            KILL_SWITCH,
            detail,
        )
        return
    message = (
        "WARNING shared-db-guard: refusing to run live_db tests against the shared local databases: "
        f"{detail}. Use a throwaway container on a registered port (see .claude/rules/test-backends.md); "
        f"{KILL_SWITCH}=1 overrides."
    )
    LOG.warning(message)
    pytest.exit(message, returncode=int(pytest.ExitCode.USAGE_ERROR))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_db: dials a real database; refused against the shared local ports",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session):
    if not any(item.get_closest_marker("live_db") for item in session.items):
        return
    try:
        refused = shared_refusals()
    except ValueError as exc:
        _stop_or_warn(f"cannot verify the endpoints, {exc}")
        return
    if refused:
        _stop_or_warn("; ".join(f"{s} {h}:{p} (from {v})" for s, h, p, v in refused))


@pytest.fixture(autouse=True)
def refuse_shared_db_connections():
    """Runtime backstop: a connect to a localhost shared port raises instead of reaching the shared server."""
    if os.environ.get(KILL_SWITCH) == "1":
        return
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guard(address):
        if refuses(address):
            raise SharedDBRefused(
                f"test connection to shared local port {address[1]} refused ({SHARED_PORTS[address[1]]}); "
                "use a throwaway container on a registered port (.claude/rules/test-backends.md)"
            )

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    # No ``monkeypatch`` here: a plugin autouse fixture runs before the conftest's, so requesting it would move its
    # teardown after the env-leak guard's and make that guard report every monkeypatched variable.
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = real_connect, real_connect_ex
