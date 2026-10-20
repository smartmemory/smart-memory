"""CAPTURE-HUGE-TRANSCRIPT-1: incremental capture, size cap, dedupe, lock waiters, RSS
recycle, spawn cap.

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
_real_spawn_worker = queue.spawn_worker


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CAPTURE_MAX_MB", "0.0006")
    monkeypatch.setattr(queue, "spawn_worker", lambda: None)
    monkeypatch.setattr(queue, "spawn_successor", lambda: None)
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


def test_cap_never_captured_over_cap_drains_fully_across_passes(
    transcript, tmp_path, fake_import
):
    transcript.write_text(lines(20))
    size = transcript.stat().st_size
    assert size > CAP_BYTES
    other = tmp_path / "other.jsonl"
    other.write_text(lines(1))
    queue.enqueue("s", str(transcript), "ws")
    queue.enqueue("s2", str(other), "ws")
    worker.run()
    huge = [
        j for j in queue.jobs() if j["transcript_path"] == str(transcript.resolve())
    ]
    pieces = [c for c in fake_import if c is not None]
    assert len(pieces) > 1 and len(huge) == len(pieces)  # One slice per job.
    assert all(j["status"] == "done" for j in queue.jobs())
    assert pieces[0][0] == 0 and pieces[-1][1] == size
    assert [a[1] for a in pieces[:-1]] == [b[0] for b in pieces[1:]]  # Contiguous.
    assert "".join(text for _, _, text in pieces) == transcript.read_text()
    assert fake_import[1] is None  # The other session ran right after slice 1.
    assert huge[0]["continued_by"] == huge[1]["capture_id"]
    assert stored_offset(transcript) == size


def test_cap_slice_memory_stays_bounded(transcript, monkeypatch):
    """Measured 2026-10-20 (report-c2): RSS grows with what one process holds, so a job
    holds one slice of at most the cap, and only one slice exists at a time."""
    transcript.write_text(lines(20))
    seen = []

    def fake(job, tail=None):
        held = list((queue.capture_dir() / "slices").glob("*/*/*"))
        seen.append((tail.path.stat().st_size, len(held)))
        return {}

    monkeypatch.setattr(worker, "import_capture", fake)
    capture(transcript)
    assert len(seen) > 1
    assert all(size <= CAP_BYTES and held == 1 for size, held in seen)
    assert all(j.get("slices") == 1 for j in queue.jobs())


def test_cap_under_is_processed(transcript, fake_import):
    assert capture(transcript)["status"] == "done" and fake_import == [None]


def test_cap_tail_over_cap_is_chunked(transcript, fake_import):
    capture(transcript)
    base = transcript.stat().st_size
    grown = lines(20, start=2)
    with transcript.open("a") as handle:
        handle.write(grown)
    capture(transcript)
    pieces = fake_import[1:]
    tails = [j for j in queue.jobs() if j.get("tail_start", 0) >= base]
    assert len(pieces) > 1 and len(tails) == len(pieces)
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

    def drain_then_race(root, *args):
        result = real_drain(root, *args)
        if (
            not raced
        ):  # An enqueue lands after the last scan, before the lock is released.
            raced.append(queue.enqueue("s", str(late), "ws"))
        return result

    monkeypatch.setattr(worker, "_drain", drain_then_race)
    queue.enqueue("s", str(transcript), "ws")
    worker.run()
    assert queue.counts() == {"queued": 0, "done": 2, "error": 0}
    assert len(fake_import) == 2


def test_rss_over_limit_recycles_and_leaves_the_item_queued(
    transcript, tmp_path, fake_import, monkeypatch, caplog
):
    other = tmp_path / "other.jsonl"
    other.write_text(lines(1))
    monkeypatch.setattr(worker, "rss_mb", lambda: 5000.0)
    monkeypatch.setenv("CAPTURE_MAX_RSS_MB", "3072")
    lock_free = []

    def successor():
        probe = FileLock(str(queue.capture_dir() / "worker.lock"), timeout=0)
        probe.acquire()  # Raises if the recycling worker still held it.
        probe.release()
        lock_free.append(True)

    monkeypatch.setattr(queue, "spawn_successor", successor)
    queue.enqueue("s", str(transcript), "ws")
    queue.enqueue("s", str(other), "ws")
    worker.run()
    first, second = queue.jobs()
    assert first["status"] == "done"  # A fresh process always takes its first item.
    assert second["status"] == "queued" and stored_offset(other) is None
    assert lock_free == [True]
    assert [r.levelname for r in caplog.records if "RSS" in r.message] == ["WARNING"]


def test_rss_unreadable_does_not_block_capture(transcript, fake_import, monkeypatch):
    monkeypatch.setattr(worker, "rss_mb", lambda: None)
    assert capture(transcript)["status"] == "done"


def test_rss_reader_reads_this_process():
    value = slices.rss_mb()
    assert value is None or value > 1


def test_rss_default_is_3072(monkeypatch):
    monkeypatch.delenv("CAPTURE_MAX_RSS_MB")
    assert slices.max_rss_mb() == 3072


def _fake_pgrep(bin_dir, running):
    bin_dir.mkdir()
    if running is not None:
        script = bin_dir / "pgrep"
        script.write_text(
            "#!/bin/sh\n"
            + "".join(f"echo {1000 + i}\n" for i in range(running))
            + ("exit 0\n" if running else "exit 1\n")
        )
        script.chmod(0o755)


# (id, live workers per pgrep or None when pgrep is missing, SMARTMEMORY_CAPTURE_MAX, spawns)
_SPAWN_CAP_CASES = (
    ("at-cap", 2, None, False),
    ("under-cap", 1, None, True),
    ("off", 9, "0", True),
    ("pgrep-missing-fails-open", None, None, True),
)


@pytest.mark.parametrize(
    "running,cap,spawns",
    [c[1:] for c in _SPAWN_CAP_CASES],
    ids=[c[0] for c in _SPAWN_CAP_CASES],
)
def test_spawn_cap_table(tmp_path, monkeypatch, running, cap, spawns):
    _fake_pgrep(tmp_path / "bin", running)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    if cap is None:
        monkeypatch.delenv("SMARTMEMORY_CAPTURE_MAX")
    else:
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_MAX", cap)
    started = Mock()
    monkeypatch.setattr(queue, "spawn_successor", started)
    _real_spawn_worker()
    log = queue.capture_dir().parent / "hooks.log"
    cap_lines = [
        ln
        for ln in (log.read_text() if log.exists() else "").splitlines()
        if "HOOK-LOAD-CAP-1" in ln
    ]
    assert started.call_count == (1 if spawns else 0)
    assert cap_lines == (
        []
        if spawns
        else [
            "WARNING: capture worker spawn skipped: 2 already running "
            "(cap 2, HOOK-LOAD-CAP-1)"
        ]
    )
