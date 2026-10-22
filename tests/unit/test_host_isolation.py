"""The CLI suite cannot reach the owner's real daemon port, HOME or launchd job.

CLI-TEST-HOST-ISOLATION-1. Incident 2026-10-22: a leftover viewer held 9014 and the owner's
``ai.smartmemory.daemon`` job was found unloaded. The guard itself is the autouse
``host_isolation_guard`` fixture (tests/conftest.py, tests/_host_isolation.py). These
tests prove each site that binds a port or calls launchctl is covered.
"""

import re
import socket
import subprocess
from pathlib import Path

import pytest

from smartmemory_app import config, daemon, setup
from tests import _host_isolation as hi

APP = Path(daemon.__file__).parent

# Step-1 table: the files in smartmemory_app that call launchctl (daemon_diagnosis only names a plist).
LAUNCHCTL_FILES = {"daemon.py", "setup.py", "daemon_diagnosis.py"}
REAL_PORT = 9014


def _guard_body_lines(path: Path):
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if "launchctl" in line and not line.lstrip().startswith(("#", '"""')):
            yield number, line


def test_launchctl_is_only_called_by_bare_name_from_the_known_files():
    """Source scan: no absolute launchctl path (it would skip the PATH shim) and no new caller."""
    found = {}
    for path in sorted(APP.rglob("*.py")):
        lines = list(_guard_body_lines(path))
        if lines:
            found[path.relative_to(APP).as_posix()] = lines
    assert set(found) <= LAUNCHCTL_FILES, (
        "new launchctl caller: add it to LAUNCHCTL_FILES and prove the guard covers it: "
        + ", ".join(sorted(set(found) - LAUNCHCTL_FILES))
    )
    absolute = [
        f"{name}:{n}"
        for name, lines in found.items()
        for n, line in lines
        if re.search(r"/(usr/)?bin/launchctl", line)
    ]
    assert not absolute, f"absolute launchctl path skips the test shim: {absolute}"


@pytest.mark.parametrize(
    ("call", "flagged"),
    [
        ("launchctl print gui/501/ai.smartmemory.daemon", True),
        ("launchctl print gui/501/ai.smartmemory.worker", True),
        ("launchctl bootout gui/501/ai.smartmemory.daemon", True),
        ("launchctl bootout gui/501/ai.smartmemory.worker", True),
        ("launchctl unload /x/ai.smartmemory.daemon.plist", True),
        ("launchctl load /x/ai.smartmemory.daemon.plist", True),
        ("launchctl bootstrap gui/501 /x/ai.smartmemory.daemon.plist", True),
        ("launchctl unload /x/ai.smartmemory.test.abc.daemon.plist", True),
        ("launchctl print gui/501/ai.smartmemory.test.abc.daemon", False),
        ("launchctl bootout gui/501/ai.smartmemory.test.abc.worker", False),
    ],
)
def test_only_scratch_label_print_and_bootout_are_allowed(tmp_path, call, flagged):
    log = tmp_path / "calls.log"
    log.write_text(call + "\n")
    assert bool(hi.real_launchctl_calls(log)) is flagged


def test_label_and_paths_are_redirected_for_every_test(host_isolation_guard):
    import os

    home = Path(os.environ["HOME"])
    assert daemon._LAUNCHD_DAEMON_LABEL.startswith(hi.TEST_LABEL_PREFIX)
    assert daemon._LAUNCHD_WORKER_LABEL.startswith(hi.TEST_LABEL_PREFIX)
    for name in ("DATA_DIR", "CLAUDE_DIR", "LAUNCH_AGENTS_DIR"):
        assert Path(getattr(setup, name)).is_relative_to(home), name
    assert daemon._launchd_plist_path("x").is_relative_to(home)


@pytest.mark.parametrize(
    "argv",
    [
        ["launchctl", "print", "gui/501/ai.smartmemory.daemon"],
        ["/bin/launchctl", "bootout", "gui/501/ai.smartmemory.worker"],
        ["/usr/bin/launchctl", "unload", "/x/ai.smartmemory.daemon.plist"],
    ],
)
@pytest.mark.parametrize("env", [None, {}], ids=["inherit-env", "empty-env"])
def test_a_real_label_call_reaches_the_shim_not_launchd(
    host_isolation_guard, argv, env
):
    result = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert result.returncode == 113
    assert "blocked by test host isolation" in result.stderr
    assert hi.real_launchctl_calls(host_isolation_guard.launchctl_log), (
        "call was not recorded"
    )
    host_isolation_guard.forgive()


def test_stop_daemon_with_a_loaded_job_only_touches_scratch_labels(
    host_isolation_guard, monkeypatch
):
    """The 2026-10-22 mechanism: stop_daemon asks launchd about its job labels whatever HOME
    is and boots out what is loaded. With the job reported loaded, only scratch labels are named."""
    monkeypatch.setattr(daemon, "_stop_workers", lambda: None)
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    loaded = {daemon._LAUNCHD_DAEMON_LABEL, daemon._LAUNCHD_WORKER_LABEL}
    real_loaded, real_bootout = daemon._launchd_loaded, daemon._launchd_bootout

    def loaded_until_booted_out(label):
        real_loaded(label)  # still asks (the shim) as the product does
        return label in loaded

    def booted_out(label):
        loaded.discard(label)
        return real_bootout(label)

    monkeypatch.setattr(daemon, "_launchd_loaded", loaded_until_booted_out)
    monkeypatch.setattr(daemon, "_launchd_bootout", booted_out)
    daemon.stop_daemon()
    calls = host_isolation_guard.launchctl_log.read_text().splitlines()
    assert any(
        c.startswith("launchctl bootout") and c.endswith(".daemon") for c in calls
    ), calls
    assert all(hi.TEST_LABEL_PREFIX in c for c in calls), calls
    assert hi.real_launchctl_calls(host_isolation_guard.launchctl_log) == []


# ── ports ────────────────────────────────────────────────────────────────────


def test_every_test_gets_a_reserved_port_never_9014(host_isolation_guard, monkeypatch):
    assert host_isolation_guard.port not in hi.REAL_DAEMON_PORTS
    assert config.load_config().daemon_port == host_isolation_guard.port
    assert daemon._port() == host_isolation_guard.port


@pytest.mark.parametrize("port", sorted(hi.REAL_DAEMON_PORTS))
@pytest.mark.parametrize(
    "host", ["127.0.0.1", "0.0.0.0", ""], ids=["loopback", "any", "empty"]
)
def test_binding_a_real_daemon_port_fails_and_is_recorded(
    host_isolation_guard, host, port
):
    with socket.socket() as sock, pytest.raises(hi.HostIsolationViolation):
        sock.bind((host, port))
    assert host_isolation_guard.binds
    host_isolation_guard.forgive()


def test_binding_a_scratch_port_still_works(host_isolation_guard):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        assert sock.getsockname()[1] not in hi.REAL_DAEMON_PORTS
    assert not host_isolation_guard.binds


def test_viewer_main_without_a_port_uses_the_configured_one_not_9014(
    host_isolation_guard, monkeypatch, tmp_path
):
    """The bind site: viewer_server.main(port=None) served 9014 whatever the env said."""
    from smartmemory_app import viewer_server

    monkeypatch.setattr("smartmemory_app.storage._resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(viewer_server, "_start_background_warmup", lambda: None)
    monkeypatch.setattr(viewer_server, "_print_enrichment_status", lambda: None)
    monkeypatch.setattr(viewer_server, "_print_llm_extraction_status", lambda: None)
    monkeypatch.setattr(viewer_server, "_run_until_stopped", lambda *a, **k: None)
    monkeypatch.setattr("smartmemory_app.events_server.start_background", lambda: None)
    monkeypatch.setattr(
        "smartmemory_app.runtime_diagnostics.install_daemon_diagnostics",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "smartmemory_app.crash_reporter.flush_in_background", lambda: None
    )
    monkeypatch.setattr(
        "smartmemory_app.hook_failures.consume_in_background", lambda *a: None
    )
    monkeypatch.setattr(
        "smartmemory_app.config.load_llm_keys_from_stores", lambda: None
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    config_args = []
    monkeypatch.setattr(
        viewer_server.uvicorn,
        "Config",
        lambda app, **kw: config_args.append(kw) or object(),
    )
    monkeypatch.setattr(viewer_server.uvicorn, "Server", lambda config: object())
    viewer_server.main(open_browser=False)
    assert config_args[0]["port"] == host_isolation_guard.port != REAL_PORT
    viewer_server.main(port=18123, open_browser=False)
    assert config_args[1]["port"] == 18123


def test_spawned_processes_are_stopped_at_teardown(host_isolation_guard):
    """stop_spawned ends a process the test left running, on the failure path too."""
    spawned = []
    init = hi.tracking_popen_init(spawned, host_isolation_guard.shim)
    proc = subprocess.Popen.__new__(subprocess.Popen)
    init(proc, ["sleep", "60"])
    assert proc.poll() is None
    assert hi.stop_spawned(spawned) == [f"pid {proc.pid}"]
    assert proc.poll() is not None
