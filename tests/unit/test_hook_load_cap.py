"""HOOK-LOAD-CAP-1 load skip (recall) and its removal from observe/learn/distill (HOOK-WRITE-QUEUE-1).

One row per case. `smartmemory`, `pgrep` and `sysctl` are fakes on a temp PATH and HOME is a temp dir,
so nothing here touches the real ~/.smartmemory or a live process table. The write hooks no longer
count running hooks, skip, spill (HOOK-SPILL-INTERIM-1) or start the CLI: at any concurrency they
append one job to the hook queue and return; the queue's depth cap and the consumer are covered in
test_hook_consumer.py and test_hook_write_queue.py.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[2] / "smartmemory_app" / "hooks"
PAYLOAD = b'{"session_id": "cap-test", "prompt": "a long enough prompt for recall"}'

# (id, script, env, running_procs, load1, expect): expect is "skip" (recall shed under load),
# "ran" (recall reached smartmemory) or "queued" (one hook-queue job, no process started).
_CASES = (
    ("observe-was-over-cap", "observe.sh", {}, 9, "1.0", "queued"),
    ("observe-under-old-cap", "observe.sh", {}, 2, "1.0", "queued"),
    (
        "observe-old-cap-env-ignored",
        "observe.sh",
        {"SMARTMEMORY_OBSERVE_MAX": "1"},
        4,
        "1.0",
        "queued",
    ),
    (
        "observe-old-spill-env-ignored",
        "observe.sh",
        {"SMARTMEMORY_HOOK_SPILL": "0"},
        9,
        "1.0",
        "queued",
    ),
    ("learn-was-at-cap", "learn.sh", {}, 2, "1.0", "queued"),
    ("distill-was-at-cap", "distill.sh", {}, 2, "1.0", "queued"),
    ("recall-load-over", "recall.sh", {}, 0, "75.5", "skip"),
    ("recall-load-at-max", "recall.sh", {}, 0, "48.0", "ran"),
    ("recall-load-under", "recall.sh", {}, 0, "12.3", "ran"),
    (
        "recall-custom-max",
        "recall.sh",
        {"SMARTMEMORY_HOOK_LOAD_MAX": "100"},
        0,
        "75.5",
        "ran",
    ),
    ("recall-off", "recall.sh", {"SMARTMEMORY_HOOK_LOAD_MAX": "0"}, 0, "999.0", "ran"),
)


def _fakes(bin_dir: Path, marker: Path) -> None:
    bin_dir.mkdir()
    scripts = {
        "smartmemory": f'#!/bin/sh\ncat >/dev/null\necho "$@" >>"{marker}"\necho \'{{"fake":"recall"}}\'\n',
        "pgrep": '#!/bin/sh\ni=0\nwhile [ "$i" -lt "${FAKE_PGREP_N:-0}" ]; do echo $((1000+i)); i=$((i+1)); done\n[ "${FAKE_PGREP_N:-0}" -gt 0 ]\n',
        "sysctl": '#!/bin/sh\necho "{ ${FAKE_LOAD1:-1.0} 1.0 1.0 }"\n',
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)


@pytest.mark.parametrize(
    "script,env,running,load1,expect",
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_hook_cap_table(tmp_path, script, env, running, load1, expect):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "ran.txt"
    _fakes(tmp_path / "bin", marker)
    run_env = {
        "HOME": str(home),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "FAKE_PGREP_N": str(running),
        "FAKE_LOAD1": load1,
        **env,
    }
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script)],
        input=PAYLOAD,
        env=run_env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    data = home / ".smartmemory"
    log = data / "hooks.log"
    log_text = log.read_text() if log.exists() else ""
    cap_lines = [ln for ln in log_text.splitlines() if "HOOK-LOAD-CAP-1" in ln]
    if expect == "skip":
        assert result.stdout == b""
        assert len(cap_lines) == 1, log_text
        assert " skipped" in cap_lines[0]
        time.sleep(0.3)  # a background run would have written the marker by now
        assert not marker.exists()
    elif expect == "ran":
        assert cap_lines == []
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "hook did not reach smartmemory"
        assert b'"fake":"recall"' in result.stdout
    else:  # queued: the payload is one hook-queue job, nothing skipped, spilled or started
        assert result.stdout == b""
        assert log_text == "", log_text
        records = sorted((data / "hook-queue" / "queue").glob("*.json"))
        assert len(records) == 1, records
        record = json.loads(records[0].read_text())
        assert record["phase"] == script[:-3]
        assert json.loads(Path(record["payload_path"]).read_bytes()) == json.loads(
            PAYLOAD
        )
        assert not (data / "hook-spill").exists()
        time.sleep(0.3)  # a detached CLI start would have written the marker by now
        assert not marker.exists()


@pytest.mark.parametrize(
    "name", ["observe", "learn", "distill", "recall", "orient", "persist"]
)
def test_template_parses(name):
    subprocess.run(["bash", "-n", str(HOOKS_DIR / f"{name}.sh")], check=True)
