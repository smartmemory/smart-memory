"""WORKSPACE-PROP-BOUNDARY-1 (smart-memory arms): a caller property never picks the workspace.

One row per local entry in this repo that takes caller properties and is not already
refused at the command (``sm add --prop`` is covered in ``tests/cli/test_workspace_cli.py``).
Core's importer and the MCP ``memory_add`` live in other repos and are not covered here.
"""

from __future__ import annotations

import logging

import pytest

from smartmemory_app import local_api, scope_header

CALLER_WS = "ws_caller0000000"
STAMPED_WS = "ws_stamped000000"


@pytest.fixture(autouse=True)
def _no_ambient_workspace(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)


def _daemon_ingest(properties, *, workspace_id=None, env_workspace=None, monkeypatch):
    """Run the real endpoint and return the properties storage.ingest received."""
    import smartmemory_app.storage as storage

    if env_workspace:
        monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", env_workspace)
    seen = {}

    def fake_ingest(
        content, memory_type="episodic", sync=True, properties=None, origin=None
    ):
        seen["properties"] = properties
        return "id-x"

    monkeypatch.setattr(storage, "ingest", fake_ingest)
    local_api.ingest_endpoint(
        local_api.IngestRequest(
            content="x", properties=properties, workspace_id=workspace_id
        )
    )
    return seen["properties"]


def _daemon_patch(**fields):
    def drive(monkeypatch):
        seen = {}

        class FakeMemory:
            def update_properties(self, item_id, props, write_mode="merge"):
                seen["properties"] = props

        monkeypatch.setattr(local_api, "_get_mem", lambda: FakeMemory())
        local_api.update_memory_item("item-1", local_api.UpdateRequest(**fields))
        return seen["properties"]

    return drive


# (id, driver(monkeypatch) -> properties the store received, expected, warns)
ROWS = [
    (
        "daemon ingest, untagged arm: caller workspace_id dropped",
        lambda mp: _daemon_ingest(
            {"workspace_id": CALLER_WS, "a": "1"}, monkeypatch=mp
        ),
        {"a": "1"},
        True,
    ),
    (
        "daemon ingest, request names a workspace: stamp replaces the caller value",
        lambda mp: _daemon_ingest(
            {"workspace_id": CALLER_WS}, workspace_id=STAMPED_WS, monkeypatch=mp
        ),
        {"workspace_id": STAMPED_WS},
        True,
    ),
    (
        "daemon ingest, workspace from the environment: stamp replaces the caller value",
        lambda mp: _daemon_ingest(
            {"workspace_id": CALLER_WS}, env_workspace=STAMPED_WS, monkeypatch=mp
        ),
        {"workspace_id": STAMPED_WS},
        True,
    ),
    (
        "daemon ingest control: other properties and the stamp pass",
        lambda mp: _daemon_ingest(
            {"project": "atlas"}, workspace_id=STAMPED_WS, monkeypatch=mp
        ),
        {"project": "atlas", "workspace_id": STAMPED_WS},
        False,
    ),
    (
        "daemon-down fallback, no stamp: caller workspace_id dropped",
        lambda mp: scope_header.fallback_properties(
            {}, {"workspace_id": CALLER_WS, "a": "1"}
        ),
        {"a": "1"},
        True,
    ),
    (
        "daemon-down fallback, only a caller workspace_id: nothing left to store",
        lambda mp: scope_header.fallback_properties({}, {"workspace_id": CALLER_WS}),
        None,
        True,
    ),
    (
        "daemon-down fallback, stamp: replaces the caller value",
        lambda mp: scope_header.fallback_properties(
            {"workspace_id": STAMPED_WS}, {"workspace_id": CALLER_WS}
        ),
        {"workspace_id": STAMPED_WS},
        True,
    ),
    (
        "daemon PATCH, properties: caller workspace_id dropped",
        _daemon_patch(properties={"workspace_id": CALLER_WS, "a": "1"}),
        {"a": "1"},
        True,
    ),
    (
        "daemon PATCH, metadata: caller workspace_id dropped",
        _daemon_patch(metadata={"workspace_id": CALLER_WS, "b": "2"}),
        {"b": "2"},
        True,
    ),
    (
        "daemon PATCH control: content and other properties still update",
        _daemon_patch(content="new", properties={"a": "1"}),
        {"a": "1", "content": "new"},
        False,
    ),
]


@pytest.mark.parametrize(
    "driver, expected, warns",
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_caller_property_never_picks_the_workspace(
    driver, expected, warns, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING):
        got = driver(monkeypatch)

    assert got == expected
    logged = [
        r.getMessage() for r in caplog.records if "workspace_id" in r.getMessage()
    ]
    assert bool(logged) is warns
    assert CALLER_WS not in caplog.text  # ids from callers are never logged
