"""Portable regressions for the wrapper Windows audit, without native OS side effects."""

import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock

import click
import psutil
import pytest
from click.testing import CliRunner

from smartmemory.utils import process
from smartmemory_app import (
    capture_queue,
    cli_mcp,
    daemon,
    lifecycle,
    recall_format,
    setup,
    viewer_server,
)
from smartmemory_app.console import read_utf8_stdin
from smartmemory_app.runtime_diagnostics import SharedRotatingFileHandler


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setattr(daemon, "_data_dir", lambda: tmp_path)


def test_windows_liveness_and_identity_never_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        os, "kill", Mock(side_effect=AssertionError("liveness must not signal"))
    )
    child = SimpleNamespace(
        status=lambda: psutil.STATUS_RUNNING,
        cmdline=lambda: ["python", "-m", "unrelated"],
    )
    monkeypatch.setattr(process.psutil, "pid_exists", lambda pid: pid == 123)
    monkeypatch.setattr(process.psutil, "Process", lambda pid: child)
    (tmp_path / "worker.0.pid").write_text("123")
    assert daemon._pid_alive(123)
    daemon._retire_legacy_workers()
    assert not (tmp_path / "worker.0.pid").exists()
    os.kill.assert_not_called()


def test_unavailable_legacy_identity_preserves_marker(monkeypatch, tmp_path):
    marker = tmp_path / "worker.0.pid"
    marker.write_text("123")
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "process_cmdline", lambda pid: None)
    terminate = Mock()
    monkeypatch.setattr(daemon, "terminate_process", terminate)
    with pytest.raises(RuntimeError, match="Cannot inspect live legacy PID"):
        daemon._retire_legacy_workers()
    assert marker.read_text() == "123"
    terminate.assert_not_called()


def test_portable_termination_escalates_without_signal_constants(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    class Child:
        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append("wait")
            if calls.count("wait") == 1:
                raise psutil.TimeoutExpired(timeout)

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(process.psutil, "Process", lambda pid: Child())
    monkeypatch.setattr(os, "kill", Mock(side_effect=AssertionError("raw signalling")))
    assert process.terminate_process(123, 0.01)
    assert calls == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize(
    "identity",
    [None, ["python", "unrelated"], ["python", "-m", "smartmemory_app.viewer_server"]],
)
def test_unhealthy_daemon_identity_and_exit(monkeypatch, tmp_path, identity):
    marker = tmp_path / "daemon.pid"
    marker.write_text("123")
    monkeypatch.setattr(daemon, "_stop_workers", lambda: None)
    monkeypatch.setattr(daemon, "is_running", lambda **kw: False)
    monkeypatch.setattr(daemon, "process_cmdline", lambda pid: identity)
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_pause", lambda seconds: None)
    # Expire the cooperative grace immediately to exercise revalidation/escalation.
    clock = iter(range(100))
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(clock))
    terminated = Mock(return_value=True)
    monkeypatch.setattr(daemon, "terminate_process", terminated)
    if identity is None:
        with pytest.raises(RuntimeError, match="marker retained"):
            daemon.stop_daemon()
        assert marker.exists()
    else:
        daemon.stop_daemon()
        assert not marker.exists()
        assert terminated.call_count == int("smartmemory_app.viewer_server" in identity)
    assert not (tmp_path / ".daemon.stop-123").exists()


def test_daemon_cooperative_stop_drains_then_cleans_up(tmp_path):
    events = []
    request = tmp_path / ".daemon.stop-123"

    class Server:
        should_exit = False

        def run(self):
            request.write_text("stop")
            deadline = time.monotonic() + 2
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)
            assert self.should_exit
            events.append("drained")

    viewer_server._run_until_stopped(
        Server(), tmp_path, 123, lambda: events.append("closed")
    )
    assert events == ["drained", "closed"]
    assert not request.exists()
    assert not any(t.name == "smartmemory-stop" for t in threading.enumerate())


def test_restart_stops_orphan_even_when_daemon_absent(monkeypatch):
    from importlib import import_module

    cli = import_module("smartmemory_app.cli")
    events = []
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "should_be_running", lambda: False)
    monkeypatch.setattr(daemon, "stop_daemon", lambda: events.append("orphan stopped"))
    monkeypatch.setattr(
        cli,
        "_start_with_progress",
        lambda **kw: events.append("replacement started") or {"status": "ok"},
    )
    monkeypatch.setattr(cli, "_report_start_status", lambda info: None)
    result = CliRunner().invoke(cli.cli, ["restart"])
    assert result.exit_code == 0, result.output
    assert events == ["orphan stopped", "replacement started"]


def test_session_second_save_replaces_and_serializes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path, "rename", Mock(side_effect=FileExistsError("Windows rename"))
    )
    session = lifecycle.MemoryLifecycle("test_windows_session")
    session._current_user_turn = "first"
    session._save_state()
    session._current_user_turn = "你好 Łukasz"
    session._save_state()
    assert (
        json.loads(session._state_path().read_text())["current_user_turn"]
        == "你好 Łukasz"
    )
    sessions = [lifecycle.MemoryLifecycle("test_windows_session") for _ in range(6)]
    errors = []

    def save(i):
        try:
            sessions[i]._current_user_turn = str(i)
            sessions[i]._save_state()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(len(sessions))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert json.loads(session._state_path().read_text())["current_user_turn"] in {
        str(i) for i in range(6)
    }
    assert not list((tmp_path / "sessions").glob("*.tmp"))


@pytest.mark.parametrize(
    "command",
    [
        r"C:\Users\Alice Smith\Ł你好\smartmemory-mcp.exe",
        'C:\\a"b[1]\\smartmemory-mcp.exe',
    ],
)
def test_codex_windows_command_roundtrips(tmp_path, monkeypatch, command):
    monkeypatch.setattr(cli_mcp, "_resolve_server_command", lambda: command)
    path = tmp_path / "config.toml"
    path.write_text('model = "example"\n[mcp_servers.other]\ncommand = "other"\n')
    for _ in range(2):
        cli_mcp._write_codex_toml(path, dry_run=False)
        config = tomllib.loads(path.read_text())
        assert config["mcp_servers"]["smartmemory"]["command"] == command
        assert config["mcp_servers"]["other"]["command"] == "other"
        assert config["model"] == "example"


def test_invalid_codex_config_preserved(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "unterminated')
    original = path.read_bytes()
    with pytest.raises(click.ClickException, match="Refusing"):
        cli_mcp._write_codex_toml(path, dry_run=False)
    assert path.read_bytes() == original


def test_clear_retained_file_is_failure_without_success_side_effects(
    tmp_path, monkeypatch
):
    from smartmemory_app import local_api, storage, store_generation
    from fastapi import HTTPException
    from importlib import import_module

    cli = import_module("smartmemory_app.cli")
    path = tmp_path / "memory.db"
    path.write_bytes(b"retained")
    real_unlink = Path.unlink

    def unlink(p, *args, **kwargs):
        if p == path:
            raise PermissionError("Windows open handle")
        return real_unlink(p, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(storage, "_shutdown", lambda: None)
    monkeypatch.setattr(storage, "_resolve_data_dir", lambda: tmp_path)
    seed, bump = Mock(), Mock()
    monkeypatch.setattr(setup, "_seed_data_dir", seed)
    monkeypatch.setattr(store_generation, "bump_store_generation", bump)
    with pytest.raises(HTTPException) as exc:
        local_api.clear_all()
    assert exc.value.status_code == 409
    assert "retained memory.db" in exc.value.detail
    monkeypatch.setattr(cli, "require_local", lambda command: None)
    monkeypatch.setattr(cli, "_daemon_request", lambda *a, **kw: None)
    result = CliRunner().invoke(cli.cli, ["clear", "--yes"])
    assert result.exit_code != 0 and "retained memory.db" in result.output
    assert "Cleared" not in result.output
    assert path.read_bytes() == b"retained"
    seed.assert_not_called()
    bump.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    ["你好 Łukasz", json.dumps({"prompt": "你好 Łukasz"}, ensure_ascii=False)],
)
def test_utf8_redirected_input_ignores_locale(monkeypatch, payload):
    stream = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", stream)
    assert read_utf8_stdin() == payload


def test_lifecycle_utf8_protocol_and_invalid_input(monkeypatch, caplog):
    from smartmemory_app.cli import _read_lifecycle_payload

    body = {"prompt": "你好 Łukasz", "cwd": "C:\\Users\\Łukasz"}
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(json.dumps(body, ensure_ascii=False).encode()), encoding="cp1252"
        ),
    )
    assert _read_lifecycle_payload("recall") == body
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="cp1252")
    )
    assert _read_lifecycle_payload("recall") is None
    assert "not UTF-8" in caplog.text


def test_claude_uninstall_reads_utf8_and_preserves_undecodable_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(setup, "SETTINGS", path)
    path.write_text(
        json.dumps({"name": "你好 Łukasz", "hooks": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    read_text = Path.read_text

    def checked_read(p, *args, **kwargs):
        if p == path:
            assert kwargs.get("encoding") == "utf-8"
        return read_text(p, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    setup._deregister_hooks()
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "你好 Łukasz"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        setup._deregister_hooks()
    assert path.read_bytes() == b"\xff"


def test_git_workspace_utf8_decode_boundary(tmp_path, monkeypatch):
    root = str(tmp_path / "你好 Łukasz")

    def run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        return subprocess.CompletedProcess(
            args, 0, root.encode().decode(kwargs["encoding"]), ""
        )

    monkeypatch.setattr(recall_format.subprocess, "run", run)
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    expected = "ws_" + hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:12]
    assert recall_format.derive_workspace_id(str(tmp_path)) == expected


def test_windows_workspace_native_and_git_slash_paths_match(monkeypatch):
    import ntpath

    monkeypatch.setattr(recall_format, "os", SimpleNamespace(environ={}, path=ntpath))
    roots = iter(["C:/Users/你好/project", "C:\\Users\\你好\\project"])
    monkeypatch.setattr(
        recall_format.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, next(roots), ""),
    )
    assert recall_format.derive_workspace_id(
        "C:/Users/你好/project"
    ) == recall_format.derive_workspace_id("C:\\Users\\你好\\project")


def test_shell_profile_utf8_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    profile = tmp_path / ".zshrc"
    profile.write_text(
        '# 你好 Łukasz\nexport TEST_WINDOWS_KEY="old"\n', encoding="utf-8"
    )
    real_read, real_write = Path.read_text, Path.write_text

    def read(path, **kwargs):
        if path == profile:
            assert kwargs.get("encoding") == "utf-8"
        return real_read(path, **kwargs)

    def write(path, data, **kwargs):
        if path == profile:
            assert kwargs.get("encoding") == "utf-8"
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.delenv("TEST_WINDOWS_KEY", raising=False)
    setup._persist_env_var("TEST_WINDOWS_KEY", "replacement")
    assert setup._read_env_from_profile("TEST_WINDOWS_KEY") == "replacement"
    assert "你好 Łukasz" in profile.read_text(encoding="utf-8")


@pytest.mark.parametrize("launch", ["daemon", "worker", "capture"])
def test_windows_native_detachment_at_each_launch(tmp_path, monkeypatch, launch):
    monkeypatch.setattr(sys, "platform", "win32")
    popen = Mock(return_value=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda **kw: False)
    if launch == "worker":
        from smartmemory.pipeline.work_graph import spawn

        monkeypatch.setattr(spawn, "worker_is_running", lambda data: False)
        daemon._start_workers()
    elif launch == "capture":
        root = tmp_path / "captures"
        root.mkdir()
        monkeypatch.setattr(capture_queue, "capture_dir", lambda: root)
        capture_queue.spawn_worker()
    else:
        import socket

        statuses = iter([None, {"status": "ok"}])
        monkeypatch.setattr(daemon, "get_status", lambda: next(statuses))
        monkeypatch.setattr(daemon, "_port", lambda: 9014)
        monkeypatch.setattr(daemon, "_start_workers", lambda *a: None)
        monkeypatch.setattr(
            socket,
            "socket",
            lambda *a: SimpleNamespace(
                settimeout=lambda t: None, connect_ex=lambda addr: 0, close=lambda: None
            ),
        )
        daemon.start_daemon()
    kw = popen.call_args.kwargs
    assert kw["creationflags"] == 0x200 | 0x08000000 | 0x01000000
    assert "start_new_session" not in kw
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"].closed
    if launch == "daemon":
        assert Path(kw["stdout"].name).name == "daemon-output.log"


def test_shared_rotation_releases_handles_and_preserves_records(tmp_path):
    path = tmp_path / "shared.log"
    handlers = [
        SharedRotatingFileHandler(path, maxBytes=60, backupCount=20, encoding="utf-8")
        for _ in range(2)
    ]
    records = []
    try:
        for i in range(20):
            text = f"test_windows_record_{i:02}"
            records.append(text)
            handler = handlers[i % 2]
            handler.handle(
                logging.makeLogRecord({"msg": text, "levelno": logging.INFO})
            )
            assert all(h.stream is None for h in handlers)
        contents = "".join(p.read_text() for p in tmp_path.glob("shared.log*"))
        assert all(contents.count(record) == 1 for record in records)
        assert path.with_name("shared.log.1").exists()
    finally:
        for handler in handlers:
            handler.close()


def test_shared_rotation_across_real_processes(tmp_path):
    path = tmp_path / "test_windows_shared.log"
    script = """import logging, sys
from smartmemory_app.runtime_diagnostics import SharedRotatingFileHandler
handler = SharedRotatingFileHandler(sys.argv[1], maxBytes=120, backupCount=50, encoding='utf-8')
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.DEBUG)
try:
    for i in range(30):
        root.info('test_windows_%s_%02d', sys.argv[2], i)
finally:
    root.removeHandler(handler)
    handler.close()
"""
    children = []
    try:
        for name in ("a", "b"):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(path), name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for child in children:
            _, errors = child.communicate(timeout=10)
            assert child.returncode == 0, errors
            assert not errors
        contents = "".join(
            p.read_text() for p in tmp_path.glob("test_windows_shared.log*")
        )
        for name in ("a", "b"):
            for i in range(30):
                assert contents.count(f"test_windows_{name}_{i:02}") == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            child.stderr.close()


def test_windows_setup_discloses_missing_supervisor(monkeypatch, capsys, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda: False)
    monkeypatch.setattr(setup, "_install_launchd_plist", lambda: False)
    monkeypatch.setattr(daemon, "start_daemon", lambda **kw: {"status": "ok"})
    setup._start_daemon_local()
    text = capsys.readouterr().out
    assert "login auto-start" in text and "sm start" in text
    assert "automatic crash recovery are unavailable" in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_windows_key_persistence_truthful_guidance(
    tmp_path, monkeypatch, capsys, caplog, fails
):
    import keyring

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHELL", raising=False)
    write = Mock(
        side_effect=RuntimeError("credential backend unavailable") if fails else None
    )
    monkeypatch.setattr(keyring, "set_password", write)
    key = "test_windows_secret"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    setup._persist_env_var("GROQ_API_KEY", key)
    write.assert_called_once_with("smartmemory", "GROQ_API_KEY", key)
    assert os.environ["GROQ_API_KEY"] == key
    text = capsys.readouterr().out
    assert "PowerShell" in text and "SetEnvironmentVariable" in text
    assert key not in text
    assert not (tmp_path / ".zshrc").exists()
    if fails:
        assert "only set for this setup session" in text
        assert "Credential persistence failed" in caplog.text


def test_windows_doctor_repair_uses_native_python(monkeypatch):
    from smartmemory_app.cli import _venv_repair_commands

    monkeypatch.setattr(sys, "platform", "win32")
    text = _venv_repair_commands()
    assert ".venv\\Scripts\\python.exe -m pip" in text
    assert "source" not in text and "&&" not in text
