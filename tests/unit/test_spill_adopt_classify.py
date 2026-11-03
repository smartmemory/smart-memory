"""SM-SPILL-ADOPT-CLASSIFY-1: observe spills pass the remote-skip classifier before they reach the hook queue.

One table over both spill entry points: ``adopt_spill`` (a spill file written before the hook had the
check) and ``requeue_dead_hooks`` (an adopted spill job that sat in ``dead/``). Each row builds one input
in a temp data dir, runs the entry point once, and checks where the file ended up and what was logged.
"""

from __future__ import annotations

import json
import logging

import pytest

from smartmemory_app import capture_queue as queue, observe_classify

STAMP = "20261025T100001000000Z"
SPILL = f"{STAMP}-7-observe.json"
JOB = f"spill-{STAMP}-7.observe.json"
SECRET = "hunter2-payload-text"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "SMARTMEMORY_OBSERVE_REMOTE_TOKENS_EXTRA",
        "SMARTMEMORY_OBSERVE_LOCAL_EXTRA",
    ):
        monkeypatch.delenv(name, raising=False)
    queue._spill_warned.clear()
    queue._spill_backoff.clear()


def _bash(command: str) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": SECRET},
    }


def _envelope(payload) -> str:
    return json.dumps(
        {"v": 1, "phase": "observe", "ts": "x", "pid": 7, "payload": payload}
    )


def _write_spill(root, body: str) -> None:
    spill = queue.hook_spill_dir(root)
    spill.mkdir(exist_ok=True)
    (spill / SPILL).write_text(body + "\n")


def _write_dead_adopted(root, payload) -> None:
    """An adopted spill job as ``_adopt_one`` wrote it before the classifier, dead-lettered since."""
    payload_path = root / "payloads" / JOB
    payload_path.write_text(json.dumps(payload))
    record = {
        "v": 1,
        "phase": "observe",
        "session_id": "s1",
        "cwd": None,
        "workspace_id": None,
        "payload_path": str(payload_path),
        "state_snapshot": None,
        "queued_ns": 1,
        "spill": SPILL,
        "attempts": 3,
        "last_error": "down",
    }
    (root / "dead" / JOB).write_text(json.dumps(record))


def _boom(payload):
    raise RuntimeError(SECRET)


# label, entry point, input kind, input, classifier override, expected (queue/, quarantine/, dead/, action)
ROWS = [
    ("pre-classifier remote Bash spill", "adopt", "payload", _bash("ssh host uptime"), None, ([], [SPILL], [], 0)),
    ("quoted ssh beats the token list, not the allowlist", "adopt", "payload", _bash("s''sh host uptime"), None, ([], [SPILL], [], 0)),
    ("infra MCP spill", "adopt", "payload", {"session_id": "s1", "tool_name": "mcp__smartmemory-infra__run", "tool_input": {"cmd": "uptime"}}, None, ([], [SPILL], [], 0)),
    ("local Edit spill", "adopt", "payload", {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "/w/ssh.md"}}, None, ([JOB], [], [], 1)),
    ("local Read spill", "adopt", "payload", {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": "/w/a.py"}}, None, ([JOB], [], [], 1)),
    ("provably local Bash spill", "adopt", "payload", _bash("git status && ls -la | head"), None, ([JOB], [], [], 1)),
    ("classifier error fails closed", "adopt", "payload", {"session_id": "s1", "tool_name": "Read"}, _boom, ([], [SPILL], [], 0)),
    ("malformed spill: dead-lettered as before", "adopt", "body", "{not json", None, ([], [], [f"spill-{SPILL}"], 0)),
    ("requeue of an adopted remote spill", "requeue", "payload", _bash("rsync -a x host:/y"), None, ([], [SPILL], [], "quarantined")),
    ("requeue of an adopted local spill", "requeue", "payload", {"session_id": "s1", "tool_name": "Read"}, None, ([JOB], [], [], "requeued")),
]  # fmt: skip


def test_spill_entry_points_classify_observe_payloads(monkeypatch, caplog):
    got, want = [], []
    for label, entry, kind, value, override, expected in ROWS:
        root = queue.hook_queue_dir(
            queue.hook_queue_dir().parent / f"q-{len(got)}" / "hook-queue"
        )
        with monkeypatch.context() as m:
            if override is not None:
                m.setattr(observe_classify, "_precheck", override)
            caplog.clear()
            with caplog.at_level(
                logging.WARNING, logger="smartmemory_app.capture_queue"
            ):
                if entry == "adopt":
                    _write_spill(root, value if kind == "body" else _envelope(value))
                    action = queue.adopt_spill(root)
                    again = queue.adopt_spill(
                        root
                    )  # a quarantined file is never adopted on the next sweep
                else:
                    _write_dead_adopted(root, value)
                    action = queue.requeue_dead_hooks(root, [JOB])[0][0]
                    again = 0
        quarantine = queue.hook_spill_dir(root) / queue.SPILL_QUARANTINE
        observed = (
            sorted(p.name for p in (root / "queue").glob("*.json")),
            sorted(p.name for p in quarantine.glob("*.json"))
            if quarantine.is_dir()
            else [],
            sorted(p.name for p in (root / "dead").glob("*.json")),
            action,
        )
        got.append((label, observed, again, SECRET in caplog.text))
        want.append((label, expected, 0, False))
    assert got == want
