"""CAPTURE-HOLD-ALL-1: the capture hold, the size quarantine and release-held."""

import builtins
import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from smartmemory_app import capture_hold as hold
from smartmemory_app import capture_queue as queue
from smartmemory_app import capture_worker as worker
from smartmemory_app.lifecycle import MemoryLifecycle


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")
    monkeypatch.setenv(
        "SMARTMEMORY_CAPTURE_HOLD", "1"
    )  # The shipped default, set loudly.


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("x" * 5000)
    return str(path)


def _files(name):
    return sorted((queue.capture_dir() / name).glob("*.json"))


def _warnings(caplog):
    """Warnings from the capture modules only (not e.g. workspace resolution)."""
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name.startswith("smartmemory_app.capture_")
    ]


def _ledger(status):
    return [j for j in queue.jobs() if j["status"] == status]


def _queue_while_hold_off(monkeypatch, *paths):
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    jobs = [queue.enqueue(f"s{i}", p, "ws") for i, p in enumerate(paths)]
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "1")
    return jobs


def test_hold_on_enqueue_parks_event_and_spawns_no_worker(
    transcript, monkeypatch, caplog
):
    spawned = []
    monkeypatch.setattr(queue, "spawn_worker", lambda: spawned.append(1))
    monkeypatch.setattr(queue, "spawn_successor", lambda: spawned.append(1))
    life = MemoryLifecycle.__new__(MemoryLifecycle)
    life.session_id = "sess-1"
    life._config = type("C", (), {"enabled": True})()
    life._delete_state = lambda: None
    with caplog.at_level(logging.WARNING):
        life.persist(cwd="/tmp", transcript_path=transcript)
    assert spawned == []
    held = _files(hold.HELD)
    assert len(held) == 1
    event = json.loads(held[0].read_text())
    assert event["session_id"] == "sess-1" and event["status"] == "queued"
    assert queue.jobs() == []  # Not in the pending ledger.
    warned = [r.getMessage() for r in _warnings(caplog)]
    assert len(warned) == 1 and "sess-1" in warned[0] and hold.HOLD_ENV in warned[0]
    assert "held" in warned[0]


def test_hold_on_worker_parks_pending_and_reads_no_bytes(
    transcript, monkeypatch, caplog
):
    job = _queue_while_hold_off(monkeypatch, transcript)[0]
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(
        builtins,
        "open",
        lambda f, *a, **k: opened.append(str(f)) or real_open(f, *a, **k),
    )
    for name in ("capture_transcript", "import_capture", "_memory"):
        monkeypatch.setattr(
            worker, name, lambda *a, **k: pytest.fail("worker touched the transcript")
        )
    real_path_open = Path.open
    monkeypatch.setattr(
        Path,
        "open",
        lambda self, *a, **k: opened.append(str(self)) or real_path_open(self, *a, **k),
    )
    with caplog.at_level(logging.WARNING):
        worker.run()
    assert transcript not in opened
    assert [json.loads(p.read_text())["capture_id"] for p in _files(hold.HELD)] == [
        job["capture_id"]
    ]
    assert queue.counts()["queued"] == 0 and _ledger("held")
    assert len(_warnings(caplog)) == 1


def test_hold_off_under_size_knob_processes(transcript, monkeypatch):
    _queue_while_hold_off(monkeypatch, transcript)
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    monkeypatch.setenv(hold.OVER_ENV, "1")
    seen = []
    monkeypatch.setattr(
        worker, "capture_transcript", lambda j: seen.append(j["session_id"]) or {}
    )
    worker.run()
    assert seen == ["s0"] and _files(hold.HELD) == [] and _files(hold.LARGE) == []


def test_hold_off_over_size_knob_quarantines_without_reading(
    transcript, monkeypatch, caplog
):
    _queue_while_hold_off(monkeypatch, transcript)
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    monkeypatch.setenv(hold.OVER_ENV, "0.001")
    monkeypatch.setattr(
        worker, "capture_transcript", lambda j: pytest.fail("read a large transcript")
    )
    with caplog.at_level(logging.WARNING):
        worker.run()
    assert len(_files(hold.LARGE)) == 1 and _files(hold.HELD) == []
    assert _ledger("quarantined") and queue.counts()["queued"] == 0
    message = _warnings(caplog)[0].getMessage()
    assert len(_warnings(caplog)) == 1 and "s0" in message and hold.OVER_ENV in message


def test_size_knob_zero_with_hold_off_parks_nothing(transcript, monkeypatch):
    _queue_while_hold_off(monkeypatch, transcript)
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    monkeypatch.setenv(hold.OVER_ENV, "0")
    seen = []
    monkeypatch.setattr(worker, "capture_transcript", lambda j: seen.append(1) or {})
    worker.run()
    assert seen == [1] and _files(hold.LARGE) == []


def test_second_session_end_for_a_held_path_replaces_the_first(transcript):
    queue.enqueue("first", transcript, "ws")
    second = queue.enqueue("second", transcript, "ws")
    held = _files(hold.HELD)
    assert len(held) == 1
    assert json.loads(held[0].read_text())["capture_id"] == second["capture_id"]


def test_large_quarantine_keeps_one_event_per_path(transcript, monkeypatch):
    for _ in range(2):
        _queue_while_hold_off(monkeypatch, transcript)
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
        monkeypatch.setenv(hold.OVER_ENV, "0.001")
        worker.run()
        monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "1")
    assert len(_files(hold.LARGE)) == 1


def test_hold_flag_table(monkeypatch):
    rows = [
        (None, True),
        ("", True),
        ("1", True),
        ("0", False),
        ("off", False),
        ("typo", True),
    ]
    for raw, expected in rows:
        if raw is None:
            monkeypatch.delenv(hold.HOLD_ENV)
        else:
            monkeypatch.setenv(hold.HOLD_ENV, raw)
        assert hold.hold_on() is expected, raw


def test_release_dry_run_lists_and_moves_nothing(transcript, capsys):
    queue.enqueue("s0", transcript, "ws")
    assert queue.main(["release-held", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would released s0" in out and len(_files(hold.HELD)) == 1
    assert queue.jobs() == []


def test_release_refused_while_hold_on(transcript, capsys):
    queue.enqueue("s0", transcript, "ws")
    assert queue.main(["release-held"]) == 1
    assert "refused" in capsys.readouterr().err
    assert len(_files(hold.HELD)) == 1 and queue.jobs() == []


def test_release_force_moves_back_exactly_once(transcript, monkeypatch):
    spawned = []
    monkeypatch.setattr(queue, "spawn_worker", lambda: spawned.append(1))
    queue.enqueue("s0", transcript, "ws")
    assert queue.main(["release-held", "--force"]) == 0
    assert _files(hold.HELD) == [] and len(_ledger("queued")) == 1
    assert queue.main(["release-held", "--force"]) == 0
    assert len(_ledger("queued")) == 1 and spawned == []


def test_release_with_hold_off_needs_no_force(transcript, monkeypatch):
    queue.enqueue("s0", transcript, "ws")
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    assert queue.main(["release-held"]) == 0
    assert len(_ledger("queued")) == 1


def test_release_large_table(transcript, monkeypatch, capsys):
    _queue_while_hold_off(monkeypatch, transcript)
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    monkeypatch.setenv(hold.OVER_ENV, "0.001")
    worker.run()
    assert len(_files(hold.LARGE)) == 1
    assert queue.main(["release-held", "--large"]) == 1  # Knob would park it again.
    assert "refused" in capsys.readouterr().err
    monkeypatch.setenv(hold.OVER_ENV, "0")
    assert queue.main(["release-held", "--large"]) == 0
    assert _files(hold.LARGE) == [] and len(_ledger("queued")) == 1
    monkeypatch.setenv(hold.OVER_ENV, "0.001")
    assert queue.main(["release-held", "--large", "--force"]) == 0  # Nothing left.


def test_release_keeps_newest_per_path_and_supersedes_the_rest(transcript, tmp_path):
    root = queue.capture_dir()
    (root / hold.HELD).mkdir()
    for n, sid in enumerate(("old", "mid", "new")):
        (root / hold.HELD / f"{n:020d}-a.json").write_text(
            json.dumps(_chat_event(sid, transcript, f"id{n}"))
        )
    other = str(tmp_path / "other.jsonl")
    (root / hold.HELD / f"{9:020d}-b.json").write_text(
        json.dumps(_chat_event("solo", other, "id9"))
    )
    hold_off = pytest.MonkeyPatch()
    hold_off.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    try:
        assert queue.main(["release-held"]) == 0
    finally:
        hold_off.undo()
    queued = {j["session_id"] for j in _ledger("queued")}
    assert queued == {"new", "solo"}
    assert {j["session_id"] for j in _ledger("superseded")} == {"old", "mid"}
    assert _files(hold.HELD) == []


def _chat_event(session, path, cid):
    """The key set of a file s1-chat moved by hand into held-all/ (read from a live file)."""
    return {
        "capture_id": cid,
        "cwd": "/Users/x/proj",
        "queued_at": 1.0,
        "recorded_at": 1.0,
        "session_id": session,
        "status": "queued",
        "transcript_path": path,
        "workspace_id": "ws",
    }


def test_hand_moved_file_format_is_accepted(transcript, monkeypatch):
    root = queue.capture_dir()
    (root / hold.HELD).mkdir()
    (
        root / hold.HELD / "01792419164152026000-c246d493239c4363a3af91155e750087.json"
    ).write_text(
        json.dumps(_chat_event("chat", transcript, "c246d493239c4363a3af91155e750087"))
    )
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    assert queue.main(["release-held"]) == 0
    (job,) = _ledger("queued")
    assert job["capture_id"] == "c246d493239c4363a3af91155e750087"
    assert job["transcript_path"] == transcript


def test_click_command_dry_run(transcript):
    from smartmemory_app.cli import cli

    queue.enqueue("s0", transcript, "ws")
    result = CliRunner().invoke(cli, ["lifecycle", "release-held", "--dry-run"])
    assert result.exit_code == 0 and "would released s0" in result.output
