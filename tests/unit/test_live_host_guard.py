"""The live-host guard (CLI-TESTS-LIVE-HOST-CONNECTS-1): no test reaches a real host."""

import os
import socket

import pytest

from tests import _host_isolation as hi

# (what the test does, expected violation recorded). None = allowed.
CONNECT_ROWS = [
    ("connect 93.184.216.34:443", ("93.184.216.34", 443), "connect 93.184.216.34:443"),
    ("connect real daemon port", ("127.0.0.1", 9014), "9014"),
    ("connect loopback test port", ("127.0.0.1", 1), None),
    ("connect ipv6 loopback", ("::1", 1), None),
]


def test_connect_guard_refuses_live_hosts_and_the_real_daemon_port(
    host_isolation_guard,
):
    seen = {}
    for label, address, expected in CONNECT_ROWS:
        sock = socket.socket(
            socket.AF_INET6 if ":" in address[0] else socket.AF_INET,
            socket.SOCK_STREAM,
        )
        sock.settimeout(0.2)
        before = len(host_isolation_guard.connects)
        try:
            sock.connect(address)
        except hi.HostIsolationViolation:
            seen[label] = host_isolation_guard.connects[before:]
        except OSError:
            seen[label] = []  # allowed through to the (closed) loopback port
        finally:
            sock.close()
        if expected is None:
            assert seen[label] == [], (label, seen[label])
        else:
            assert len(seen[label]) == 1 and expected in seen[label][0], (label, seen)
    host_isolation_guard.forgive()


def test_resolving_a_live_name_is_refused_and_recorded(host_isolation_guard):
    with pytest.raises(hi.HostIsolationViolation):
        socket.getaddrinfo("api.example.com", 443)
    assert host_isolation_guard.connects == ["resolve api.example.com"]
    host_isolation_guard.forgive()


def test_proxy_discovery_is_off(host_isolation_guard, monkeypatch):
    """A system proxy on loopback would otherwise hide a live host from the guard."""
    import urllib.request

    assert urllib.request.getproxies() == {}
    assert not [n for n in hi.PROXY_ENV_NAMES if n in os.environ]


def test_cleared_env_keeps_the_conftest_switches_and_drops_the_rest(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    expected = {n: os.environ[n] for n in hi.CONFTEST_ENV_SWITCHES if n in os.environ}
    assert expected["SMARTMEMORY_CRASH_REPORTS"] == "0"
    assert expected["SMARTMEMORY_NO_UPDATE_CHECK"] == "1"
    with hi.cleared_env({"BROWSER": "true"}):
        assert dict(os.environ) == {**expected, "BROWSER": "true"}
    assert os.environ["GROQ_API_KEY"] == "x"
