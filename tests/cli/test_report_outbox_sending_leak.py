"""No report-outbox ``.sending`` claim outlives its sender (OPS-REPORT-OUTBOX-SENDING-LEAK-1).

Every ``sm`` command flushes the outbox on a daemon thread (cli.py, crash_reporter
``flush_in_background``). A hook that exits mid-send used to leave its claim behind.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from smartmemory_app import report_outbox as outbox
from tests import _report_leak

ROOT = Path(__file__).resolve().parents[2]
REPORT_ID = "test-sending-leak"

# A short-lived `sm` process: the flush thread claims, then the interpreter exits
# while the transport is still waiting.
EXIT_MID_SEND = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from smartmemory_app import crash_reporter, report_outbox
data_dir = Path(sys.argv[2])
assert report_outbox.__file__.startswith(sys.argv[1]), report_outbox.__file__
report_outbox.enqueue({"properties": {"report_id": sys.argv[3]}}, sys.argv[3], 600,
                      data_dir=data_dir)
crash_reporter._post = lambda payload: time.sleep(30) or True
crash_reporter.enabled = lambda: True
crash_reporter.flush_in_background(data_dir=data_dir)
deadline = time.time() + 5
while not list((data_dir / "report-outbox").glob("*.sending")) and time.time() < deadline:
    time.sleep(0.01)
print(len(list((data_dir / "report-outbox").glob("*.sending"))))
"""


def _counts(root: Path) -> dict:
    return {
        "json": len(list(root.glob("*.json"))),
        "sending": len(list(root.glob("*.sending"))),
    }


def _dead_process() -> tuple[int, float]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    started = psutil.Process(child.pid).create_time()
    child.kill()
    child.wait(5)
    return child.pid, started


def _claim_name(pid: int, started: float, claimed_at: float) -> str:
    lease = f"{int(claimed_at * 1_000_000)}-{'0' * 32}"
    return f"{REPORT_ID}.{pid}.{int(started * 1_000_000)}.{lease}.sending"


def _queued_claim(pid: int, started: float) -> Path:
    outbox.enqueue({"properties": {"report_id": REPORT_ID}}, REPORT_ID, 600)
    pending = outbox.directory() / f"{REPORT_ID}.json"
    claim = pending.with_name(_claim_name(pid, started, time.time()))
    pending.rename(claim)
    return claim


def _exit_mid_send(tmp_path):
    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "SMARTMEMORY_DATA_DIR": str(data_dir),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            EXIT_MID_SEND,
            str(ROOT),
            str(data_dir),
            REPORT_ID,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout.strip() == "1", "precondition: the flush thread claimed mid-send"
    return _counts(data_dir / "report-outbox")


def _interrupt_mid_send(tmp_path):
    outbox.enqueue({"properties": {"report_id": REPORT_ID}}, REPORT_ID, 600)

    def interrupted(payload):
        assert _counts(outbox.directory())["sending"] == 1  # Precondition: claimed.
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        outbox.flush(interrupted, lambda: True)
    return _counts(outbox.directory())


def _claimed_by(pid, started):
    claim = _queued_claim(pid, started)
    posted = []
    delivered = outbox.flush(lambda p: posted.append(p) or True, lambda: True)
    result = {**_counts(outbox.directory()), "posted": len(posted)}
    assert (delivered == {REPORT_ID}) == bool(posted)
    claim.unlink(missing_ok=True)
    return result


def _dead_pid_claim(tmp_path):
    return _claimed_by(*_dead_process())


def _live_pid_claim(tmp_path):
    me = psutil.Process()
    return _claimed_by(me.pid, me.create_time())


def _gate(pid, started, age):
    now = time.time()
    claim = Path("/fake-home/.smartmemory/report-outbox") / _claim_name(
        pid, started, now - age
    )
    found = _report_leak.leaks({}, {str(claim): b"{}"}, now=now)
    return [
        line.replace(str(claim), "<claim>").replace(f" {pid} ", " <pid> ")
        for line in found
    ]


def _gate_put_back(tmp_path):
    outbox_dir = Path("/fake-home/.smartmemory/report-outbox")
    claim = outbox_dir / _claim_name(*_dead_process(), time.time() - 600)
    before = {str(claim): b'{"payload": 1}'}
    after = {str(outbox_dir / f"{REPORT_ID}.json"): b'{"payload": 1}'}
    return _report_leak.leaks(before, after)


GRACE = _report_leak.SENDING_GRACE_SECONDS
ME = psutil.Process()
ROWS = [
    # id, scenario, expected observation
    ("normal-exit-leak", _exit_mid_send, {"json": 1, "sending": 0}),
    ("failure-exit-leak", _interrupt_mid_send, {"json": 1, "sending": 0}),
    ("dead-pid-reclaimed", _dead_pid_claim, {"json": 0, "sending": 0, "posted": 1}),
    ("live-pid-not-reclaimed", _live_pid_claim, {"json": 0, "sending": 1, "posted": 0}),
    ("gate-ignores-young-claim", lambda t: _gate(*_dead_process(), GRACE - 10), []),
    (
        "gate-reports-old-claim-dead-pid",
        lambda t: _gate(*_dead_process(), GRACE + 10),
        [f"<claim> (pid <pid> dead, {GRACE + 10}s old)"],
    ),
    (
        "gate-reports-old-claim-live-pid",
        lambda t: _gate(ME.pid, ME.create_time(), GRACE + 10),
        [f"<claim> (pid <pid> alive, {GRACE + 10}s old)"],
    ),
    ("gate-ignores-put-back-claim", _gate_put_back, []),
]


@pytest.mark.parametrize(
    "scenario,expected", [r[1:] for r in ROWS], ids=[r[0] for r in ROWS]
)
def test_sending_claim_never_outlives_its_sender(tmp_path, scenario, expected):
    assert scenario(tmp_path) == expected
