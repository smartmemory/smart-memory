"""Whitespace credentials and database-free shutdown retirement regressions."""

import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import daemon
from smartmemory_app.cli import cli
from smartmemory_app.diagnostics import redact_credentials
from tests.unit import test_daemon_lifecycle
from tests.unit.test_daemon_deadlines import (
    SECRETS,
    test_startup_render_boundaries as check_startup_boundary,
)

launchd = test_daemon_lifecycle.launchd

WHITESPACE_SECRETS = [
    ("Authentication failed with password fixture-password", "fixture-password"),
    ("Invalid api_key fixture-api-value", "fixture-api-value"),
    ("Authorization Basic Zml4dHVyZTpwYXNzd29yZA==", "Zml4dHVyZTpwYXNzd29yZA=="),
]
LABEL_SECRETS = [
    (f"Invalid {label}\tfixture-value useful-context", "fixture-value")
    for label in (
        "password",
        "passwd",
        "pwd",
        "api_key",
        "api-key",
        "apikey",
        "API key",
        "secret",
        "token",
        "access_key",
        "access-key",
        "accesskey",
        "key",
    )
] + [("Authorization Bearer fixture-bearer", "fixture-bearer")]
PATHS = [
    "/private/tmp/sk-project/data/daemon.log",
    '"/Users/monkey Smith/.smartmemory/daemon.log"',
    "/private/tmp/secret folder/data/daemon.log",
]


@pytest.mark.parametrize("message,secret", WHITESPACE_SECRETS + LABEL_SECRETS + SECRETS)
def test_credential_table_preserves_paths(message, secret):
    paths = " and ".join(PATHS)
    output = redact_credentials(message + " at " + paths)
    assert secret not in output
    assert paths in output
    if "useful-context" in message:
        assert "useful-context" in output
    assert redact_credentials(output) == output


@pytest.mark.parametrize(
    "message",
    ["Failure at " + path for path in PATHS]
    + ["monkey Smith", "secretary folder", "passwordless login", "mytoken value"],
)
def test_whitespace_labels_use_complete_words(message):
    assert redact_credentials(message) == message


@pytest.mark.parametrize("message,secret", WHITESPACE_SECRETS)
@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("boundary", ["tail", "stream", "exception", "health"])
def test_whitespace_startup_boundaries(
    launchd, monkeypatch, message, secret, command, boundary
):
    check_startup_boundary(launchd, monkeypatch, message, secret, command, boundary)


@pytest.mark.parametrize("message,secret", WHITESPACE_SECRETS)
def test_whitespace_health_endpoint(tmp_path, monkeypatch, message, secret):
    from smartmemory_app.viewer_server import _build_app

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    config = SimpleNamespace(
        mode="local", llm_provider="none", embedding_provider="local"
    )
    with (
        TestClient(_build_app()) as client,
        patch("smartmemory_app.config.load_config", return_value=config),
        patch("smartmemory_app.config.llm_key_present", return_value=False),
        patch("smartmemory_app.storage.get_memory", side_effect=RuntimeError(message)),
        patch("smartmemory_app.work_graph.get_work_status", return_value={}),
    ):
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert secret not in response.json()["degraded_reason"]


@pytest.mark.parametrize("budget", [None, 0.8])
@pytest.mark.parametrize("legacy_identity_lost", [False, True])
def test_sqlite_lock_does_not_delay_stop_signals(
    launchd, monkeypatch, caplog, budget, legacy_identity_lost
):
    from smartmemory.pipeline.work_graph import spawn
    from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph

    data = daemon._data_dir()
    database = data / "memory.db"
    store = SQLiteWorkGraph(str(database))
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE enrichment_queue (item_id TEXT, status TEXT)")
        conn.execute("INSERT INTO enrichment_queue VALUES ('flight', 'processing')")
        conn.commit()
        if legacy_identity_lost:
            # Also prove failure diagnostics do not perform a blocking queue read.
            conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    if legacy_identity_lost:
        (data / "worker.0.pid").write_text("invalid")
    locked = threading.Event()
    release = threading.Event()
    writer_errors = []

    def hold_write_lock():
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "BEGIN EXCLUSIVE" if legacy_identity_lost else "BEGIN IMMEDIATE"
            )
            locked.set()
            release.wait(11.2 if budget is None else 1.5)
            conn.rollback()
        except Exception as exc:
            writer_errors.append(exc)
        finally:
            conn.close()

    writer = threading.Thread(target=hold_write_lock)
    writer.start()
    signals = []
    running = True
    real_kill = daemon.os.kill

    def kill(pid, sig):
        nonlocal running
        if pid == 434343:
            signals.append(sig)
            if sig == signal.SIGTERM:
                running = False
        else:
            return real_kill(pid, sig)

    def run(command, **kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(
                command, 0, "python -m smartmemory_app.worker_entry", ""
            )
        return launchd.run(command, **kwargs)

    monkeypatch.setattr(daemon.os, "kill", kill)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(spawn, "worker_is_running", lambda data: running)
    (data / ".worker.pid").write_text("434343")
    launchd.loaded = True
    daemon._pid_file().write_text("424242")
    try:
        assert locked.wait(2)
        started = time.monotonic()
        with daemon.lifecycle_budget(budget) if budget is not None else nullcontext():
            result = CliRunner().invoke(cli, ["stop"])
        elapsed = time.monotonic() - started
        print(
            "SQLite locked stop", budget or 10, "elapsed", elapsed, "signals", signals
        )
        assert elapsed < (budget or 10) + 0.4
        assert result.exit_code == 0, result.output
        assert writer.is_alive(), "Shutdown waited for the database writer"
        assert signal.SIGTERM in signals
        assert "bootout" in launchd.events
        assert not daemon._pid_file().exists()
        assert "deferred" in caplog.text.lower()
        assert "shutdown budget" in caplog.text.lower()
    finally:
        release.set()
        writer.join(3)
    assert not writer.is_alive()
    assert not writer_errors
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT status FROM enrichment_queue").fetchone()[0]
            == "processing"
        )
    assert store.stats()["runs"] == 0

    replacements = []

    def replace(*args, **kwargs):
        assert signal.SIGTERM in signals
        assert store.stats()["runs"] == 1
        with sqlite3.connect(database) as conn:
            assert (
                conn.execute("SELECT status FROM enrichment_queue").fetchone()[0]
                == "migrated"
            )
        replacements.append(args)

    monkeypatch.setattr(subprocess, "Popen", replace)
    if legacy_identity_lost:
        with pytest.raises(RuntimeError, match="Invalid legacy PID"):
            daemon._start_workers()
        assert (data / "worker.0.pid").exists()
        assert not replacements
        assert store.stats()["runs"] == 0
    else:
        daemon._start_workers()
        assert len(replacements) == 1
        assert store.migrate_enrichment_queue(recover_processing=True) == 0
