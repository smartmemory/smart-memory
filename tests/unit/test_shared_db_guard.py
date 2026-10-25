"""The shared-DB guard refuses 9010, 9012 and 9013 (TEST-HOLD-RECEIPT-OFF-9010-1)."""

import socket

import pytest

from tests import _shared_db_guard as guard

# One explicit row per forbidden port: (port, service, variable that names it).
ROWS = [
    (9010, "FalkorDB", "FALKORDB_PORT"),
    (9012, "Redis", "REDIS_PORT"),
    (9013, "MongoDB", "MONGODB_PORT"),
]


def test_shared_ports_table_has_exactly_the_three_rows():
    assert set(guard.SHARED_PORTS) == {port for port, _, _ in ROWS}


@pytest.mark.parametrize("row", ROWS, ids=[str(r[0]) for r in ROWS])
def test_named_shared_endpoint_is_refused(row):
    port, service, variable = row
    refused = guard.shared_refusals({variable: str(port)})
    assert [(s, h, p, v) for s, h, p, v in refused] == [
        (service, "localhost", port, variable)
    ]


@pytest.mark.parametrize("row", ROWS, ids=[str(r[0]) for r in ROWS])
def test_socket_connect_to_shared_port_is_refused(row):
    port = row[0]
    sock = socket.socket()
    try:
        with pytest.raises(guard.SharedDBRefused):
            sock.connect(("127.0.0.1", port))
        with pytest.raises(guard.SharedDBRefused):
            sock.connect_ex(("localhost", port))
    finally:
        sock.close()


def test_throwaway_port_and_remote_host_are_not_refused():
    assert guard.shared_refusals({"FALKORDB_PORT": "19001"}) == []
    assert (
        guard.shared_refusals(
            {"FALKORDB_HOST": "db.example.com", "FALKORDB_PORT": "9010"}
        )
        == []
    )
    assert guard.shared_refusals({}) == []
    assert not guard.refuses(("127.0.0.1", 19001)) and not guard.refuses(
        ("db.example.com", 9010)
    )


def test_bad_port_is_a_config_error():
    with pytest.raises(ValueError):
        guard.named_endpoints({"FALKORDB_PORT": "abc"})
