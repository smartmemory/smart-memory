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
        "daemon ingest, workspace_id nested in metadata: dropped on the untagged arm",
        lambda mp: _daemon_ingest(
            {"metadata": {"workspace_id": CALLER_WS, "m": "1"}, "a": "1"},
            monkeypatch=mp,
        ),
        {"metadata": {"m": "1"}, "a": "1"},
        True,
    ),
    (
        "daemon ingest, workspace_id three metadata levels deep: dropped, stamp applies",
        lambda mp: _daemon_ingest(
            {"metadata": {"metadata": {"metadata": {"workspace_id": CALLER_WS}}}},
            workspace_id=STAMPED_WS,
            monkeypatch=mp,
        ),
        {"metadata": {"metadata": {"metadata": {}}}, "workspace_id": STAMPED_WS},
        True,
    ),
    (
        "daemon-down fallback, workspace_id nested in metadata: dropped",
        lambda mp: scope_header.fallback_properties(
            {}, {"metadata": {"workspace_id": CALLER_WS, "m": "1"}}
        ),
        {"metadata": {"m": "1"}},
        True,
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


# ── r1 HIGH-1: every representation of a PATCH body, on real SQLite ────────────
# Core lifts a ``metadata`` dict, and ``metadata`` inside ``metadata`` to any depth,
# onto the node root before it writes, so a nested workspace_id lands where a flat one
# does. Each row drives the real PATCH function against core's real CRUD on SQLite.

OWNER_WS = "ws_owner00000000"


def _nest(depth: int, leaf: dict) -> dict:
    """``leaf`` wrapped in ``depth`` levels of ``{"metadata": ...}``."""
    for _ in range(depth):
        leaf = {"metadata": leaf}
    return leaf


# (id, request field carrying the body, nesting depth, write mode)
PATCH_REPRESENTATIONS = [
    ("properties-d0-merge", "properties", 0, "merge"),
    ("properties-d0-replace", "properties", 0, "replace"),
    ("properties-d1-merge", "properties", 1, "merge"),
    ("properties", "properties", 1, "replace"),  # reviewer's r1 row
    ("properties-d2-merge", "properties", 2, "merge"),
    ("properties-d2-replace", "properties", 2, "replace"),
    ("properties-d3-merge", "properties", 3, "merge"),
    ("properties-d3-replace", "properties", 3, "replace"),
    ("metadata-d0-merge", "metadata", 0, "merge"),
    ("metadata-d0-replace", "metadata", 0, "replace"),
    ("metadata-d1-merge", "metadata", 1, "merge"),
    ("metadata", "metadata", 1, "replace"),  # reviewer's r1 row
    ("metadata-d2-merge", "metadata", 2, "merge"),
    ("metadata-d3-replace", "metadata", 3, "replace"),
]


@pytest.fixture
def real_sqlite_item(monkeypatch, tmp_path):
    """The real PATCH function over core's real CRUD on a SQLite store."""
    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory.graph.smartgraph import SmartGraph
    from smartmemory.lightweight import CrudMemory
    from smartmemory.scope_provider import DefaultScopeProvider

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    backend = SQLiteBackend()
    try:
        graph = SmartGraph(backend=backend)
        memory = CrudMemory(DefaultScopeProvider(single_tenant=True), graph=graph)
        graph.add_node(
            "wpb_item",
            {"content": "synthetic", "workspace_id": OWNER_WS, "note": "before"},
            memory_type="semantic",
        )
        monkeypatch.setattr(local_api, "_get_mem", lambda: memory)
        yield backend
    finally:
        backend.close()


def _patch_real(backend, field, depth, mode, leaf):
    body = {field: _nest(depth, leaf)}
    local_api.update_memory_item(
        "wpb_item",
        local_api.UpdateRequest(content="synthetic updated", write_mode=mode, **body),
    )
    return backend.get_node("wpb_item")


@pytest.mark.parametrize(
    "field, depth, mode",
    [row[1:] for row in PATCH_REPRESENTATIONS],
    ids=[row[0] for row in PATCH_REPRESENTATIONS],
)
def test_patch_cannot_move_workspace_through_nested_metadata(
    real_sqlite_item, field, depth, mode
):
    persisted = _patch_real(
        real_sqlite_item, field, depth, mode, {"workspace_id": CALLER_WS}
    )
    assert persisted["workspace_id"] == OWNER_WS


# Controls: a non-reserved property still updates at every depth, beside a reserved one.
PATCH_CONTROLS = [
    ("flat-merge", "properties", 0, "merge"),
    ("flat-replace", "properties", 0, "replace"),
    ("nested-merge", "properties", 2, "merge"),
    ("nested-replace", "metadata", 2, "replace"),
]


@pytest.mark.parametrize(
    "field, depth, mode",
    [row[1:] for row in PATCH_CONTROLS],
    ids=[row[0] for row in PATCH_CONTROLS],
)
def test_patch_nested_non_reserved_property_still_updates(
    real_sqlite_item, field, depth, mode
):
    persisted = _patch_real(
        real_sqlite_item,
        field,
        depth,
        mode,
        {"note": "after", "workspace_id": CALLER_WS},
    )
    assert persisted["note"] == "after"
    assert persisted["workspace_id"] == OWNER_WS
