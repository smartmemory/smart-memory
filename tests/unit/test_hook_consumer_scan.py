"""HOOK-CONSUMER-SCAN-1: the consumer does not rescan a large queue it cannot claim from,
and a rescan parses only the records that changed.

Two row tables. The loop rows run the real ``consume`` against 9,000 queued records and count
the scan calls per poll tick. The cache rows count record parses across scans and compare the
result with the pre-cache algorithm (``_uncached_hook_jobs``) on the same fixture.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from smartmemory_app import capture_queue, hook_consumer


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    return capture_queue.hook_queue_dir()


def _write(root: Path, name: str, **fields) -> Path:
    record = {"phase": "observe", "session_id": name, **fields}
    path = root / "queue" / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _uncached_hook_jobs(root: Path) -> list[dict]:
    """The scan as it was before HOOK-CONSUMER-SCAN-1: parse every record, every call."""
    records = []
    for path in (root / "queue").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        mtime_ns = path.stat().st_mtime_ns
        capture_queue._contain_payloads(root, record)
        record.setdefault("queued_ns", mtime_ns)
        if not record.get("session_id"):
            record["session_id"] = (
                capture_queue._payload_session(record.get("payload_path")) or "unknown"
            )
        record["_path"] = str(path)
        records.append(record)
    records.sort(key=lambda r: (r["queued_ns"], Path(r["_path"]).name))
    return records


# ── loop: a full pool does no scan ───────────────────────────────────────────────────

# name, queued records, parallel, poll ticks to watch with every slot busy
LOOP_ROWS = [
    ("9,000 queued, 2 slots both busy", 9000, 2, 25),
    ("9,000 queued, 1 slot busy", 9000, 1, 25),
]


def test_full_pool_makes_no_spill_or_queue_scan(root):
    failures = []
    for name, count, parallel, ticks in LOOP_ROWS:
        for old in (root / "queue").glob("*.json"):
            old.unlink()
        for i in range(count):
            _write(root, f"s{i:05d}", queued_ns=i + 1)
        calls = {"scan": 0, "spill": 0}
        real_scan, real_spill = capture_queue.hook_jobs, capture_queue.adopt_spill

        # only this queue's calls: other tests' consumer threads may still poll their own roots
        def scan(queue_root, *a, _real=real_scan, **k):
            calls["scan"] += Path(queue_root) == root
            return _real(queue_root, *a, **k)

        def spill(queue_root, *a, _real=real_spill, **k):
            calls["spill"] += Path(queue_root) == root
            return _real(queue_root, *a, **k)

        patches = pytest.MonkeyPatch()
        patches.setattr(capture_queue, "hook_jobs", scan)
        patches.setattr(capture_queue, "adopt_spill", spill)
        started, release, stop = threading.Event(), threading.Event(), threading.Event()
        running = []

        def execute(job):
            running.append(job["session_id"])
            if len(running) >= parallel:
                started.set()
            release.wait(20)
            return True

        poll = 0.02
        consumer = threading.Thread(
            target=hook_consumer.consume,
            kwargs=dict(root=root, execute=execute, stop=stop, parallel=parallel, poll=poll),
        )
        consumer.start()
        try:
            assert started.wait(20), name
            time.sleep(poll * 2)  # let the pass that filled the pool finish
            before = dict(calls)
            time.sleep(poll * ticks)
            after = dict(calls)
        finally:
            release.set()
            stop.set()
            consumer.join(20)
            patches.undo()
        if after != before:
            failures.append((name, before, after))
    assert failures == [], f"(row, calls when full, calls after {ticks} ticks): {failures}"


# ── cache: a rescan parses only new or changed records ──────────────────────────────

# name, what to do between the first and the second scan, expected parses on the 2nd scan
CACHE_ROWS = [
    ("nothing changed", lambda root, files: None, 0),
    (
        "one record rewritten",
        lambda root, files: (
            files[3].write_text(json.dumps({"phase": "observe", "session_id": "new"})),
            os.utime(files[3], ns=(1, 2_000_000_000_000)),
        ),
        1,
    ),
    ("one new record", lambda root, files: _write(root, "added"), 1),
    ("one record claimed", lambda root, files: files[5].unlink(), 0),
]


def test_rescan_parses_only_changed_records(root):
    failures = []
    for name, change, expected in CACHE_ROWS:
        for old in (root / "queue").glob("*.json"):
            old.unlink()
        files = [_write(root, f"s{i:03d}") for i in range(40)]
        parses = {"n": 0}
        real = json.loads

        def counting(*a, _real=real, **k):
            parses["n"] += 1
            return _real(*a, **k)

        patches = pytest.MonkeyPatch()
        patches.setattr(json, "loads", counting)
        try:
            first = capture_queue.hook_jobs(root)
            first_parses, parses["n"] = parses["n"], 0
            change(root, files)
            second = capture_queue.hook_jobs(root)
        finally:
            patches.undo()
        gone = {str(p) for p in files if not p.exists()}
        stale = [
            k for k in getattr(capture_queue, "_hook_record_cache", {}) if k in gone
        ]
        if (first_parses, parses["n"], len(first), stale) != (40, expected, 40, []):
            failures.append(
                (name, first_parses, parses["n"], len(first), len(second), stale)
            )
    assert failures == [], f"(row, 1st parses, 2nd parses (want 40, n), 1st, 2nd, evicted-missing): {failures}"


# name, records (file name, fields, payload session or None), 
ORDER_ROWS = [
    ("queued_ns order, ties by name", [("b", {"queued_ns": 5}), ("a", {"queued_ns": 5}), ("c", {"queued_ns": 1})]),
    ("no queued_ns takes mtime", [("m1", {}), ("m2", {})]),
    ("not_before survives", [("n1", {"queued_ns": 2, "not_before": 4102444800.0}), ("n2", {"queued_ns": 1})]),
    ("session from payload", [("p1", {"queued_ns": 1, "session_id": "", "_payload": "from-payload"})]),
    ("session unknown", [("u1", {"queued_ns": 1, "session_id": ""})]),
]


def test_scan_order_and_fields_match_the_uncached_scan(root):
    failures = []
    for name, records in ORDER_ROWS:
        for old in (root / "queue").glob("*.json"):
            old.unlink()
        for stem, fields in records:
            fields = dict(fields)
            payload = fields.pop("_payload", None)
            if payload:
                path = root / "payloads" / f"{stem}.payload.json"
                path.write_text(json.dumps({"session_id": payload}), encoding="utf-8")
                fields["payload_path"] = str(path)
            _write(root, stem, **fields) if "session_id" not in fields else (
                (root / "queue" / f"{stem}.json").write_text(
                    json.dumps({"phase": "observe", **fields}), encoding="utf-8"
                )
            )
        oracle = _uncached_hook_jobs(root)
        cold = capture_queue.hook_jobs(root)
        warm = capture_queue.hook_jobs(root)
        if not (oracle == cold == warm):
            failures.append((name, [r["_path"] for r in oracle], [r["_path"] for r in warm]))
        # a caller mutating its copy must not change the next scan
        warm[0]["session_id"] = "mutated"
        if capture_queue.hook_jobs(root) != oracle:
            failures.append((name, "cache returned a shared record"))
    assert failures == [], failures
