"""R5 captured-payload and deterministic durable-queue regressions."""

import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from smartmemory_app import (
    crash_reporter as reporter,
    hook_failures,
    report_outbox as outbox,
)


def payload(report_id):
    return {"properties": {"report_id": report_id}}


@pytest.mark.parametrize(
    "source", ["os-proxy", "setup-url", "setup-url-equals", "api-env"]
)
def test_unsaved_and_os_endpoints_captured(monkeypatch, tmp_path, source):
    host = "test-r5-private.internal"
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    args = ["setup"]
    proxies = {}
    if source == "os-proxy":
        proxies = {"https": f"http://{host}:3128"}
    elif source == "api-env":
        monkeypatch.setenv("SMARTMEMORY_API_URL", f"https://{host}")
    elif source == "setup-url-equals":
        args += [f"--api-url=https://{host}"]
    else:
        args += ["--api-url", f"https://{host}"]
    with (
        patch("urllib.request.getproxies", return_value=proxies),
        patch.object(reporter, "_post", return_value=True) as post,
    ):
        result = reporter.report_exception(
            RuntimeError(f"certificate is not valid for '{host}'"),
            source="install",
            args=args,
            data_dir=tmp_path,
            context={"doctor": [f"SSL peer '{host}'"]},
        )
    assert result.status == "sent"
    body = json.dumps(post.call_args.args[0])
    assert host not in body
    assert "certificate is not valid" in body


def test_startup_flush_before_unconsumed_hook_after_reservation_crash(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    markers = hook_failures.marker_directory()
    markers.mkdir(parents=True)
    (markers / "hook-failures.tsv").write_text("recall\t127\t0\n")
    atomic = outbox._atomic

    def interrupt(path, value):
        if path.name == ".reservations":
            raise KeyboardInterrupt("process exited before reservation commit")
        atomic(path, value)

    with patch.object(outbox, "_atomic", interrupt), pytest.raises(KeyboardInterrupt):
        hook_failures.consume_failures(markers)
    assert outbox.queued_count() == 1
    assert not (markers / ".hook-failure-state.json").exists()
    with patch.object(reporter, "_post", return_value=True) as post:
        reporter.flush_outbox()  # Actual CLI ordering: recovery precedes marker consumption.
        hook_failures.consume_failures(markers)
        reporter.flush_outbox()
    assert post.call_count == 1
    assert outbox.queued_count() == 0
    assert json.loads((markers / ".hook-failure-state.json").read_text())["offset"] > 0


def test_receipt_before_unlink_prevents_second_post():
    root = outbox.directory()
    root.mkdir(parents=True)
    (root / "acked.json").write_text(
        json.dumps({"payload": payload("acked"), "created": time.time()})
    )
    (root / ".delivered").write_text(json.dumps({"acked": time.time()}))
    assert outbox.was_delivered("acked")
    posts = []
    outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    assert posts == []
    assert outbox.queued_count() == 0


@pytest.mark.parametrize("name", [".reservations", ".delivered"])
@pytest.mark.parametrize(
    "bad", ["{", "[]", '{"recent": []}', '{"bad": "not-a-timestamp"}']
)
def test_corrupt_bookkeeping_does_not_block_or_resend(name, bad, caplog):
    root = outbox.directory()
    root.mkdir(parents=True)
    (root / name).write_text(bad)
    outbox.enqueue(payload("healthy"), "healthy", 600)
    posts = []
    for _ in range(2):
        outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    assert [p["properties"]["report_id"] for p in posts] == ["healthy"]
    assert outbox.queued_count() == 0
    assert list(root.glob("*.corrupt"))
    assert any(
        record.levelname == "WARNING" and "quarantin" in record.message.lower()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "bad",
    [
        "{",
        "[]",
        "{}",
        '{"payload": []}',
        '{"payload": {"properties": {"report_id": []}}}',
    ],
)
def test_bad_envelope_does_not_block_healthy_sibling(bad, caplog):
    root = outbox.directory()
    root.mkdir(parents=True)
    path = root / "oldest.json"
    path.write_text(bad)
    old = time.time() - 10
    os.utime(path, (old, old))
    outbox.enqueue(payload("healthy"), "healthy", 600)
    posts = []
    outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    assert [p["properties"]["report_id"] for p in posts] == ["healthy"]
    assert not path.exists()
    assert list(root.glob("*.corrupt"))
    assert "quarantin" in caplog.text.lower()


def test_quarantine_is_bounded():
    root = outbox.directory()
    root.mkdir(parents=True)
    for i in range(15):
        (root / f"bad-{i}.json").write_text("[]")
    outbox.flush(lambda p: pytest.fail("invalid envelope sent"), lambda: True)
    assert 0 < len(list(root.glob("*.corrupt"))) <= 10


def test_full_backlog_enqueue_during_send_preserves_new_recurrence(monkeypatch):
    root = outbox.directory()
    root.mkdir(parents=True)
    now = time.time()
    for i in range(outbox.MAX_REPORTS):
        path = root / f"f{i}.json"
        path.write_text(
            json.dumps({"payload": payload(f"original-{i}"), "created": now - 90000})
        )
        os.utime(path, (now - 90000 + i, now - 90000 + i))
    entered, release = threading.Event(), threading.Event()
    posts, errors = [], []

    def post(p):
        posts.append(p["properties"]["report_id"])
        if posts[-1] == "original-0":
            entered.set()
            assert release.wait(5)
        return True

    def send():
        try:
            outbox.flush(post, lambda: True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert entered.wait(2)
        outbox.enqueue(payload("new"), "new", 600)
        replacement, accepted = outbox.enqueue(payload("recurrence-0"), "f0", 600)
        assert accepted
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors
        # Coalescing with the in-flight report is allowed. Otherwise the new ID must survive or be sent.
        if replacement["properties"]["report_id"] == "recurrence-0":
            queued = [
                json.loads(p.read_text())["payload"]["properties"]["report_id"]
                for p in root.glob("*.json")
            ]
            assert "recurrence-0" in posts or "recurrence-0" in queued
        else:
            assert replacement["properties"]["report_id"] == "original-0"
        assert "new" in posts or any(
            json.loads(p.read_text())["payload"]["properties"]["report_id"] == "new"
            for p in root.glob("*.json")
        )
    finally:
        release.set()
        thread.join(5)


def test_receipt_committed_before_claim_unlink_recovers(monkeypatch):
    outbox.enqueue(payload("acked-claim"), "acked-claim", 600)
    posts = []
    unlink = type(outbox.directory()).unlink

    def interrupt(path, *args, **kwargs):
        if path.suffix == ".sending":
            raise KeyboardInterrupt("exit after receipt, before unlink")
        return unlink(path, *args, **kwargs)

    with (
        patch.object(type(outbox.directory()), "unlink", interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    assert outbox.was_delivered("acked-claim")
    assert outbox.queued_count() == 1
    outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    assert len(posts) == 1
    assert outbox.queued_count() == 0


def test_corrupt_receipts_at_successful_send_are_repaired(caplog):
    outbox.enqueue(payload("healthy-ack"), "healthy-ack", 600)
    posts = []

    def post(p):
        posts.append(p)
        (outbox.directory() / ".delivered").write_text("{")
        return True

    outbox.flush(post, lambda: True)
    outbox.flush(post, lambda: True)
    assert len(posts) == 1
    assert outbox.was_delivered("healthy-ack")
    assert outbox.queued_count() == 0
    assert "Quarantined" in caplog.text
