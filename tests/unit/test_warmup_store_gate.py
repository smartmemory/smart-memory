"""HOOK-CONSUMER-WARMUP-STORE-GATE-FU-1: startup work holds the store lease, and nothing
opens or publishes a store once shutdown has begun.

One row table, one test. Each row runs the daemon's real cleanup
(``_stop_hook_consumer_then_close_store``) against the real warmup thread
(``_start_background_warmup``) or the real storage gate, parks the startup step on an
event (never a sleep) and records what the store saw:

- ``scan``: the filed repro. The startup re-extraction notice walks a real SQLite store
  (real ``SQLiteBackend``, ``SmartGraph``, core ``reextract_offer`` traversal) and is parked
  at the backend read. Shutdown must leave that connection open.
- ``open``: warmup is inside the store's construction when shutdown begins. The store is
  never published; its creator closes it.
- ``late-open``: warmup reaches the store only after cleanup ran. Nothing is constructed.
- ``late-get``: any ``get_memory()`` after cleanup (the notice's own lookup) opens nothing.
- ``atexit``: the exit-time close admits no new store user while it closes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_app import storage
from smartmemory_app import viewer_server as vs
from smartmemory_app.config import SmartMemoryConfig


@dataclass
class Row:
    name: str
    stage: str
    expected: dict = field(default_factory=dict)


ROWS = [
    Row(
        "shutdown during the startup re-extraction scan: the connection stays open",
        "scan",
        {
            "cleanup_closed": False,
            "conn_open_during_scan": True,
            "scan_finished": True,
            "hooks_synced": False,
            "consumer_started": False,
            "store_users_after": 0,
        },
    ),
    Row(
        "shutdown while warmup constructs the store: never published, its creator closes it",
        "open",
        {
            "cleanup_closed": False,
            "published": False,
            "closed_by_creator": True,
            "crash_reports": 0,
            "hooks_synced": False,
            "store_users_after": 0,
        },
    ),
    Row(
        "warmup reaches the store after cleanup: nothing is constructed",
        "late-open",
        {
            "cleanup_closed": True,
            "constructed": 0,
            "published": False,
            "crash_reports": 0,
            "hooks_synced": False,
            "store_users_after": 0,
        },
    ),
    Row(
        "get_memory after cleanup: refused, nothing is constructed",
        "late-get",
        {"cleanup_closed": True, "refused": True, "constructed": 0, "published": False},
    ),
    Row(
        "exit-time close: a store user arriving during the close is refused",
        "atexit",
        {"closed": 1, "user_admitted_during_close": False},
    ),
]


@pytest.fixture
def daemon_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setattr(storage, "_memory", None)
    yield tmp_path
    storage.open_store_gate()
    vs._shutdown_requested.clear()
    vs._hook_consumer = None
    # A warmup stopped by shutdown leaves /health at "warming"; later tests expect none.
    vs._set_startup_state(None)


class _Recorder:
    def __init__(self):
        self.hooks_synced = False
        self.consumer_started = False
        self.crash_reports = 0


def _patch_daemon_edges(stack, rec: _Recorder, cfg) -> None:
    """Replace only what is not under test: the hook copy and consumer start behind their
    real shutdown guards (``_sync_hooks``, ``_start_hook_consumer``), crash reports, config."""
    stack.enter_context(
        patch(
            "smartmemory_app.setup._copy_hooks",
            lambda: setattr(rec, "hooks_synced", True),
        )
    )

    def start_in_daemon():
        rec.consumer_started = True
        return MagicMock()

    stack.enter_context(
        patch("smartmemory_app.hook_consumer.start_in_daemon", start_in_daemon)
    )

    def report(*args, **kwargs):
        rec.crash_reports += 1

    stack.enter_context(
        patch("smartmemory_app.crash_reporter.report_in_background", report)
    )
    stack.enter_context(patch.object(vs, "_startup_line", lambda message: None))
    stack.enter_context(patch("smartmemory_app.config.load_config", return_value=cfg))


def _patch_store_construction(stack, cfg, construct) -> None:
    """The real ``storage.get_memory`` path with core's factory replaced by ``construct``."""
    stack.enter_context(patch.object(storage, "is_configured", return_value=True))
    stack.enter_context(patch.object(storage, "load_config", return_value=cfg))
    stack.enter_context(patch.object(storage, "apply_runtime_config", return_value=cfg))
    stack.enter_context(patch("smartmemory_app.patterns.JSONLPatternStore"))
    stack.enter_context(patch("smartmemory.ontology.pattern_manager.PatternManager"))
    stack.enter_context(patch("smartmemory.tools.factory._require_spacy_model"))
    stack.enter_context(patch("smartmemory.tools.factory._require_embedding_model"))
    stack.enter_context(
        patch("smartmemory.tools.factory.create_lite_memory", construct)
    )
    stack.enter_context(patch("atexit.register"))
    service = MagicMock(backend_name="stub")
    service.embed.return_value = [0.0]
    stack.enter_context(
        patch("smartmemory.plugins.embedding.EmbeddingService", return_value=service)
    )


def _scan(tmp_path) -> dict:
    """The filed repro (filed.md "Exact reproduction"), as a row."""
    from contextlib import ExitStack

    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory.graph.smartgraph import SmartGraph

    backend = SQLiteBackend(str(tmp_path / "memory.db"))
    graph = SmartGraph(backend=backend)
    memory = SimpleNamespace(graph=graph, close=backend.close)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_read = backend.get_all_nodes

    def parked_read():
        entered.set()
        assert release.wait(10)
        try:
            return real_read()
        finally:
            finished.set()

    backend.get_all_nodes = parked_read
    storage._memory = memory
    rec = _Recorder()
    cfg = SimpleNamespace(mode="local")
    with ExitStack() as stack:
        _patch_daemon_edges(stack, rec, cfg)
        stack.enter_context(patch.object(vs, "_warm_backend", return_value=True))
        stack.enter_context(patch.object(storage, "apply_runtime_config"))
        stack.enter_context(patch.object(storage, "get_memory", return_value=memory))
        stack.enter_context(
            patch(
                "smartmemory.utils.llm.llm_route_available",
                return_value=(True, "probe"),
            )
        )
        thread = vs._start_background_warmup()
        try:
            assert entered.wait(10), (
                "startup did not reach the real reextract graph read"
            )
            closed = vs._stop_hook_consumer_then_close_store()
            try:
                backend._conn.execute("SELECT 1")
                conn_open = True
            except Exception:
                conn_open = False
        finally:
            release.set()
            thread.join(10)
            storage._memory = None
            backend.close()
    assert not thread.is_alive()
    return {
        "cleanup_closed": closed,
        "conn_open_during_scan": conn_open,
        "scan_finished": finished.is_set(),
        "hooks_synced": rec.hooks_synced,
        "consumer_started": rec.consumer_started,
        "store_users_after": storage._store_users,
    }


def _warmup_with_parked_store(stage: str) -> dict:
    """Warmup's real ``_warm_backend`` + ``get_memory``, parked at ``stage``."""
    from contextlib import ExitStack

    cfg = SmartMemoryConfig(mode="local", embedding_provider="openai")
    rec = _Recorder()
    entered, release = threading.Event(), threading.Event()
    constructed: list[SimpleNamespace] = []
    closes: list[str] = []

    def construct(**kwargs):
        if stage == "open":
            entered.set()
            assert release.wait(10)
        memory = SimpleNamespace(
            close=lambda: closes.append(threading.current_thread().name)
        )
        constructed.append(memory)
        return memory

    with ExitStack() as stack:
        _patch_daemon_edges(stack, rec, cfg)
        _patch_store_construction(stack, cfg, construct)
        if stage == "late-open":
            # Park warmup on its first line, before it reaches the store.
            def first_line(message):
                if message == "Loading SmartMemory...":
                    entered.set()
                    assert release.wait(10)

            stack.enter_context(patch.object(vs, "_startup_line", first_line))
        thread = vs._start_background_warmup()
        try:
            assert entered.wait(10), f"warmup did not reach the {stage} park"
            closed = vs._stop_hook_consumer_then_close_store()
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive()
        published = storage._memory is not None
        storage._memory = None
    observed = {
        "cleanup_closed": closed,
        "published": published,
        "crash_reports": rec.crash_reports,
        "hooks_synced": rec.hooks_synced,
        "store_users_after": storage._store_users,
    }
    if stage == "open":
        observed["closed_by_creator"] = closes == [thread.name]
    else:
        observed["constructed"] = len(constructed)
    return observed


def _late_get() -> dict:
    from contextlib import ExitStack

    cfg = SmartMemoryConfig(mode="local", embedding_provider="openai")
    constructed: list[object] = []

    def construct(**kwargs):
        constructed.append(object())
        return SimpleNamespace(close=lambda: None)

    with ExitStack() as stack:
        _patch_store_construction(stack, cfg, construct)
        closed = vs._stop_hook_consumer_then_close_store()
        refused = False
        try:
            storage.get_memory()
        except getattr(storage, "StoreClosingError", RuntimeError):
            refused = True
        published = storage._memory is not None
        storage._memory = None
    return {
        "cleanup_closed": closed,
        "refused": refused,
        "constructed": len(constructed),
        "published": published,
    }


def _atexit() -> dict:
    admitted: list[bool] = []
    closes: list[int] = []

    def try_user():
        try:
            with storage.store_in_use():
                admitted.append(True)
        except storage.StoreClosingError:
            admitted.append(False)

    def close():
        # A thread that starts store work while the exit-time close runs.
        user = threading.Thread(target=try_user)
        user.start()
        user.join(5)
        closes.append(1)

    storage._memory = SimpleNamespace(close=close)
    storage._shutdown_at_exit()
    storage._memory = None
    return {"closed": len(closes), "user_admitted_during_close": any(admitted)}


def test_startup_work_holds_the_store_and_nothing_opens_after_shutdown(daemon_state):
    failures = []
    for row in ROWS:
        storage.open_store_gate()
        vs._shutdown_requested.clear()
        vs._hook_consumer = None
        if row.stage == "scan":
            observed = _scan(daemon_state)
        elif row.stage in ("open", "late-open"):
            observed = _warmup_with_parked_store(row.stage)
        elif row.stage == "late-get":
            observed = _late_get()
        else:
            observed = _atexit()
        if observed != row.expected:
            failures.append(f"{row.name}: {observed}")
    assert not failures, "\n".join(failures)
