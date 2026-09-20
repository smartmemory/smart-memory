"""Integration tests for the current six lifecycle hook shell scripts."""

import json
import socket
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[2] / "smartmemory_app" / "hooks"


def _unused_local_port() -> int:
    """Reserve and release a local port so lifecycle commands take the direct path."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_HOOK_CASES = (
    (
        "SessionStart",
        "orient.sh",
        {"session_id": "hook-orient", "cwd": "/tmp/project"},
    ),
    (
        "UserPromptSubmit",
        "recall.sh",
        {"session_id": "hook-recall", "prompt": "ok", "cwd": "/tmp/project"},
    ),
    (
        "PostToolUse",
        "observe.sh",
        {
            "session_id": "hook-observe",
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": "done",
            "cwd": "/tmp/project",
        },
    ),
    (
        "Stop",
        "distill.sh",
        {"session_id": "hook-distill", "last_assistant_message": "done"},
    ),
    (
        "PostToolUseFailure",
        "learn.sh",
        {"session_id": "hook-learn", "tool_name": "Bash", "error": "failed"},
    ),
    ("SessionEnd", "persist.sh", {"session_id": "hook-persist"}),
)


@pytest.mark.parametrize("event,script_name,payload", _HOOK_CASES)
def test_lifecycle_hooks_exit_0_with_event_json(
    event, script_name, payload, tmp_path, monkeypatch
):
    """Every registered lifecycle hook accepts its event JSON and exits zero."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config" / "smartmemory"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[lifecycle]\nenabled = false\n", encoding="utf-8"
    )
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(_unused_local_port()))

    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script_name)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"{event} hook {script_name} must exit 0. stderr: {result.stderr.decode()}"
    )


def test_recall_hook_consumes_stdin_json(tmp_path, monkeypatch):
    """recall.sh passes stdin JSON to the lifecycle CLI and persists prompt state."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(_unused_local_port()))
    payload = {
        "session_id": "stdin-contract",
        "prompt": "ok",  # trivial prompt avoids recall while still saving hook state
        "cwd": str(tmp_path),
    }

    result = subprocess.run(
        ["bash", str(HOOKS_DIR / "recall.sh")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode()
    state = json.loads(
        (data_dir / "sessions" / "stdin-contract.json").read_text(encoding="utf-8")
    )
    assert state["session_id"] == "stdin-contract"
    assert state["current_user_turn"] == "ok"
    assert state["turn_count"] == 1
