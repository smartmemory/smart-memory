"""Real local API lifecycle, including persisted SQLite effects and clear."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import traceback
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
# KNOWN_GAP: CORE-LITE-REDIS-PROBE-1 — lite mode probes server Redis; remove when fixed
# Each entry is (module, function qualname, hosts): the innermost core frame of a
# blocked loopback lookup. Unmatched attempts and unmatched entries both fail.
KNOWN_GAP_REDIS_PROBES = [
    (
        "smartmemory.smart_memory",
        "SmartMemory._ruler_reload_redis_client",
        LOOPBACK_HOSTS,
    ),
    (
        "smartmemory.observability.retrieval_tracking",
        "_get_redis_client",
        LOOPBACK_HOSTS,
    ),
    ("smartmemory.streams.pipeline_producer", "_get_redis_client", LOOPBACK_HOSTS),
    ("smartmemory.activation.boost", "_get_redis_client", LOOPBACK_HOSTS),
]


def _attempt_host(event, args):
    address = args[0] if event == "socket.getaddrinfo" else args[1]
    if event == "socket.connect":
        address = address[0] if isinstance(address, tuple) else None
    return address.decode() if isinstance(address, bytes) else address


def _innermost_core_frame(frame):
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module == "smartmemory" or module.startswith("smartmemory."):
            return module, frame.f_code.co_qualname
        frame = frame.f_back
    return None


def _known_gap(attempt):
    return next(
        (
            entry
            for entry in KNOWN_GAP_REDIS_PROBES
            if attempt["frame"] == entry[:2] and attempt["host"] in entry[2]
        ),
        None,
    )


@pytest.fixture
def sqlite_golden(tmp_path, monkeypatch, isolated_support_diagnostics):
    """Reuse conftest's isolated HOME/model cache and close real stores on failure."""
    from smartmemory_app import local_api, storage

    root = tmp_path / f"test_sqlite_golden_{uuid4().hex}"
    home, data = root / "home", root / "data"
    home.mkdir(parents=True)
    processes, killed, network_attempts = [], [], []
    previous_profile = sys.getprofile()
    previous_thread_profile = threading.getprofile()
    active = True
    # Set only once the singleton is proven fresh and its data dir is under root.
    owns_store = False
    yielded = False

    def record_spawn(frame, event, _arg):
        # Observe the actual Popen return, without replacing the spawn or backend.
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            process = frame.f_locals["self"]
            if getattr(process, "pid", None) is not None:
                processes.append(process)

    def no_network(event, args):
        if active and event in {"socket.connect", "socket.getaddrinfo"}:
            network_attempts.append(
                {
                    "event": event,
                    "host": _attempt_host(event, args),
                    "frame": _innermost_core_frame(sys._getframe(1)),
                    "stack": "".join(traceback.format_stack()),
                }
            )
            raise AssertionError(f"SQLite golden attempted network access: {event}")

    def stop_spawned():
        for process in processes:
            if process.poll() is None:
                killed.append(process.pid)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        assert all(process.poll() is not None for process in processes)

    try:
        for name in list(os.environ):
            if name.startswith(
                ("OPENAI_", "GROQ_", "ANTHROPIC_", "SMARTMEMORY_")
            ) or name in {
                "DEEPSEEK_API_KEY",
                "GEMINI_API_KEY",
                "LLM_PROVIDER",
                "BEDROCK_MODEL_ID",
            }:
                monkeypatch.delenv(name)
        for name, value in {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "appdata"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "SMARTMEMORY_MODE": "local",
            "SMARTMEMORY_DATA_DIR": str(data),
            "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
            "SMARTMEMORY_CRASH_REPORTS": "0",
            "SMARTMEMORY_NO_WARM": "1",
            "SMARTMEMORY_NO_UPDATE_CHECK": "1",
            # Pin a cloud model with no credentials so installed Claude SDK cannot
            # auto-select a subscription route. PipelineConfig.lite() then selects
            # the product's existing no-LLM path and local enrichers.
            "SMARTMEMORY_LLM_MODEL": "gpt-4o-mini",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }.items():
            monkeypatch.setenv(name, value)

        assert storage._memory is None, "golden requires a fresh wrapper singleton"
        monkeypatch.setattr(storage, "_data_path", None)
        assert Path.home().resolve().is_relative_to(tmp_path.resolve())
        resolved = storage._resolve_data_dir().resolve()
        assert resolved == data.resolve()
        assert resolved.is_relative_to(tmp_path.resolve()), (
            "refuse any store outside tmp_path"
        )
        owns_store = resolved.is_relative_to(root.resolve())
        assert not any(
            name.startswith(("OPENAI_", "GROQ_", "ANTHROPIC_")) for name in os.environ
        )

        from smartmemory.utils.llm import llm_route_available

        assert not llm_route_available()[0], "no LLM route may be available"
        # KNOWN_GAP: CORE-LITE-REDIS-PROBE-1. These singletons cache a failed probe
        # per process; reset them so every declared probe runs here regardless of
        # test order, keeping the stale-declaration check deterministic.
        from smartmemory.activation import boost
        from smartmemory.observability import retrieval_tracking
        from smartmemory.streams import pipeline_producer

        for module in (boost, retrieval_tracking, pipeline_producer):
            monkeypatch.setattr(module, "_redis_client", None)
            monkeypatch.setattr(module, "_redis_failed", False)
        sys.addaudithook(no_network)
        sys.setprofile(record_spawn)
        threading.setprofile(record_spawn)
        # Same mount as viewer_server, using real route dependencies and storage.
        app = FastAPI()
        app.mount("/memory", local_api.api)
        with TestClient(app) as client:
            memory = storage.get_memory()
            from smartmemory.graph.backends.sqlite import SQLiteBackend

            assert isinstance(memory._graph.backend, SQLiteBackend)
            with closing(
                sqlite3.connect(f"file:{data / 'memory.db'}?mode=ro", uri=True)
            ) as connection:
                actual_db = Path(
                    connection.execute("PRAGMA database_list").fetchone()[2]
                ).resolve()
                assert actual_db.is_relative_to(tmp_path.resolve())
                assert actual_db == resolved / "memory.db"
                assert (
                    connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
                )
            yielded = True
            yield client, actual_db, stop_spawned
    finally:
        try:
            try:
                stop_spawned()
            finally:
                try:
                    # Never flush or close a singleton this fixture did not create
                    # under the golden root, e.g. a pre-existing real ~/.smartmemory.
                    store_path = storage._data_path
                    if owns_store and (
                        storage._memory is None
                        or (
                            store_path is not None
                            and Path(store_path)
                            .resolve()
                            .is_relative_to(root.resolve())
                        )
                    ):
                        storage._shutdown()
                    else:
                        print(
                            "SQLite golden skipped storage._shutdown(): the singleton "
                            "was not created by this fixture under the golden root"
                        )
                finally:
                    active = False
                    sys.setprofile(previous_profile)
                    threading.setprofile(previous_thread_profile)
                    shutil.rmtree(root)
                    print(f"SQLite golden kill list: {killed}")
                    print(f"SQLite golden root removed: {not root.exists()}")
            assert not root.exists()
            matched = [_known_gap(attempt) for attempt in network_attempts]
            print(
                "SQLite golden KNOWN_GAP matches: "
                f"{sorted({(entry[0], entry[1], matched.count(entry)) for entry in matched if entry})}"
            )
            # Any non-loopback attempt, or one outside the declared gap, fails.
            unmatched = [
                f"{attempt['event']} host={attempt['host']!r} frame={attempt['frame']}\n"
                f"{attempt['stack']}"
                for attempt, entry in zip(network_attempts, matched)
                if entry is None
            ]
            # Staleness is only meaningful once the lifecycle body has run.
            stale = [
                entry
                for entry in KNOWN_GAP_REDIS_PROBES
                if yielded and entry not in matched
            ]
        finally:
            # The audit hook stays registered for the interpreter's life (Python has
            # no removal API). It is inert after active=False and now holds no state.
            network_attempts.clear()
            processes.clear()
        assert not unmatched, (
            f"network attempted even if product swallowed the error: {unmatched}"
        )
        assert not stale, f"stale KNOWN_GAP declaration, remove it: {stale}"


def test_sqlite_lifecycle_golden(sqlite_golden):
    client, database, stop_spawned = sqlite_golden
    tokens = [f"test_sqlite_golden_{uuid4().hex}" for _ in range(2)]
    contents = [
        f"The saved SQLite lifecycle note contains {token}." for token in tokens
    ]
    item_ids = []

    for content in contents:
        response = client.post(
            "/memory/ingest",
            json={
                "content": content,
                "memory_type": "episodic",
                "context": {"origin": "cli:add"},
            },
        )
        assert response.status_code == 200, response.text
        item_ids.append(response.json()["item_id"])
    assert len(set(item_ids)) == 2

    # Independent disk reads reject an API echo or in-memory-only save.
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        for item_id, content in zip(item_ids, contents):
            row = connection.execute(
                "SELECT properties FROM nodes WHERE item_id = ?", (item_id,)
            ).fetchone()
            assert row is not None, f"missing persisted item {item_id}"
            assert json.loads(row[0])["content"] == content

    for token, item_id, content in zip(tokens, item_ids, contents):
        response = client.post("/memory/search", json={"query": token, "top_k": 5})
        assert response.status_code == 200, response.text
        assert any(
            item["item_id"] == item_id and item["content"] == content
            for item in response.json()["items"]
        )
        response = client.get(
            "/memory/recall", params={"query": token, "include_snapshot": False}
        )
        assert response.status_code == 200, response.text
        assert content in response.json()["context"]

    # Clear intentionally refuses live owners. Stop only workers recorded at
    # spawn, as a user must stop store owners before requesting this reset.
    stop_spawned()
    # User-facing local API path: local_api.clear_all, also used by CLI clear
    # when its local daemon answers. No direct database delete is used.
    response = client.post("/memory/clear")
    assert response.status_code == 200, response.text
    assert response.json()["cleared"] > 0

    for token in tokens:
        response = client.post("/memory/search", json={"query": token, "top_k": 5})
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        response = client.get(
            "/memory/recall", params={"query": token, "include_snapshot": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["context"] == ""
    response = client.post("/memory/search", json={"query": "*", "top_k": 10})
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        for item_id in item_ids:
            assert (
                connection.execute(
                    "SELECT 1 FROM nodes WHERE item_id = ?", (item_id,)
                ).fetchone()
                is None
            )
