"""Regression tests for the process-separated lite enrichment worker."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    from smartmemory_app import enrichment_queue

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(enrichment_queue, "_db_path", lambda: db_path)
    return db_path


def _queue_row(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT status, attempts, error FROM enrichment_queue ORDER BY id LIMIT 1"
        ).fetchone()


def test_missing_item_is_retryable_and_never_marked_done(queue_db, caplog):
    from smartmemory_app import enrichment_worker
    from smartmemory_app.enrichment_queue import enqueue, stats

    enqueue("missing-item", {})
    with (
        patch.object(
            enrichment_worker,
            "process_one_job",
            return_value={
                "status": "item_not_found",
                "new_entities": 0,
                "new_relations": 0,
            },
        ),
        caplog.at_level("WARNING"),
    ):
        enrichment_worker.drain_queue()

    assert stats() == {"pending": 1, "processing": 0, "done": 0, "failed": 0}
    assert _queue_row(queue_db) == ("pending", 1, "item_not_found")
    assert "entities and relations were not written" in caplog.text


def test_queue_migrates_legacy_schema_and_tracks_store_generation(
    queue_db, tmp_path, monkeypatch
):
    from smartmemory_app import enrichment_queue, store_generation

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    with sqlite3.connect(queue_db) as conn:
        conn.execute(
            """
            CREATE TABLE enrichment_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                entity_ids TEXT DEFAULT '{}',
                workspace_id TEXT DEFAULT '',
                enqueued_at REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                started_at REAL,
                completed_at REAL,
                error TEXT
            )
            """
        )

    generation = store_generation.bump_store_generation()
    enrichment_queue.enqueue("item-1", {})
    [job] = enrichment_queue.dequeue()

    assert job["attempts"] == 1
    assert job["_store_generation"] == generation


def test_dequeued_job_retains_pre_clear_generation(queue_db, tmp_path, monkeypatch):
    from smartmemory_app import enrichment_queue, store_generation

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    old_generation = store_generation.bump_store_generation()
    enrichment_queue.enqueue("old-item", {})
    new_generation = store_generation.bump_store_generation()

    [job] = enrichment_queue.dequeue()

    assert job["_store_generation"] == old_generation
    assert job["_store_generation"] != new_generation


def test_stale_generation_job_is_not_processed(queue_db, tmp_path, monkeypatch):
    from smartmemory_app import enrichment_worker, store_generation
    from smartmemory_app.enrichment_queue import enqueue

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    store_generation.bump_store_generation()
    enqueue("old-item", {})
    store_generation.bump_store_generation()

    with patch.object(enrichment_worker, "process_one_job") as process:
        assert enrichment_worker.drain_queue() == 0

    process.assert_not_called()


def test_missing_item_fails_after_three_attempts(queue_db):
    from smartmemory_app import enrichment_worker
    from smartmemory_app.enrichment_queue import enqueue, stats

    enqueue("missing-item", {})
    with patch.object(
        enrichment_worker,
        "process_one_job",
        return_value={
            "status": "item_not_found",
            "new_entities": 0,
            "new_relations": 0,
        },
    ):
        enrichment_worker.drain_queue()
        enrichment_worker.drain_queue()
        enrichment_worker.drain_queue()

    assert stats() == {"pending": 0, "processing": 0, "done": 0, "failed": 1}
    assert _queue_row(queue_db) == ("failed", 3, "item_not_found")


@pytest.mark.parametrize("status", ["no_text", "llm_failed"])
def test_non_ok_terminal_status_is_failed_not_done(queue_db, status):
    from smartmemory_app import enrichment_worker
    from smartmemory_app.enrichment_queue import enqueue, stats

    enqueue("item-1", {})
    with patch.object(
        enrichment_worker,
        "process_one_job",
        return_value={"status": status, "new_entities": 0, "new_relations": 0},
    ):
        enrichment_worker.drain_queue()

    assert stats() == {"pending": 0, "processing": 0, "done": 0, "failed": 1}
    assert _queue_row(queue_db) == ("failed", 1, status)


def test_store_generation_change_refreshes_worker_before_processing(
    tmp_path, monkeypatch
):
    from smartmemory_app import enrichment_worker, store_generation

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(enrichment_worker, "_observed_store_generation", None)
    monkeypatch.setattr(enrichment_worker, "_generation_initialized", False)
    store_generation.bump_store_generation()

    old_memory = MagicMock()
    old_memory.get.return_value = {"content": "old item"}
    old_memory._di_context.return_value = nullcontext()
    fresh_memory = MagicMock()
    fresh_memory.get.return_value = {"content": "new item after clear"}
    fresh_memory._di_context.return_value = nullcontext()

    with (
        patch(
            "smartmemory_app.storage.get_memory",
            side_effect=[old_memory, old_memory, fresh_memory, fresh_memory],
        ),
        patch("smartmemory_app.storage._shutdown") as shutdown,
        patch(
            "smartmemory.background.extraction_worker._run_llm_extraction",
            return_value={
                "status": "ok",
                "extraction": {"entities": [], "relations": []},
            },
        ),
        patch(
            "smartmemory.background.extraction_worker.process_extract_job",
            return_value={"status": "ok", "new_entities": 0, "new_relations": 0},
        ) as process,
    ):
        enrichment_worker.process_one_job({"item_id": "old", "entity_ids": {}})
        store_generation.bump_store_generation()  # the clear handler's generation change
        result = enrichment_worker.process_one_job({"item_id": "new", "entity_ids": {}})

    assert result["status"] == "ok"
    shutdown.assert_called_once()
    assert process.call_args.args[0] is fresh_memory
    assert process.call_args.kwargs["item_override"] == {
        "content": "new item after clear"
    }


def test_clear_during_llm_prevents_writing_old_extraction(tmp_path, monkeypatch):
    from smartmemory_app import enrichment_worker, store_generation

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(enrichment_worker, "_observed_store_generation", None)
    monkeypatch.setattr(enrichment_worker, "_generation_initialized", False)
    generation = store_generation.bump_store_generation()
    memory = MagicMock()
    memory.get.return_value = {"content": "old item"}

    def clear_while_extracting(_content):
        store_generation.bump_store_generation()
        return {"status": "ok", "extraction": {"entities": [], "relations": []}}

    with (
        patch("smartmemory_app.storage.get_memory", return_value=memory),
        patch(
            "smartmemory.background.extraction_worker._run_llm_extraction",
            side_effect=clear_while_extracting,
        ),
        patch(
            "smartmemory.background.extraction_worker.process_extract_job"
        ) as process,
    ):
        result = enrichment_worker.process_one_job(
            {"item_id": "old", "entity_ids": {}, "_store_generation": generation}
        )

    assert result["status"] == "store_replaced"
    process.assert_not_called()


def test_success_notifies_daemon_before_marking_done(queue_db):
    from smartmemory_app import enrichment_worker
    from smartmemory_app.enrichment_queue import enqueue, stats

    enqueue("item-1", {})
    result = {
        "status": "ok",
        "new_entities": 1,
        "new_relations": 1,
        "new_entity_nodes": [{"memory_id": "bo", "label": "Bo"}],
        "new_relation_edges": [
            {"source_id": "zed", "target_id": "bo", "edge_type": "TRUSTS"}
        ],
    }
    with (
        patch.object(enrichment_worker, "process_one_job", return_value=result),
        patch.object(enrichment_worker, "_notify_daemon", return_value=True) as notify,
    ):
        enrichment_worker.drain_queue()

    notify.assert_called_once_with(result)
    assert stats()["done"] == 1


def test_clear_preserves_logs_and_bumps_store_generation(tmp_path, monkeypatch):
    from smartmemory_app import local_api, store_generation

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / "memory.db").write_text("db")
    (tmp_path / "vectors.usearch").write_text("vectors")
    daemon_log = tmp_path / "daemon.log"
    worker_log = tmp_path / "worker.log"
    daemon_log.write_text("daemon evidence\n")
    worker_log.write_text("worker evidence\n")
    before = store_generation.bump_store_generation()

    with (
        patch("smartmemory_app.storage._shutdown"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.async_enrichment.reset_queue"),
        patch("smartmemory_app.event_sink.get_event_sink"),
    ):
        result = local_api.clear_all()

    assert result["cleared"] == 2
    assert daemon_log.read_text() == "daemon evidence\n"
    assert worker_log.read_text() == "worker evidence\n"
    assert not (tmp_path / "memory.db").exists()
    assert store_generation.read_store_generation() != before


def test_cli_clear_fallback_preserves_logs_and_bumps_generation(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from smartmemory_app import store_generation
    from smartmemory_app.cli import cli

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    (tmp_path / "memory.db").write_text("db")
    daemon_log = tmp_path / "daemon.log"
    worker_log = tmp_path / "worker.log"
    daemon_log.write_text("daemon evidence\n")
    worker_log.write_text("worker evidence\n")
    before = store_generation.bump_store_generation()

    with (
        patch("smartmemory_app.cli._daemon_request", return_value=None),
        patch("smartmemory_app.storage._shutdown"),
        patch("smartmemory_app.setup._seed_data_dir"),
    ):
        result = CliRunner().invoke(cli, ["clear"], input="y\n")

    assert result.exit_code == 0, result.output
    assert daemon_log.read_text() == "daemon evidence\n"
    assert worker_log.read_text() == "worker evidence\n"
    assert not (tmp_path / "memory.db").exists()
    assert store_generation.read_store_generation() != before
