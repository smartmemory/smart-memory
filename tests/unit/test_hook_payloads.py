"""Exercise the actual shell hooks with a local lifecycle executable."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[2] / "smartmemory_app" / "hooks"
ASYNC_HOOKS = sorted(
    path.name
    for path in HOOKS_DIR.glob("*.sh")
    if re.search(r"(?<!&)&\s*$", path.read_text(), re.MULTILINE)
)
BASHES = [Path("/bin/bash")]
HOMEBREW_BASH = Path("/opt/homebrew/opt/bash/bin/bash")
if HOMEBREW_BASH.is_file():
    BASHES.append(HOMEBREW_BASH)


@pytest.fixture
def hook_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "smartmemory"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'sleep "${STUB_DELAY:-0}"\n'
        'cat > "$STUB_OUTPUT"\n'
        'printf "%s\\n" "$*" > "$STUB_ARGS"\n'
        'echo "stub diagnostic" >&2\n'
        'touch "$STUB_DONE"\n'
        'exit "${STUB_EXIT:-0}"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    # Failing stubs make hooks write failure markers under $HOME; keep them out of the real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for name in ("OUTPUT", "ARGS", "DONE"):
        monkeypatch.setenv(f"STUB_{name}", str(tmp_path / name.lower()))
    return tmp_path


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "Background hook did not finish"
        time.sleep(0.02)


def queued(hook_stub, script):
    """The one queue record a write hook published, and its payload bytes (HOOK-WRITE-QUEUE-1)."""
    queue = hook_stub / "data/hook-queue"
    (record_path,) = (queue / "queue").glob(f"*.{Path(script).stem}.json")
    record = json.loads(record_path.read_text())
    return record, Path(record["payload_path"]).read_bytes()


@pytest.mark.parametrize("script", ASYNC_HOOKS)
@pytest.mark.parametrize("size", [32, 200_000])
@pytest.mark.parametrize("bash", BASHES)
def test_async_hook_preserves_exact_payload(script, size, bash, hook_stub):
    payload = (json.dumps({"tool_response": "x" * size}) + "\n\n").encode()
    result = subprocess.run(
        [str(bash), str(HOOKS_DIR / script)],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    record, queued_bytes = queued(hook_stub, script)
    assert queued_bytes == payload
    assert record["phase"] == Path(script).stem
    wait_for(lambda: (hook_stub / "done").exists())
    queue = hook_stub / "data/hook-queue"
    assert (hook_stub / "args").read_text() == f"lifecycle drain-hooks --dir {queue}\n"
    assert (
        hook_stub / "output"
    ).read_bytes() == b""  # the drainer reads the queue, not stdin
    assert "stub diagnostic" in (hook_stub / "data/hooks.log").read_text()
    assert not list((queue / "queue").glob(".*.tmp"))


@pytest.mark.parametrize("script", ASYNC_HOOKS)
@pytest.mark.parametrize("bash", BASHES)
def test_async_hook_returns_before_slow_failing_cli(
    script, bash, hook_stub, monkeypatch
):
    monkeypatch.setenv("STUB_DELAY", "3")
    monkeypatch.setenv("STUB_EXIT", "1")
    started = time.monotonic()
    result = subprocess.run(
        [str(bash), str(HOOKS_DIR / script)],
        input=b'{"session_id": "slow"}\n',
        capture_output=True,
        timeout=10,
        check=False,
    )
    elapsed = time.monotonic() - started
    try:
        assert result.returncode == 0
        assert elapsed < 2
        assert not (hook_stub / "done").exists()
        assert queued(hook_stub, script)[0]["session_id"] == "slow"
    finally:
        wait_for(lambda: (hook_stub / "done").exists())
    failures = hook_stub / "home/.smartmemory/hook-failures.tsv"
    wait_for(failures.exists)
    assert failures.read_text().startswith(f"{Path(script).stem}\t1\t")


@pytest.mark.parametrize("script", ASYNC_HOOKS)
@pytest.mark.parametrize("bash", BASHES)
def test_async_hook_keeps_payloads_only_in_the_queue(script, bash, hook_stub):
    """The write hooks no longer stage payloads in data/tmp; they write only the queue and hooks.log."""
    result = subprocess.run(
        [str(bash), str(HOOKS_DIR / script)],
        input=b'{"session_id": "where"}\n',
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    wait_for(lambda: (hook_stub / "done").exists())
    written = {
        path.relative_to(hook_stub / "data").parts[0]
        for path in (hook_stub / "data").rglob("*")
        if path.is_file()
    }
    assert written <= {"hook-queue", "hooks.log"}
    assert not (hook_stub / "data/tmp").exists()


@pytest.mark.parametrize("script", ["orient.sh", "recall.sh", "persist.sh"])
def test_foreground_hooks_preserve_payload(script, hook_stub):
    payload = b'{"session_id": "foreground"}\n\n'
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script)],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert (hook_stub / "done").exists()
    assert (hook_stub / "output").read_bytes() == payload
