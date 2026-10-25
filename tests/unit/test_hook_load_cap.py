"""HOOK-LOAD-CAP-1: concurrency caps (observe/learn/distill) and load skip (recall) in the hook templates.

One row per case. `smartmemory`, `pgrep` and `sysctl` are fakes on a temp PATH and HOME is a temp dir,
so nothing here touches the real ~/.smartmemory or a live process table.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parents[2] / "smartmemory_app" / "hooks"
PAYLOAD = b'{"session_id": "cap-test", "prompt": "a long enough prompt for recall"}'

# (id, script, env, running_procs, load1, expect_skip)
_CASES = (
    ("observe-at-cap", "observe.sh", {}, 3, "1.0", True),
    ("observe-over-cap", "observe.sh", {}, 9, "1.0", True),
    ("observe-under-cap", "observe.sh", {}, 2, "1.0", False),
    ("observe-custom-cap", "observe.sh", {"SMARTMEMORY_OBSERVE_MAX": "5"}, 4, "1.0", False),
    ("observe-at-cap-spill-off", "observe.sh", {"SMARTMEMORY_HOOK_SPILL": "0"}, 3, "1.0", True),
    ("observe-off", "observe.sh", {"SMARTMEMORY_OBSERVE_MAX": "0"}, 99, "1.0", False),
    ("learn-at-cap", "learn.sh", {}, 2, "1.0", True),
    ("learn-under-cap", "learn.sh", {}, 1, "1.0", False),
    ("learn-off", "learn.sh", {"SMARTMEMORY_LEARN_MAX": "0"}, 99, "1.0", False),
    ("distill-at-cap", "distill.sh", {}, 2, "1.0", True),
    ("distill-under-cap", "distill.sh", {}, 1, "1.0", False),
    ("distill-off", "distill.sh", {"SMARTMEMORY_DISTILL_MAX": "0"}, 99, "1.0", False),
    ("recall-load-over", "recall.sh", {}, 0, "75.5", True),
    ("recall-load-at-max", "recall.sh", {}, 0, "48.0", False),
    ("recall-load-under", "recall.sh", {}, 0, "12.3", False),
    ("recall-custom-max", "recall.sh", {"SMARTMEMORY_HOOK_LOAD_MAX": "100"}, 0, "75.5", False),
    ("recall-off", "recall.sh", {"SMARTMEMORY_HOOK_LOAD_MAX": "0"}, 0, "999.0", False),
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
    "script,env,running,load1,expect_skip",
    [c[1:] for c in _CASES],
    ids=[c[0] for c in _CASES],
)
def test_hook_cap_table(tmp_path, script, env, running, load1, expect_skip):
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
    log = home / ".smartmemory" / "hooks.log"
    log_text = log.read_text() if log.exists() else ""
    cap_lines = [ln for ln in log_text.splitlines() if "HOOK-LOAD-CAP-1" in ln]
    if expect_skip:
        assert result.stdout == b""
        spill = sorted((home / ".smartmemory" / "hook-spill").glob("*.json"))
        if script == "recall.sh" or env.get("SMARTMEMORY_HOOK_SPILL") == "0":  # recall never spills; HOOK-SPILL-INTERIM-1 kill switch = old discard
            assert len(cap_lines) == 1, log_text
            assert " skipped" in cap_lines[0]
            assert spill == []
        else:  # over-cap payload is spilled for the drain, not discarded, and nothing logs "skipped"
            assert cap_lines == [], log_text
            assert len(spill) == 1
            assert json.loads(spill[0].read_text())["payload"] == json.loads(PAYLOAD)
        time.sleep(0.3)  # background runs would have written the marker by now
        assert not marker.exists()
    else:
        assert cap_lines == []
        for _ in range(50):  # observe/learn/distill run detached
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "hook did not reach smartmemory"
        if script == "recall.sh":
            assert b'"fake":"recall"' in result.stdout
        else:
            assert result.stdout == b""


# HOOK-SPILL-INTERIM-1: (id, script, env, runs, payload, files_left, log_fragment)
_SPILL_CASES = (
    ("observe-three-in-order", "observe.sh", {}, 3, PAYLOAD, 3, None),
    ("learn-file-cap", "learn.sh", {"SMARTMEMORY_HOOK_SPILL_MAX_FILES": "2"}, 3, PAYLOAD, 2, "spill full: dropped (files=2"),
    ("distill-byte-cap", "distill.sh", {"SMARTMEMORY_HOOK_SPILL_MAX_BYTES": "100"}, 3, PAYLOAD, 1, "spill full: dropped"),
    ("observe-not-json", "observe.sh", {}, 1, b'not json "{', 1, None),
    ("observe-empty", "observe.sh", {}, 1, b"", 1, None),
)


@pytest.mark.parametrize(
    "script,env,runs,payload,files_left,log_fragment",
    [c[1:] for c in _SPILL_CASES],
    ids=[c[0] for c in _SPILL_CASES],
)
def test_spill_table(tmp_path, script, env, runs, payload, files_left, log_fragment):
    home = tmp_path / "home"
    home.mkdir()
    _fakes(tmp_path / "bin", tmp_path / "ran.txt")
    run_env = {
        "HOME": str(home),
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "FAKE_PGREP_N": "99",
        **env,
    }
    for _ in range(runs):
        result = subprocess.run(["bash", str(HOOKS_DIR / script)], input=payload, env=run_env, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr.decode()
    spill = home / ".smartmemory" / "hook-spill"
    names = sorted(p.name for p in spill.iterdir())
    assert len(names) == files_left, names
    assert all(n.endswith(f"-{script[:-3]}.json") for n in names), names  # no .tmp-* leftovers, sortable names
    docs = [json.loads((spill / n).read_text()) for n in names]
    assert [d["v"] for d in docs] == [1] * files_left
    assert {d["phase"] for d in docs} == {script[:-3]}
    log = home / ".smartmemory" / "hooks.log"
    log_text = log.read_text() if log.exists() else ""
    assert " skipped" not in log_text
    if log_fragment:
        assert log_fragment in log_text, log_text
    else:
        assert "HOOK-SPILL-INTERIM-1" not in log_text, log_text


@pytest.mark.parametrize("name", ["observe", "learn", "distill", "recall", "orient", "persist"])
def test_template_parses(name):
    subprocess.run(["bash", "-n", str(HOOKS_DIR / f"{name}.sh")], check=True)
