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
    for name in ("OUTPUT", "ARGS", "DONE"):
        monkeypatch.setenv(f"STUB_{name}", str(tmp_path / name.lower()))
    return tmp_path


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "Background hook did not finish"
        time.sleep(0.02)


@pytest.mark.parametrize("script", ASYNC_HOOKS)
@pytest.mark.parametrize("size", [32, 200_000])
def test_async_hook_preserves_exact_payload(script, size, hook_stub):
    payload = (json.dumps({"tool_response": "x" * size}) + "\n\n").encode()
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script)],
        input=payload,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    wait_for(lambda: (hook_stub / "done").exists())
    assert (hook_stub / "output").read_bytes() == payload
    assert (hook_stub / "args").read_text() == f"lifecycle {Path(script).stem}\n"
    assert "stub diagnostic" in (hook_stub / "data/hooks.log").read_text()
    wait_for(lambda: not list((hook_stub / "data/tmp").glob("*")))


@pytest.mark.parametrize("script", ASYNC_HOOKS)
def test_async_hook_returns_before_slow_failing_cli(script, hook_stub, monkeypatch):
    monkeypatch.setenv("STUB_DELAY", "3")
    monkeypatch.setenv("STUB_EXIT", "1")
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script)],
        input=b'{"session_id": "slow"}\n',
        capture_output=True,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    try:
        assert result.returncode == 0
        assert elapsed < 2
        assert not (hook_stub / "done").exists()
    finally:
        wait_for(lambda: (hook_stub / "done").exists())
        wait_for(lambda: not list((hook_stub / "data/tmp").glob("*")))


@pytest.mark.parametrize("script", ["orient.sh", "recall.sh", "persist.sh"])
def test_foreground_hooks_preserve_payload(script, hook_stub):
    payload = b'{"session_id": "foreground"}\n\n'
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / script)],
        input=payload,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert (hook_stub / "done").exists()
    assert (hook_stub / "output").read_bytes() == payload
