"""Pytest configuration for smartmemory tests."""

import os
from pathlib import Path

import pytest

from tests import _host_isolation as host_isolation

# Never send automatic reports during tests, including import/collection failures.
os.environ["SMARTMEMORY_CRASH_REPORTS"] = "0"
# Suites set SMARTMEMORY_* on purpose: no inherited-variable prompt or warning
# (CLI-ENV-DETECT-1). test_env_pin.py removes this to test the check itself.
os.environ["SMARTMEMORY_ENV_CHECK"] = "0"
# Keep existing model files available without restoring access to the user's HOME.
_REAL_HF_HOME = os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))


def pytest_collection_modifyitems(items):
    """Auto-mark tests in tests/integration/ with the integration marker."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# CLI-TEST-ENV-LEAK-1: provider/model/key variables. A test that leaves one changed
# makes every later test read it (a leaked LLM_PROVIDER=groq failed 7 doctor rows).
_PROVIDER_ENV_PREFIXES = (
    "LLM_",
    "SMARTMEMORY_",
    "ANTHROPIC_",
    "OPENAI_",
    "GROQ_",
    "GEMINI_",
    "DEEPSEEK_",
)
_PROVIDER_ENV_SUFFIXES = ("_API_KEY", "_BASE_URL")


def _provider_env_names(names):
    return {
        n
        for n in names
        if n.startswith(_PROVIDER_ENV_PREFIXES) or n.endswith(_PROVIDER_ENV_SUFFIXES)
    }


def _provider_env_snapshot():
    return {n: os.environ[n] for n in _provider_env_names(list(os.environ))}


def assert_provider_env_unchanged(before):
    """Undo any provider env change since `before`, then fail naming the variables."""
    after = _provider_env_snapshot()
    leaked = sorted(
        n for n in before.keys() | after.keys() if before.get(n) != after.get(n)
    )
    for name in leaked:
        if name in before:
            os.environ[name] = before[name]
        else:
            os.environ.pop(name, None)
    assert not leaked, (
        "Test leaked provider env vars (use monkeypatch.setenv/delenv or "
        "mock.patch.dict(os.environ)): " + ", ".join(leaked)
    )


@pytest.fixture(autouse=True)
def provider_env_leak_guard():
    """Fail the test that leaves a provider env var changed, then undo the change.

    Names only, never values. The fixtures that use monkeypatch or restore
    SMARTMEMORY_* request this one, so it is set up first and its check runs
    after their teardown.
    """
    before = _provider_env_snapshot()
    yield
    assert_provider_env_unchanged(before)


@pytest.fixture(autouse=True)
def capture_rss_guard_off(provider_env_leak_guard, monkeypatch):
    """A pytest process can be over the worker's RSS default, so in-process worker runs
    would recycle. Tests of the guard set CAPTURE_MAX_RSS_MB themselves. The spawn cap
    counts real capture workers on this machine, so it is off unless a test sets it."""
    monkeypatch.setenv("CAPTURE_MAX_RSS_MB", "1000000")
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_MAX", "0")
    # CAPTURE-HOLD-ALL-1: the hold ships ON, so capture tests opt out. Hold tests set it.
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD", "0")
    monkeypatch.setenv("SMARTMEMORY_CAPTURE_HOLD_OVER_MB", "0")


@pytest.fixture
def provider_pin_isolated(monkeypatch):
    """Undo the provider pin that capture_lessons/_memory write straight to os.environ.

    CLI-TEST-ENV-LEAK-1: session_lessons and capture_worker set LLM_PROVIDER,
    LLM_API_KEY, OPENAI_BASE_URL and SMARTMEMORY_LLM_MODEL themselves, so a test
    that calls them must have monkeypatch own those names first. setenv before
    delenv, because delenv on an absent name records nothing to restore.
    """
    for name in (
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "OPENAI_BASE_URL",
        "SMARTMEMORY_LLM_MODEL",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def isolated_support_diagnostics(provider_env_leak_guard, monkeypatch, tmp_path):
    """Never write user logs or make doctor probes against a live network."""
    if not os.environ.get("HF_HOME"):
        monkeypatch.setenv("HF_HOME", _REAL_HF_HOME)
    if not os.environ.get("HF_HUB_CACHE"):
        monkeypatch.setenv("HF_HUB_CACHE", str(Path(os.environ["HF_HOME"]) / "hub"))
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # No login keychain exists under a redirected HOME: a real keyring write would pop a
    # macOS "Keychain Not Found" dialog (CLI-KEYRING-NULL-GUARD-1). Tests that exercise the
    # write path patch keyring.set_password and use the keyring_writes_enabled fixture.
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setenv("APPDATA", str(home / "appdata"))
    from smartmemory_app import (
        bug_report,
        support_diagnostics,
        install_check,
        diagnostic_process,
        crash_reporter,
        hook_failures,
    )

    monkeypatch.setattr(
        bug_report, "debug_log_path", lambda: tmp_path / "cli-debug.log"
    )
    monkeypatch.setattr(
        crash_reporter, "debug_log_path", lambda: tmp_path / "cli-debug.log"
    )
    monkeypatch.setattr(
        hook_failures, "marker_directory", lambda: tmp_path / "hook-markers"
    )
    monkeypatch.setattr(
        support_diagnostics, "_probe", lambda host: "reachable (1 ms, HTTP 200)"
    )
    monkeypatch.setattr(
        support_diagnostics, "_daemon_status", lambda: "Daemon: stopped"
    )

    # These CLI tests isolate diagnostics unrelated to their command. Dedicated
    # install/store suites and native TVPC drivers exercise the real probes.
    monkeypatch.setattr(
        install_check, "run_native_checks", lambda: ["Native libraries: OK"]
    )
    monkeypatch.setattr(diagnostic_process, "offline_checks", lambda task, **kwargs: [])
    # SETUP-DOCTOR-1: the setup/tour/doctor table probes the daemon, keychain and
    # hosted API. tests/unit/test_setup_doctor.py drives it with injected fakes.
    from smartmemory_app import doctor

    monkeypatch.setattr(doctor, "run_checks", lambda context="doctor": [])
    # SETUP-RESUMABLE-1: every setup step is checked against its real effect (model
    # files, daemon health, a save-and-recall round trip). Suites that fake the
    # install steps get an all-pass check here. tests/unit/test_setup_resumable.py
    # restores the real one (setup_verify._verify_step) for the checks it tests.
    from smartmemory_app import setup_verify

    monkeypatch.setattr(
        setup_verify,
        "verify_step",
        lambda key, choices: setup_verify.Check(True, f"{key} checked (test stub)"),
    )

    # Detached-entry tests also install global handlers/hooks. Restore them per test.
    import logging
    import sys
    import threading

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    states = [(handler, handler.level, handler.formatter) for handler in handlers]
    process_hook, thread_hook = sys.excepthook, threading.excepthook
    stdout, stderr = sys.stdout, sys.stderr
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for handler, handler_level, formatter in states:
        handler.setLevel(handler_level)
        handler.setFormatter(formatter)
    sys.excepthook, threading.excepthook = process_hook, thread_hook
    sys.stdout, sys.stderr = stdout, stderr


@pytest.fixture(autouse=True)
def restore_smartmemory_env(provider_env_leak_guard):
    """Undo SMARTMEMORY_* changes a test's code made straight to os.environ.

    env_pin (CLI-ENV-DETECT-1) applies a project's ignore/override decisions to
    os.environ once per process. monkeypatch cannot undo a write it did not make,
    so an override applied in one test leaked into every later test.
    """
    from smartmemory_app import env_pin, env_sources

    before = {k: v for k, v in os.environ.items() if k.startswith("SMARTMEMORY_")}
    state = (env_pin._applied, env_sources._pre_pin_names)
    yield
    for name in [k for k in os.environ if k.startswith("SMARTMEMORY_")]:
        if name not in before:
            del os.environ[name]
    os.environ.update(before)
    env_pin._applied, env_sources._pre_pin_names = state


@pytest.fixture(scope="session", autouse=True)
def cli_test_port():
    """One port per pytest process, registered in ~/.ports as cliTest (CLI-TEST-HOST-ISOLATION-1).

    Released on teardown whether the session passed or failed, and a listener still
    on it is stopped and reported.
    """
    port, registered = host_isolation.reserve_test_port()
    try:
        yield port
    finally:
        problems = host_isolation.release_test_port(port, registered)
    assert not problems, "; ".join(problems)


@pytest.fixture(autouse=True)
def host_isolation_guard(
    isolated_support_diagnostics, monkeypatch, tmp_path_factory, cli_test_port
):
    """No test reaches the owner's real daemon port, HOME or launchd job.

    Port: SMARTMEMORY_DAEMON_PORT is the reserved test port (config, the daemon and
    the viewer all honour it), and a bind to 9014/9015 fails the test.
    launchd: the daemon module's job labels are scratch names, and a launchctl shim is
    first on PATH, so a call from the test or from any process it spawns is logged,
    answered "no such service", and fails the test unless it only prints or boots out
    a scratch label.
    The setup module's import-time paths are re-pointed at the isolated HOME.
    Processes the test started are stopped at teardown.
    """
    import socket
    import subprocess
    import uuid

    from smartmemory_app import daemon as daemon_mod
    from smartmemory_app import setup as setup_mod

    home = Path(os.environ["HOME"])
    # A scratch job name: the daemon's stop/start paths ask launchd about these labels
    # whatever HOME is, so the real ai.smartmemory.daemon/worker must never reach it.
    token = uuid.uuid4().hex[:8]
    monkeypatch.setattr(
        daemon_mod,
        "_LAUNCHD_DAEMON_LABEL",
        f"{host_isolation.TEST_LABEL_PREFIX}{token}.daemon",
    )
    monkeypatch.setattr(
        daemon_mod,
        "_LAUNCHD_WORKER_LABEL",
        f"{host_isolation.TEST_LABEL_PREFIX}{token}.worker",
    )
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(cli_test_port))
    monkeypatch.setattr(setup_mod, "DATA_DIR", home / ".smartmemory")
    monkeypatch.setattr(setup_mod, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(
        setup_mod, "LAUNCH_AGENTS_DIR", home / "Library" / "LaunchAgents"
    )

    # Beside tmp_path, not in it: tests list and assert on their own tmp_path.
    guard_dir = tmp_path_factory.mktemp("host-isolation")
    launchctl_log = guard_dir / "launchctl-calls.log"
    shim = host_isolation.install_launchctl_shim(
        guard_dir / "bin",
        launchctl_log,
        loaded=os.environ.get("SM_TEST_LAUNCHCTL_LOADED") == "1",
    )
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    binds: list[str] = []
    spawned: list = []
    monkeypatch.setattr(socket.socket, "bind", host_isolation.bind_guard(binds))
    monkeypatch.setattr(
        subprocess.Popen, "__init__", host_isolation.tracking_popen_init(spawned, shim)
    )
    try:
        yield host_isolation.HostGuard(cli_test_port, launchctl_log, binds, shim)
    finally:
        host_isolation.stop_spawned(spawned)
    calls = host_isolation.real_launchctl_calls(launchctl_log)
    assert not calls, "test called launchctl (real launchd job): " + "; ".join(calls)
    assert not binds, "test bound the owner's real daemon port: " + "; ".join(binds)


@pytest.fixture(scope="session", autouse=True)
def real_home_report_leak_gate():
    """Fail if a CLI or shell test adds reporting content to the real profile."""
    root = Path.home() / ".smartmemory"

    def snapshot():
        paths = [root / "hook-failures.tsv"]
        paths.extend((root / "report-outbox").rglob("*"))
        return {
            str(p): p.read_bytes() for p in paths if p.is_file() and p.stat().st_size
        }

    before = snapshot()
    yield
    gained = [p for p, content in snapshot().items() if before.get(p) != content]
    assert not gained, (
        "Tests leaked reporting content into the real home: " + ", ".join(gained)
    )


@pytest.fixture
def keyring_writes_enabled(monkeypatch):
    """Let setup's keyring write sites reach a patched ``keyring.set_password``."""
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)
    monkeypatch.delenv("SMARTMEMORY_NO_KEYRING", raising=False)
