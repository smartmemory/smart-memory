"""R6 complete-capture privacy and live-sender recovery regressions."""

import json
import os
import threading
import time
from unittest.mock import patch

import psutil
import pytest
from filelock import FileLock

from smartmemory_app import crash_reporter as reporter, report_outbox as outbox


@pytest.mark.parametrize("joined", [False, True], ids=["separate-url", "joined-url"])
def test_setup_equals_path_host_absent_from_captured_payload(
    monkeypatch, tmp_path, joined
):
    host = "new-setup-host.internal"
    url = f"https://{host}/tenant=acme"
    args = ["setup", "--mode", "remote"]
    args += [f"--api-url={url}"] if joined else ["--api-url", url]
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(reporter, "_post", return_value=True) as post:
        result = reporter.report_exception(
            RuntimeError(f"certificate is not valid for '{host}'"),
            source="install",
            args=args,
            data_dir=tmp_path,
            context={"doctor": [f"SSL peer '{host}'"]},
        )
    assert result.status == "sent"
    assert post.call_count == 1
    body = json.dumps(post.call_args.args[0])
    assert host not in body
    assert "certificate is not valid" in body


def test_failed_post_result_lock_timeout_recovers_with_live_pid(monkeypatch, caplog):
    now = [time.time()]
    monkeypatch.setattr(outbox.time, "time", lambda: now[0])
    report = {"properties": {"report_id": "test-r6-lock-timeout"}}
    outbox.enqueue(report, "test-r6-lock-timeout", 600)
    locked, release = threading.Event(), threading.Event()
    errors, attempts = [], []

    def hold_lock():
        try:
            with FileLock(str(outbox.directory() / ".queue.lock"), timeout=2):
                locked.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_lock)

    def fail_post(payload):
        attempts.append(payload["properties"]["report_id"])
        holder.start()
        assert locked.wait(2)
        return False

    try:
        assert outbox.flush(fail_post, lambda: True) == set()
        assert "Timeout" in caplog.text
        assert len(list(outbox.directory().glob("*.sending"))) == 1
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive() and not errors
    delivered = []

    def success(payload):
        delivered.append(payload["properties"]["report_id"])
        return True

    assert (
        outbox.flush(success, lambda: True) == set()
    )  # Lease still protects the claim.
    now[0] += 61
    assert outbox.flush(success, lambda: True) == {"test-r6-lock-timeout"}
    assert outbox.flush(success, lambda: True) == set()
    assert attempts == delivered == ["test-r6-lock-timeout"]
    assert outbox.was_delivered("test-r6-lock-timeout")
    assert outbox.queued_count() == 0


@pytest.mark.parametrize(
    "late_success", [False, True], ids=["late-failure", "late-success"]
)
def test_expired_original_result_cannot_resolve_successor_claim(
    monkeypatch, late_success
):
    now = [time.time()]
    monkeypatch.setattr(outbox.time, "time", lambda: now[0])
    report_id = "test-r6-late-result"
    outbox.enqueue({"properties": {"report_id": report_id}}, report_id, 600)
    original_entered, original_release = threading.Event(), threading.Event()
    successor_entered, successor_release = threading.Event(), threading.Event()
    errors, deliveries = [], []

    def original_post(payload):
        original_entered.set()
        assert original_release.wait(5)
        return late_success

    def successor_post(payload):
        successor_entered.set()
        assert successor_release.wait(5)
        deliveries.append(payload["properties"]["report_id"])
        return True

    def send(post):
        try:
            outbox.flush(post, lambda: True)
        except BaseException as exc:
            errors.append(exc)

    original = threading.Thread(target=send, args=(original_post,))
    successor = threading.Thread(target=send, args=(successor_post,))
    original.start()
    try:
        assert original_entered.wait(2)
        original_claim = next(outbox.directory().glob("*.sending"))
        now[0] += 61
        successor.start()
        assert successor_entered.wait(2)
        successor_claim = next(outbox.directory().glob("*.sending"))
        assert successor_claim != original_claim
        original_release.set()
        original.join(2)
        assert not original.is_alive()
        assert successor_claim.exists()
        assert not outbox.was_delivered(report_id)
        successor_release.set()
        successor.join(2)
        assert not successor.is_alive() and not errors
        assert outbox.flush(successor_post, lambda: True) == set()
        assert deliveries == [report_id]
        assert outbox.was_delivered(report_id)
        assert outbox.queued_count() == 0
    finally:
        original_release.set()
        successor_release.set()
        original.join(5)
        if successor.ident is not None:
            successor.join(5)


@pytest.mark.parametrize(
    "age", [0, 61], ids=["live-legacy-claim", "expired-legacy-claim"]
)
def test_pre_lease_claim_format_preserves_identity_and_recovers(monkeypatch, age):
    now = time.time()
    monkeypatch.setattr(outbox.time, "time", lambda: now)
    report_id = "test-r6-legacy-claim"
    outbox.enqueue({"properties": {"report_id": report_id}}, report_id, 600)
    pending = outbox.directory() / f"{report_id}.json"
    process = psutil.Process()
    claim = pending.with_name(
        f"{report_id}.{process.pid}.{int(process.create_time() * 1_000_000)}.sending"
    )
    pending.rename(claim)
    os.utime(claim, (now - age, now - age))
    posts = []
    result = outbox.flush(lambda p: posts.append(p) or True, lambda: True)
    if age == 0:
        assert result == set() and posts == [] and claim.exists()
    else:
        assert result == {report_id}
        assert [p["properties"]["report_id"] for p in posts] == [report_id]
        assert outbox.was_delivered(report_id)
        assert outbox.queued_count() == 0
