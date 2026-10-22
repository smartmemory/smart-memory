"""CLI-HYGIENE-K-1: doctor and status wording that cried wolf for a beta tester.

Each table has one row per member of the class it guards.
"""

from __future__ import annotations

import pytest

from smartmemory_app import doctor
from smartmemory_app.doctor import FAIL, PASS, WARN, Context


# ── A. doctor "not in a virtual environment" on conda ───────────────────────


def _make_prefix(tmp_path, kind: str):
    """A prefix directory shaped like the named Python install."""
    root = tmp_path / kind
    root.mkdir(parents=True)
    if kind == "conda-env":  # conda create -n demo2 (conda-meta, no envs/ inside)
        (root / "conda-meta").mkdir()
    elif kind == "conda-base":  # the root install holds envs/ and the front end
        (root / "conda-meta").mkdir()
        (root / "envs").mkdir()
    elif kind == "conda-base-condabin-only":
        (root / "conda-meta").mkdir()
        (root / "condabin").mkdir()
    return str(root)


# (id, prefix kind, same prefix as base_prefix, python, expected status, text)
PYTHON_ROWS = [
    # The tester's repro: conda env, sys.prefix == sys.base_prefix.
    ("conda-env", "conda-env", True, (3, 11, 17), PASS, "conda env"),
    ("conda-base", "conda-base", True, (3, 11, 17), WARN, "shared conda base"),
    ("conda-base-condabin", "conda-base-condabin-only", True, (3, 11, 17), WARN, "shared conda base"),
    # Pre-existing arms stay as they were.
    ("system-python", "system", True, (3, 11, 17), WARN, "not in a virtual environment"),
    ("venv", "venv", False, (3, 11, 17), PASS, "venv"),
    # A conda env on a Python SmartMemory cannot run is still a FAIL.
    ("conda-env-old-python", "conda-env", True, (3, 10, 4), FAIL, "needs 3.11"),
]


@pytest.mark.parametrize("row", PYTHON_ROWS, ids=[r[0] for r in PYTHON_ROWS])
def test_python_row_knows_conda(row, tmp_path):
    _id, kind, same_base, version, status, text = row
    prefix = _make_prefix(tmp_path, kind)
    ctx = Context(
        python_version=version,
        prefix=prefix,
        base_prefix=prefix if same_base else str(tmp_path / "base-python"),
        executable=f"{prefix}/bin/python3.11",
    )
    result = doctor.check_python(ctx)
    assert result.status == status, result
    assert text in result.detail


# ── C. "warming" says the daemon is unusable while it serves adds ───────────

WARMING_HEALTH = {"service": "smartmemory", "status": "warming", "mode": "lite", "pid": 7}


def _diagnosis_fix(tmp_path) -> str:
    from smartmemory_app.daemon_diagnosis import DaemonFacts, diagnose

    facts = DaemonFacts(
        port=9014,
        log_path=tmp_path / "daemon.log",
        config_path=tmp_path / "config.toml",
        health=WARMING_HEALTH,
    )
    return diagnose(facts).fix or ""


def _doctor_row_fix(tmp_path) -> str:
    ctx = Context(
        daemon_status=lambda: WARMING_HEALTH,
        config_path=lambda: tmp_path / "config.toml",
        load_config=lambda: doctor._default_load_config(),
    )
    return doctor._check_daemon(ctx).fix


# One row per surface that prints the warming state. `sm status` and `sm start`
# are asserted in tests/cli/test_local_status.py and tests/unit/test_cli_ux.py.
WARMING_SURFACES = [
    ("diagnosis", _diagnosis_fix),
    ("doctor-row", _doctor_row_fix),
]


@pytest.mark.parametrize("surface", WARMING_SURFACES, ids=[s[0] for s in WARMING_SURFACES])
def test_warming_text_says_adds_and_searches_work(surface, tmp_path, monkeypatch):
    from smartmemory_app.daemon_diagnosis import WARMING_NOTE

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert WARMING_NOTE in surface[1](tmp_path)
