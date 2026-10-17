"""Portable regressions for the native Windows acceptance failures."""

import pytest
from click.testing import CliRunner

from smartmemory_app import cli, support_diagnostics as support


@pytest.mark.parametrize("command", [["doctor"], ["report", "--zip"]])
def test_local_diagnostics_never_probe_hosted_api(monkeypatch, tmp_path, command):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://api.smartmemory.ai")
    probes = []
    monkeypatch.setattr(
        support, "_probe", lambda url: probes.append(url) or "reachable"
    )
    if command[-1] == "--zip":
        command = [*command, str(tmp_path / "support.zip")]
    result = CliRunner().invoke(cli.cli, command)
    assert result.exit_code == 0, result.output
    assert all("api.smartmemory.ai" not in url for url in probes)
    if command[0] == "doctor":
        assert "skipped: local mode" in result.output


def test_remote_diagnostics_use_effective_lan_url(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "http://192.168.1.4:9001/prefix/")
    probes = []
    monkeypatch.setattr(
        support, "_probe", lambda url: probes.append(url) or "reachable"
    )
    rows = support.network_checks()
    assert "http://192.168.1.4:9001/prefix" in probes
    assert all("api.smartmemory.ai" not in url for url in probes)
    assert any("192.168.1.4:9001" in row for row in rows)


@pytest.mark.parametrize("use_flag", [False, True])
def test_remote_setup_validates_and_persists_custom_api(
    monkeypatch, tmp_path, use_flag
):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from smartmemory_app import config, setup

    import httpx

    real_get = httpx.get

    def guarded_get(target, **kwargs):
        assert target.startswith("http://127.0.0.1:"), target
        return real_get(target, trust_env=False, **kwargs)

    monkeypatch.setattr(httpx, "get", guarded_get)
    seen = []

    class Auth(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            data = json.dumps({"default_team_id": "test_b2_team"}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/test-prefix"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_API_URL", "http://127.0.0.1:1" if use_flag else url)
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.setattr(config, "set_api_key", lambda key: None)
    for name in ("_copy_hooks", "_copy_skills", "_register_hooks"):
        monkeypatch.setattr(setup, name, lambda: None)
    args = ["--mode", "remote"]
    if use_flag:
        args += ["--api-url", url + "/"]
    try:
        result = CliRunner().invoke(setup.setup, args, input="test_b2_key\n")
        assert result.exit_code == 0, result.output
        assert seen == [("/test-prefix/auth/me", "Bearer test_b2_key")]
        monkeypatch.delenv("SMARTMEMORY_API_URL")
        assert config.load_config().api_url == url
        assert config.load_config().team_id == "test_b2_team"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_windows_keyring_unavailable_uses_protected_file(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import keyring
    from smartmemory_app import config, windows_credentials
    from smartmemory_mcp import windows_credentials as store

    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32"))
    path = tmp_path / "credentials" / "api_key"
    monkeypatch.setattr(windows_credentials, "key_path", lambda: path)
    monkeypatch.setattr(
        windows_credentials, "legacy_key_path", lambda: tmp_path / "legacy"
    )
    monkeypatch.setattr(keyring, "get_password", lambda *args: None)
    monkeypatch.setattr(
        keyring, "set_password", lambda *args: (_ for _ in ()).throw(OSError("1312"))
    )
    monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
    monkeypatch.setattr(store, "_current_sid", lambda: "S-1-5-21-123")
    protected = []

    def acl(destination, sid):
        assert sid == "S-1-5-21-123"
        if destination.is_file() and destination != path:
            assert destination.read_bytes() == b""
        protected.append(destination)

    monkeypatch.setattr(store, "_restrict", acl)
    with pytest.warns(UserWarning, match="user-only Windows credential file"):
        config.set_api_key("test_b2_secret")
    assert config.get_api_key() == "test_b2_secret"
    assert any(p.name.startswith(".api_key-") for p in protected)
    assert path in protected


def test_windows_credential_acl_failure_leaves_no_key(monkeypatch, tmp_path):
    from smartmemory_app import windows_credentials
    from smartmemory_mcp import windows_credentials as store

    path = tmp_path / "credentials" / "api_key"
    monkeypatch.setattr(windows_credentials, "key_path", lambda: path)
    monkeypatch.setattr(
        windows_credentials, "legacy_key_path", lambda: tmp_path / "legacy"
    )
    monkeypatch.setattr(store, "_current_sid", lambda: "S-1-5-21-123")

    def fail(destination, sid):
        raise store.CredentialProtectionError("API key not persisted: ACL denied")

    monkeypatch.setattr(store, "_restrict", fail)
    with pytest.raises(OSError, match="not persisted"):
        windows_credentials.store_key("test_b2_secret")
    assert not path.exists()


def test_windows_hook_command_uses_validated_git_bash(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from smartmemory_app import setup

    git = tmp_path / "Program Files" / "Git" / "cmd" / "git.exe"
    bash = git.parent.parent / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(setup.shutil, "which", lambda name: str(git))
    probes = []
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda args, **kw: probes.append(args)
        or SimpleNamespace(returncode=0, stdout=""),
    )
    command = setup._hook_command("smartmemory-orient.sh")
    assert command.startswith(f'"{bash.as_posix()}" ')
    assert probes and probes[-1][0] == str(bash)
    assert setup._installed_hook_name(command) == "smartmemory-orient.sh"


def test_windows_setup_refuses_hooks_without_git_bash(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from smartmemory_app import setup
    import click

    monkeypatch.setattr(setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup, "SETTINGS", tmp_path / "settings.json")
    with pytest.raises(click.ClickException, match="Git Bash"):
        setup._register_hooks()
    assert not (tmp_path / "settings.json").exists()


def test_get_outputs_api_iso_timestamps(monkeypatch):
    import json
    from datetime import datetime, timezone
    from smartmemory.models.memory_item import MemoryItem

    item = MemoryItem(content="sm-native-B2 datetime", memory_type="semantic")
    item.transaction_time = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
    result = item.to_dict()
    assert isinstance(result["transaction_time"], datetime)
    monkeypatch.setattr(cli, "_memory_request", lambda *args, **kwargs: result)
    output = CliRunner().invoke(cli.cli, ["get", item.item_id])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["transaction_time"] == "2026-10-04T00:00:00+00:00"


def test_direct_store_wait_budget_exceeds_two_seconds(monkeypatch, tmp_path):
    import subprocess
    import sys
    from smartmemory_app import storage

    monkeypatch.delenv("SMARTMEMORY_WRITE_LOCK_TIMEOUT", raising=False)
    path = tmp_path / ".write.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from filelock import FileLock; import sys,time; "
            "lock=FileLock(sys.argv[1]); lock.acquire(); print('ready',flush=True); "
            "time.sleep(2.5); lock.release()",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with storage._get_lock_file(tmp_path):
            pass
    finally:
        child.terminate()
        child.wait()
        child.stdout.close()


def test_cli_store_busy_is_clear_nonzero_error(monkeypatch, tmp_path):
    from filelock import FileLock
    from smartmemory_app import storage

    monkeypatch.setenv("SMARTMEMORY_WRITE_LOCK_TIMEOUT", "0.05")
    monkeypatch.setattr(cli, "_memory_request", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kw: None)
    monkeypatch.setattr(storage, "get_memory", lambda: object())
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    with FileLock(tmp_path / ".write.lock"):
        result = CliRunner().invoke(cli.cli, ["add", "sm-native-B2 busy test"])
    assert result.exit_code == 1
    assert "store busy" in result.output.lower()
    assert "start" in result.output and "daemon" in result.output
    assert "Traceback" not in result.output and "report --zip" not in result.output


def test_daemon_write_response_drop_is_never_replayed(monkeypatch):
    import httpx

    calls = []

    class Dropped:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            calls.append(args)
            raise httpx.RemoteProtocolError("response dropped after write")

    monkeypatch.setattr(httpx, "Client", Dropped)
    monkeypatch.setattr("time.sleep", lambda n: None)
    import click

    with pytest.raises(click.ClickException, match="may have succeeded"):
        cli._daemon_request("POST", "/memory/ingest", json={"content": "sm-native-B2"})
    assert len(calls) == 1


def test_reset_refuses_live_owner_before_deleting_anything(tmp_path):
    import os
    from smartmemory_app.store_reset import remove_store_files

    files = [tmp_path / name for name in ("memory.db", "vectors.usearch", "lexical.db")]
    for file in files:
        file.write_bytes(b"sm-native-B2 intact")
    (tmp_path / ".worker.pid").write_text(str(os.getpid()))
    with pytest.raises(RuntimeError, match="smartmemory stop"):
        remove_store_files(tmp_path)
    assert all(file.read_bytes() == b"sm-native-B2 intact" for file in files)


def test_reset_preflights_every_file_before_deleting(monkeypatch, tmp_path):
    from smartmemory_app import store_reset

    files = [tmp_path / name for name in ("a-vectors.usearch", "z-memory.db")]
    for file in files:
        file.write_bytes(b"sm-native-B2 intact")

    def blocked(paths):
        assert set(paths) == set(files)
        raise PermissionError("z-memory.db open handle")

    monkeypatch.setattr(store_reset, "_exclusive_files", blocked, raising=False)
    with pytest.raises(RuntimeError, match="refused"):
        store_reset.remove_store_files(tmp_path)
    assert all(file.read_bytes() == b"sm-native-B2 intact" for file in files)


def test_detached_output_rotates_before_child_open(monkeypatch, tmp_path):
    from smartmemory_app import daemon

    path = tmp_path / "daemon-output.log"
    path.write_bytes(b"sm-native-B2 old output" * 10)
    (tmp_path / "daemon-output.log.1").write_bytes(b"older output")
    monkeypatch.setenv("SMARTMEMORY_DAEMON_OUTPUT_MAX_BYTES", "100")
    daemon._prepare_output_log(tmp_path)
    assert not path.exists()
    assert (
        tmp_path / "daemon-output.log.1"
    ).read_bytes() == b"sm-native-B2 old output" * 10
    with path.open("a", encoding="utf-8") as handle:
        handle.write("fresh output")
    daemon._prepare_output_log(tmp_path)
    assert path.read_text() == "fresh output"


@pytest.mark.parametrize(
    "profile", [r"C:\Users\sm-native-B2-person", r"C:\Users\sm-native-B2-你好"]
)
def test_support_privacy_redacts_windows_path_forms(monkeypatch, profile):
    import json
    from smartmemory_app.report_privacy import private_text

    monkeypatch.setenv("USERPROFILE", profile)
    monkeypatch.setenv("HOME", profile)
    monkeypatch.setenv("APPDATA", profile + r"\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", profile + r"\AppData\Local")
    forms = [
        profile + r"\notes",
        profile.replace("\\", "/") + "/notes",
        json.dumps(profile + r"\notes"),
        json.dumps(profile + r"\notes", ensure_ascii=False),
        json.dumps((profile + r"\AppData\Local\notes").lower()),
    ]
    text = private_text("\n".join(forms))
    assert "sm-native-B2" not in text.lower()
    assert "你好" not in text and "\\u4f60" not in text
    assert "C:" not in text


def test_support_config_omits_absolute_windows_paths(monkeypatch, tmp_path):
    from smartmemory_app import config

    cfg = config.SmartMemoryConfig(
        mode="local", data_dir=r"D:\private\sm-native-B2-person\data"
    )
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    text = support.support_texts("doctor result")["config.txt"]
    assert "sm-native-B2-person" not in text
    assert "omitted path" in text


def test_wrapper_requires_requests_socks_transport():
    import tomllib
    from pathlib import Path
    from packaging.requirements import Requirement

    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]
    requirements = [Requirement(value) for value in project["dependencies"]]
    assert any(
        req.name.lower() == "requests" and "socks" in req.extras for req in requirements
    )
