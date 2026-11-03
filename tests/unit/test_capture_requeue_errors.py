"""CAPTURE-REQUEUE-ERRORS-1: `sm lifecycle requeue --errors` on a real queue in tmp_path."""

import pytest
from click.testing import CliRunner

from smartmemory_app import capture_hold as hold
from smartmemory_app import capture_queue as queue


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # The CLI may flush the report outbox under the real home: keep it in tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_OFFLINE", "1")


def _seed(status, cid, error=None):
    """Put one job in `status`. `held` is a parked file, every other status a ledger event."""
    root = queue.capture_dir()
    record = {
        "capture_id": cid,
        "session_id": f"s-{cid}",
        "transcript_path": f"/nowhere/{cid}.jsonl",
        "workspace_id": "ws",
        "queued_at": 1.0,
    }
    if status == "held":
        hold.park(root, record, hold.HELD)
        return
    extra = {"error": error} if error else {}
    queue._append(root, {**record, "status": status, **extra})


def _status(cid):
    return {j["capture_id"]: j["status"] for j in queue.jobs()}.get(cid)


def _events():
    return len(list((queue.capture_dir() / "events").glob("*.json")))


def _run(*args):
    from smartmemory_app.cli import cli

    return CliRunner().invoke(cli, ["lifecycle", "requeue", "--errors", *args])


# One row per starting status, with --id naming the job: (start, action, ledger after,
# events written). Only `error` moves, everything else is reported skipped and untouched.
STATUS_ROWS = [
    ("error", "requeued", "queued", 1),
    ("done", "skipped", "done", 0),
    ("queued", "skipped", "queued", 0),
    ("held", "skipped", None, 0),  # parked in held-all/, never in the ledger
    ("superseded", "skipped", "superseded", 0),
]


@pytest.mark.parametrize("start,action,after,written", STATUS_ROWS)
def test_status_table_by_id(start, action, after, written):
    _seed(start, "c1")
    before = _events()
    result = _run("--id", "c1")
    assert f"{action} c1 " in result.output
    assert _status("c1") == after
    assert _events() - before == written
    assert result.exit_code == 0


def test_without_id_moves_only_errors_and_leaves_the_rest():
    for status in ("error", "done", "queued", "superseded", "held"):
        _seed(status, f"c-{status}")
    result = _run()
    assert result.exit_code == 0
    assert {
        s: _status(f"c-{s}") for s in ("error", "done", "queued", "superseded", "held")
    } == {
        "error": "queued",
        "done": "done",
        "queued": "queued",
        "superseded": "superseded",
        "held": None,
    }
    assert "requeued c-error error -> queued" in result.output
    assert "1 requeued" in result.output


def test_id_filter_limits_the_move():
    _seed("error", "c1")
    _seed("error", "c2")
    _seed("error", "c3")
    result = _run("--id", "c1", "--id", "c3")
    assert (_status("c1"), _status("c2"), _status("c3")) == (
        "queued",
        "error",
        "queued",
    )
    assert "2 requeued" in result.output


def test_unknown_id_is_reported_and_exits_one_without_blocking_siblings():
    _seed("error", "c1")
    result = _run("--id", "nope", "--id", "c1")
    assert "unknown nope" in result.output
    assert _status("c1") == "queued"
    assert result.exit_code == 1


def test_dry_run_writes_nothing():
    _seed("error", "c1", error="boom")
    before = _events()
    result = _run("--dry-run")
    assert "would requeued c1 error -> queued: boom" in result.output
    assert _events() == before and _status("c1") == "error"
    assert result.exit_code == 0


def test_second_call_is_idempotent():
    _seed("error", "c1", error="boom")
    first = _run()
    written = _events()
    second = _run("--id", "c1")
    assert "requeued c1 error -> queued: boom" in first.output
    assert "skipped c1 queued -> queued" in second.output
    assert _events() == written and _status("c1") == "queued"
    third = _run()
    assert "0 requeued" in third.output and _events() == written


def test_log_line_truncates_the_previous_error_and_the_record_keeps_it():
    _seed("error", "c1", error="E" * 300)
    result = _run()
    assert f"requeued c1 error -> queued: {'E' * 200}\n" in result.output
    assert "E" * 201 not in result.output
    (job,) = queue.jobs()
    assert job["status"] == "queued" and "error" not in job
    assert job["previous_error"] == "E" * 200 and job["requeued_from"] == "error"
    assert job["transcript_path"] == "/nowhere/c1.jsonl"


def test_requeued_job_is_counted_queued_for_the_worker():
    _seed("error", "c1")
    assert queue.counts() == {"queued": 0, "done": 0, "error": 1}
    _run()
    assert queue.counts() == {"queued": 1, "done": 0, "error": 0}
