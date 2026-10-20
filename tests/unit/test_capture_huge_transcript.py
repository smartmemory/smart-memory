"""CAPTURE-HUGE-TRANSCRIPT-1: incremental capture, size cap, dedupe, lock waiters, RSS guard.

Transcripts are generated under tmp_path. Nothing here reads a real transcript.
"""

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from filelock import FileLock

from smartmemory_app import capture_queue as queue, capture_slices as slices
from smartmemory_app import capture_worker as worker

CAP_BYTES = 600  # CAPTURE_MAX_MB=0.0006 is about 629 bytes


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAPTURE_MAX_MB", "0.0006")
    monkeypatch.setattr(queue, "spawn_worker", lambda: None)
    monkeypatch.setattr(worker, "rss_mb", lambda: 10.0)


def lines(count, start=0, width=40):
    return "".join(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s",
                "message": {"content": f"turn {start + i:04d} " + "x" * width},
            }
        )
        + "\n"
        for i in range(count)
    )


@pytest.fixture
def fake_import(monkeypatch):
    """Record every import call: (tail start, tail end, slice text) or None for a whole file."""
    calls = []

    def fake(job, tail=None):
        calls.append(
            None if tail is None else (tail.start, tail.end, tail.path.read_text())
        )
        return {}

    monkeypatch.setattr(worker, "import_capture", fake)
    return calls


def capture(path):
    queue.enqueue("s", str(path), "ws")
    worker.run()
    return queue.jobs()[-1]


def stored_offset(path):
    return slices.read_offset(queue.capture_dir(), str(path.resolve()))


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "projects" / "ws" / "t.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(lines(2))
    return path


def test_offset_first_run_stores_size(transcript, fake_import):
    receipt = capture(transcript)
    assert receipt["status"] == "done" and fake_import == [None]
    assert stored_offset(transcript) == transcript.stat().st_size


def test_offset_tail_only_after_growth(transcript, fake_import):
    capture(transcript)
    first = transcript.stat().st_size
    with transcript.open("a") as handle:
        handle.write(lines(2, start=2))
    receipt = capture(transcript)
    start, end, text = fake_import[1]
    assert (start, end) == (first, transcript.stat().st_size)
    assert text == lines(2, start=2)
    assert receipt["tail_start"] == first and stored_offset(transcript) == end


def test_offset_no_new_bytes_imports_nothing(transcript, fake_import):
    capture(transcript)
    receipt = capture(transcript)
    assert fake_import == [None]
    assert receipt["status"] == "done" and receipt["no_new_bytes"]


def test_offset_shrink_restarts_from_zero_with_warning(transcript, fake_import, caplog):
    transcript.write_text(lines(3))
    capture(transcript)
    transcript.write_text(lines(1))
    receipt = capture(transcript)
    assert fake_import == [None, None]
    assert caplog.text.count("shrank") == 1
    assert (
        receipt["tail_start"] == 0
        and stored_offset(transcript) == transcript.stat().st_size
    )


def test_offset_failure_keeps_it(transcript, monkeypatch):
    monkeypatch.setattr(worker, "import_capture", lambda job, tail=None: {})
    capture(transcript)
    kept = stored_offset(transcript)
    with transcript.open("a") as handle:
        handle.write(lines(2, start=2))
    monkeypatch.setattr(
        worker, "import_capture", Mock(side_effect=RuntimeError("write failed"))
    )
    receipt = capture(transcript)
    assert receipt["status"] == "error" and "write failed" in receipt["error"]
    assert stored_offset(transcript) == kept


def test_offset_write_is_atomic_rename(tmp_path):
    root = tmp_path
    slices.write_offset(root, "/a", 5)
    slices.write_offset(root, "/a", 9)
    assert slices.read_offset(root, "/a") == 9
    assert [
        p.name for p in (root / "offsets").iterdir() if p.name.endswith(".tmp")
    ] == []


def test_cap_over_with_no_offset_is_skipped_and_sets_offset(
    transcript, fake_import, caplog
):
    transcript.write_text(lines(20))
    size = transcript.stat().st_size
    assert size > CAP_BYTES
    receipt = capture(transcript)
    assert receipt["status"] == "skipped" and fake_import == []
    assert str(transcript.resolve()) in caplog.text and str(size) in caplog.text
    assert stored_offset(transcript) == size
    with transcript.open("a") as handle:  # Later SessionEnds capture only the new tail.
        handle.write(lines(1, start=20))
    capture(transcript)
    assert fake_import[0][0] == size


def test_cap_under_is_processed(transcript, fake_import):
    assert capture(transcript)["status"] == "done" and fake_import == [None]


def test_cap_tail_over_cap_is_chunked(transcript, fake_import):
    capture(transcript)
    base = transcript.stat().st_size
    grown = lines(20, start=2)
    with transcript.open("a") as handle:
        handle.write(grown)
    receipt = capture(transcript)
    pieces = fake_import[1:]
    assert len(pieces) > 1 and receipt["slices"] == len(pieces)
    assert all(len(text.encode()) <= CAP_BYTES for _, _, text in pieces)
    assert pieces[0][0] == base
    assert [a[1] for a in pieces[:-1]] == [b[0] for b in pieces[1:]]  # Contiguous.
    assert "".join(text for _, _, text in pieces) == grown
    assert stored_offset(transcript) == transcript.stat().st_size


def test_cap_record_over_cap_is_skipped_with_warning(transcript, fake_import, caplog):
    capture(transcript)
    with transcript.open("a") as handle:
        handle.write(lines(1, width=CAP_BYTES * 2) + lines(1, start=1))
    capture(transcript)
    assert "record over CAPTURE_MAX_MB" in caplog.text
    assert "".join(t for _, _, t in fake_import[1:]) == lines(1, start=1)
    assert stored_offset(transcript) == transcript.stat().st_size


def test_cap_env_values(monkeypatch, caplog):
    monkeypatch.delenv("CAPTURE_MAX_MB")
    assert slices.max_bytes() == 50 * 1024 * 1024
    monkeypatch.setenv("CAPTURE_MAX_MB", "nonsense")
    assert slices.max_bytes() == 50 * 1024 * 1024 and "CAPTURE_MAX_MB" in caplog.text
    monkeypatch.setenv("CAPTURE_MAX_MB", "2")
    assert slices.max_bytes() == 2 * 1024 * 1024


def test_tail_through_real_parser_gets_own_natural_key(transcript, monkeypatch):
    monkeypatch.setenv("CAPTURE_MAX_MB", "50")
    mem = Mock()
    mem._graph.find_by_source_path.return_value = None
    mem.ingest_conversation_sync.return_value = SimpleNamespace(
        chunks_failed=0, chunks_ingested=1, chunk_results=[SimpleNamespace(item_id="c")]
    )
    monkeypatch.setattr(worker, "_memory", lambda: mem)
    monkeypatch.setattr(
        "smartmemory_app.session_lessons.capture_lessons", lambda *a, **k: {}
    )
    capture(transcript)
    with transcript.open("a") as handle:
        handle.write(lines(2, start=2))
    capture(transcript)
    first, second = (c.kwargs for c in mem.ingest_conversation_sync.call_args_list)
    assert len(first["turns"]) == 2 and len(second["turns"]) == 2
    assert "turn 0002" in json.dumps(second["turns"]) and "turn 0000" not in json.dumps(
        second["turns"]
    )
    assert "@" not in first["context"]["source_path"]
    assert "@" in second["context"]["source_path"]
    assert second["context"]["transcript_path"] == str(transcript.resolve())


def test_dedupe_same_path_keeps_the_newer(transcript):
    older = queue.enqueue("s", str(transcript), "ws")
    newer = queue.enqueue("s", str(transcript), "ws")
    by_id = {j["capture_id"]: j["status"] for j in queue.jobs()}
    assert by_id == {older["capture_id"]: "superseded", newer["capture_id"]: "queued"}
    assert queue.counts()["queued"] == 1


def test_dedupe_two_paths_keep_two(transcript, tmp_path):
    other = tmp_path / "other.jsonl"
    other.write_text(lines(1))
    queue.enqueue("s", str(transcript), "ws")
    queue.enqueue("s", str(other), "ws")
    assert [j["status"] for j in queue.jobs()] == ["queued", "queued"]


def test_dedupe_leaves_a_running_item_alone(transcript):
    running = queue.transition(queue.enqueue("s", str(transcript), "ws"), "running")
    queue.enqueue("s", str(transcript), "ws")
    statuses = {j["capture_id"]: j["status"] for j in queue.jobs()}
    assert statuses[running["capture_id"]] == "running"
    assert sorted(statuses.values()) == ["queued", "running"]


def test_lock_held_second_worker_exits_fast(transcript, caplog):
    caplog.set_level(logging.INFO)
    queue.enqueue("s", str(transcript), "ws")
    with FileLock(str(queue.capture_dir() / "worker.lock")):
        began = time.monotonic()
        worker.run()
        assert time.monotonic() - began < 2
    assert "lock is held" in caplog.text
    assert queue.counts()["queued"] == 1  # Left for the holder.


def test_lock_holder_rescans_after_the_final_scan(
    transcript, tmp_path, fake_import, monkeypatch
):
    late = tmp_path / "late.jsonl"
    late.write_text(lines(1))
    real_drain, raced = worker._drain, []

    def drain_then_race(root):
        real_drain(root)
        if (
            not raced
        ):  # An enqueue lands after the last scan, before the lock is released.
            raced.append(queue.enqueue("s", str(late), "ws"))

    monkeypatch.setattr(worker, "_drain", drain_then_race)
    queue.enqueue("s", str(transcript), "ws")
    worker.run()
    assert queue.counts() == {"queued": 0, "done": 2, "error": 0}
    assert len(fake_import) == 2


def test_rss_over_limit_skips_the_item_and_moves_on(
    transcript, tmp_path, fake_import, monkeypatch, caplog
):
    other = tmp_path / "other.jsonl"
    other.write_text(lines(1))
    readings = iter([5000.0, 10.0])
    monkeypatch.setattr(worker, "rss_mb", lambda: next(readings))
    monkeypatch.setenv("CAPTURE_MAX_RSS_MB", "1024")
    queue.enqueue("s", str(transcript), "ws")
    queue.enqueue("s", str(other), "ws")
    worker.run()
    first, second = queue.jobs()
    assert first["status"] == "skipped" and "CAPTURE_MAX_RSS_MB" in first["reason"]
    assert second["status"] == "done"
    assert (
        stored_offset(transcript) is None
    )  # Not advanced: a later SessionEnd retries it.
    assert [r.levelname for r in caplog.records if "RSS" in r.message] == ["WARNING"]


def test_rss_unreadable_does_not_block_capture(transcript, fake_import, monkeypatch):
    monkeypatch.setattr(worker, "rss_mb", lambda: None)
    assert capture(transcript)["status"] == "done"


def test_rss_reader_reads_this_process():
    value = slices.rss_mb()
    assert value is None or value > 1
