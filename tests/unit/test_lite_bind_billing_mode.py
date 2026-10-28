"""Every Lite process entry binds billing mode ``local`` (WRAPPER-2.0-LITE-BIND-BILLING-MODE-1).

Core resolves an unset ``SMARTMEMORY_BILLING_MODE`` as ``enforce``, so a Lite process that never
bound ``local`` would refuse every platform-keyed call once the meter reads the mode. Each row
runs one real entry up to its first real work, then asks core's ``resolve_billing_mode()``.
Removing the bind call from one entry turns exactly that row red.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from smartmemory.billing.mode import BILLING_MODE_ENV, BillingMode, resolve_billing_mode

import smartmemory_app
from smartmemory_app import capture_worker, cli, viewer_server, worker_entry

ROOT = Path(smartmemory_app.__file__).resolve().parent


class _Stop(Exception):
    """Raised by a stub to end an entry right after its bind, before any real work."""


def _stop(*_args, **_kwargs):
    raise _Stop


def _run_cli(mp):
    mp.setattr(cli, "cli", _stop)
    cli.main()


def _run_viewer(mp):
    import smartmemory_app.storage as storage

    mp.setattr(storage, "_resolve_data_dir", _stop)
    viewer_server.main(port=1, open_browser=False)


def _run_worker(mp):
    mp.setattr(sys, "argv", ["worker_entry"])
    mp.setattr(worker_entry, "_resolve_data_dir", _stop)
    worker_entry.main()


def _run_capture(mp):
    mp.setattr(capture_worker, "run", _stop)
    capture_worker.main()


def _run_dunder_main(mp):
    import smartmemory_app.__main__ as dunder

    mp.setattr(cli, "cli", _stop)
    dunder.main()


# entry | runner
ENTRIES = [
    ("cli.main (console scripts smartmemory, sm)", _run_cli),
    ("python -m smartmemory_app (__main__)", _run_dunder_main),
    ("viewer_server.main (daemon, launchd, tour)", _run_viewer),
    ("worker_entry.main (worker, launchd)", _run_worker),
    ("capture_worker.main (-m capture_worker)", _run_capture),
]

# env before | expected mode after
ENV_ROWS = [
    ("unset", None, BillingMode.LOCAL),
    ("hosted enforce stays enforce", "enforce", BillingMode.ENFORCE),
    ("explicit local stays local", "local", BillingMode.LOCAL),
]


@pytest.mark.parametrize("entry,runner", ENTRIES, ids=[e[0] for e in ENTRIES])
def test_entry_binds_local(monkeypatch, entry, runner):
    monkeypatch.delenv(BILLING_MODE_ENV, raising=False)
    assert resolve_billing_mode() is BillingMode.ENFORCE, "precondition: unset resolves enforce"
    with pytest.raises(_Stop):
        runner(monkeypatch)
    assert resolve_billing_mode() is BillingMode.LOCAL, entry


@pytest.mark.parametrize("label,before,expected", ENV_ROWS, ids=[r[0] for r in ENV_ROWS])
def test_operator_choice_wins_at_every_entry(monkeypatch, label, before, expected):
    for entry, runner in ENTRIES:
        with monkeypatch.context() as mp:
            if before is None:
                mp.delenv(BILLING_MODE_ENV, raising=False)
            else:
                mp.setenv(BILLING_MODE_ENV, before)
            with pytest.raises(_Stop):
                runner(mp)
            assert resolve_billing_mode() is expected, (label, entry)


def test_spawned_child_interpreter_binds(tmp_path):
    """A fresh interpreter started the way the daemon and capture queue start one binds on its own."""
    env = {k: v for k, v in os.environ.items() if k != BILLING_MODE_ENV}
    env.update(HOME=str(tmp_path), SMARTMEMORY_DATA_DIR=str(tmp_path / "data"), SMARTMEMORY_CRASH_REPORTS="0")
    code = (
        "import smartmemory_app.capture_worker as c\n"
        "from smartmemory.billing.mode import resolve_billing_mode\n"
        "c.run = lambda: print('MODE=' + resolve_billing_mode().value)\n"
        "c.main()\n"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert "MODE=local" in out.stdout, out.stdout + out.stderr


# Entries that start a fresh interpreter and do NOT bind, each with the reason. A new entry that
# is in neither table fails the inventory row below.
NOT_BOUND = {
    "diagnostic_worker": "offline child of diagnostic_process, never resolves the mode or builds a store, env is inherited",
    "enrichment_worker": "retired, logs one warning and exits",
    "setup": "`python -m smartmemory_app.setup` runs the setup wizard, which spawns no memory; the CLI group binds",
}
BOUND = {"cli", "viewer_server", "worker_entry", "capture_worker"}


def test_entry_inventory_is_complete():
    """Every ``__main__`` guard, ``-m smartmemory_app.X`` spawn, ``-c "from smartmemory_app.X import main"``
    spawn, launchd module and console script names a module that is bound or listed with a reason."""
    found = set()
    for path in ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'^if __name__ == "__main__":', text, re.M):
            found.add(path.stem)
        for match in re.finditer(r'"-m",\s*"smartmemory_app\.(\w+)"|-m smartmemory_app\.(\w+)', text):
            found.add(match.group(1) or match.group(2))
        for match in re.finditer(r"from smartmemory_app\.(\w+) import main", text):
            found.add(match.group(1))
    found.discard("__main__")
    scripts = tomllib.loads((ROOT.parent / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts.values()) == {"smartmemory_app.cli:main"}
    unknown = found - BOUND - set(NOT_BOUND)
    assert not unknown, f"entries with no bind decision: {sorted(unknown)}"
    assert BOUND <= found, f"bound entry no longer found: {sorted(BOUND - found)}"
    for module in sorted(BOUND):
        body = (ROOT / f"{module}.py").read_text(encoding="utf-8")
        assert "bind_lite_billing_mode()" in body, module
