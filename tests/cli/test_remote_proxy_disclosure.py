"""Local lifecycle disclosure through real config, CLI and ASGI health responses.

Process start/stop is replaced at the OS boundary so the suite never controls
the user's daemon. No remote backend or hosted transport is needed for health.
"""

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import cli, config, daemon, storage, viewer_server


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://test_a3.invalid/api/")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "test_a3_workspace")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))

    def forbidden(*args, **kwargs):
        pytest.fail(
            "proxy disclosure must not access hosted APIs, storage or local LLM keys"
        )

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "request", forbidden)
    monkeypatch.setattr(storage, "get_memory", forbidden)
    monkeypatch.setattr(config, "llm_key_present", forbidden)
    monkeypatch.setattr(config, "get_api_key", forbidden)
    monkeypatch.setattr(storage, "apply_runtime_config", forbidden)
    monkeypatch.setattr(storage, "llm_extraction_warning", forbidden)
    monkeypatch.setattr(storage, "_get_remote_memory", forbidden)
    monkeypatch.setattr("smartmemory_app.work_graph.get_work_status", forbidden)
    previous_state = viewer_server._get_startup_state()
    viewer_server._set_startup_state(None)
    with TestClient(viewer_server._build_app()) as client:
        try:
            yield client
        finally:
            viewer_server._set_startup_state(*previous_state)


def assert_scope(output, action):
    assert f"Remote mode: {action} manages the LOCAL proxy/viewer host." in output
    assert "https://test_a3.invalid/api/" in output
    assert "Workspace: test_a3_workspace" in output
    assert "Hosted deployment is not controlled by this command." in output
    assert "Hosted connectivity: not checked here. Run: sm status" in output
    assert (
        "Extraction runs on the hosted service. Its capability is not checked here."
        in output
    )
    assert "loading models" not in output
    assert "SmartMemory is ready." not in output


@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("status", ["ok", "warming", "degraded"])
def test_remote_start_reports_local_readiness_separately(
    proxy, monkeypatch, command, status
):
    viewer_server._set_startup_state(status, "test_a3 proxy initialization failed")
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    operations = []
    monkeypatch.setattr(daemon, "stop_daemon", lambda: operations.append("stop"))

    def start_daemon(**kwargs):
        operations.append("start")
        assert kwargs["wait_until_ready"] is (command == "restart")
        return proxy.get("/health").json()

    monkeypatch.setattr(daemon, "start_daemon", start_daemon)
    result = CliRunner().invoke(cli.cli, [command])
    assert result.exit_code == 0, result.output
    assert_scope(result.output, command)
    assert operations == (["stop", "start"] if command == "restart" else ["start"])
    assert ("Local proxy/viewer host is ready." in result.output) is (status == "ok")
    if status == "warming":
        assert (
            "Local proxy/viewer host is warming up. Readiness is not confirmed."
            in result.output
        )
        assert "sm start --wait" in result.output
    if status == "degraded":
        assert "Local proxy/viewer host needs attention." in result.output
        assert "test_a3 proxy initialization failed" in result.output


@pytest.mark.parametrize("status", ["ok", "warming", "degraded"])
def test_remote_start_reports_existing_host(proxy, monkeypatch, status):
    viewer_server._set_startup_state(status)
    monkeypatch.setattr(daemon, "get_status", lambda: proxy.get("/health").json())
    monkeypatch.setattr(
        daemon, "start_daemon", lambda **kw: pytest.fail("existing host restarted")
    )
    result = CliRunner().invoke(cli.cli, ["start"])
    assert result.exit_code == 0, result.output
    assert_scope(result.output, "start")
    assert ("Local proxy/viewer host is ready." in result.output) is (status == "ok")


def test_remote_start_waits_for_warming_host(proxy, monkeypatch):
    viewer_server._set_startup_state("warming")
    monkeypatch.setattr(daemon, "get_status", lambda: proxy.get("/health").json())

    def start_daemon(**kwargs):
        assert kwargs["wait_until_ready"] is True
        viewer_server._set_startup_state("ok")
        return proxy.get("/health").json()

    monkeypatch.setattr(daemon, "start_daemon", start_daemon)
    result = CliRunner().invoke(cli.cli, ["start", "--wait"])
    assert result.exit_code == 0, result.output
    assert_scope(result.output, "start")
    assert "Local proxy/viewer host is ready." in result.output


@pytest.mark.parametrize("command", ["start", "restart"])
@pytest.mark.parametrize("health", [None, {"status": "unexpected"}])
def test_remote_start_rejects_unverified_health(proxy, monkeypatch, command, health):
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "start_daemon", lambda **kw: health)
    monkeypatch.setattr(daemon, "stop_daemon", lambda: None)
    result = CliRunner().invoke(cli.cli, [command])
    assert result.exit_code == 1, result.output
    assert_scope(result.output, command)
    assert "Local proxy/viewer host" in result.output
    assert "is ready" not in result.output


@pytest.mark.parametrize("mode", ["local", "remote"])
@pytest.mark.parametrize("active", [False, True])
def test_stop_scope_and_wording_without_hosted_calls(proxy, monkeypatch, mode, active):
    monkeypatch.setenv("SMARTMEMORY_MODE", mode)
    monkeypatch.setattr(daemon, "is_running", lambda **kw: active)
    monkeypatch.setattr(daemon, "should_be_running", lambda: False)
    stopped = []
    monkeypatch.setattr(daemon, "stop_daemon", lambda: stopped.append(True))
    result = CliRunner().invoke(cli.cli, ["stop"])
    assert result.exit_code == 0, result.output
    assert stopped == [True]
    if mode == "remote":
        assert_scope(result.output, "stop")
        expected = (
            "Local proxy/viewer host stopped."
            if active
            else "Local proxy/viewer host is not running."
        )
        assert expected in result.output
    else:
        expected = (
            "Daemon stopped.\n"
            if active
            else "Daemon is not running. Workers stopped.\n"
        )
        assert result.output == expected


@pytest.mark.parametrize("status", [None, "ok", "warming", "degraded"])
@pytest.mark.parametrize("workspace", ["test_a3_workspace", ""])
def test_remote_health_discloses_scope_without_hosted_probe(
    proxy, monkeypatch, status, workspace
):
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", workspace)
    viewer_server._set_startup_state(status, "test_a3 failed")
    response = proxy.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "smartmemory"
    assert body["status"] == body["local_proxy_status"] == (status or "ok")
    assert body["mode"] == "remote"
    assert body["host_role"] == "local_proxy_viewer"
    assert body["api_url"] == "https://test_a3.invalid/api/"
    assert body["workspace_id"] == (workspace or None)
    assert body["workspace_selection"] == (
        "configured" if workspace else "hosted_default"
    )
    assert body["hosted_connectivity"] == body["hosted_extraction"] == "not_checked"
    assert body["hosted_connectivity_hint"] == "Run: sm status"
    assert body["extraction_location"] == body["llm_provider"] == "hosted"
    assert body["llm_key_present"] is None
    assert body["memories"] == -1
    if status == "degraded":
        assert body["degraded_reason"] == "test_a3 failed"


def test_default_workspace_is_explicit_without_auth_resolution(proxy, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "")
    monkeypatch.setattr(daemon, "is_running", lambda **kw: False)
    monkeypatch.setattr(daemon, "should_be_running", lambda: False)
    monkeypatch.setattr(daemon, "stop_daemon", lambda: None)
    result = CliRunner().invoke(cli.cli, ["stop"])
    assert result.exit_code == 0, result.output
    assert "Workspace: default (selected by hosted authentication)" in result.output


def test_remote_extraction_banner_skips_local_route_checks(proxy, capsys):
    viewer_server._print_llm_extraction_status()
    output = capsys.readouterr().out
    assert (
        "LLM extraction: runs on the hosted service (capability not checked here)."
        in output
    )
    assert "Hosted connectivity: not checked here. Run: sm status" in output
    assert "enabled" not in output
    assert "setup" not in output


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_enrichment_banner_names_active_host(proxy, monkeypatch, capsys, mode):
    monkeypatch.setenv("SMARTMEMORY_MODE", mode)
    viewer_server._print_enrichment_status()
    output = capsys.readouterr().out
    if mode == "remote":
        assert (
            output == "Enrichment: runs on the hosted service, not in a local worker.\n"
        )
    else:
        assert (
            output
            == "Enrichment queue: SQLite-backed (run `smartmemory worker --loop` for Tier 2)\n"
        )


@pytest.mark.parametrize("command", ["start", "restart"])
def test_local_startup_wording_is_unchanged(proxy, monkeypatch, command):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "start_daemon", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(daemon, "stop_daemon", lambda: None)
    result = CliRunner().invoke(cli.cli, [command])
    assert result.exit_code == 0, result.output
    expected = (
        "Starting SmartMemory (loading models)...\nSmartMemory is ready.\n"
        if command == "start"
        else "Stopping SmartMemory...\nStarting SmartMemory...\nSmartMemory is ready.\n"
    )
    assert result.output == expected


def test_local_existing_daemon_wording_is_unchanged(proxy, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr(daemon, "get_status", lambda: {"status": "ok"})
    result = CliRunner().invoke(cli.cli, ["start"])
    assert result.exit_code == 0, result.output
    assert result.output == "SmartMemory is already running.\n"
