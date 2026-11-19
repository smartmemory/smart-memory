"""Regressions for setup's MCP launcher and CLI failure reporting."""

import json
import os
import runpy
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import cli, config, setup_run, viewer_server


@pytest.fixture
def setup_launcher(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / ("smartmemory-mcp.exe" if os.name == "nt" else "smartmemory-mcp")
    launcher.touch(mode=0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    config_file = tmp_path / "config.toml"
    config_file.write_text('[smartmemory]\nmode = "local"\n', encoding="utf-8")
    monkeypatch.setattr(config, "config_path", lambda: config_file)

    # Resume a local setup whose provisioning is already complete. Never start a daemon.
    class CompletedProvisioning:
        @staticmethod
        def resumable(**kwargs):
            return SimpleNamespace(choices={"mode": "local"})

        def __init__(self, state, **kwargs):
            self.daemon_status = {"status": "ok"}

        def resume(self):
            pass

    monkeypatch.setattr(setup_run, "SetupRun", CompletedProvisioning)
    return launcher


def assert_mcp_config(output, launcher):
    line = next(line for line in output.splitlines() if "Add to your MCP config:" in line)
    block = json.loads(line.split("Add to your MCP config:", 1)[1])
    assert block == {
        "command": str(launcher),
        "args": [],
        "env": {"SMARTMEMORY_MCP_MODE": "local"},
    }


@pytest.mark.parametrize("entry", ["setup", "init"])
def test_setup_prints_mcp_launcher(entry, setup_launcher):
    result = CliRunner().invoke(cli.cli, [entry, "--for", "cursor"])
    assert result.exit_code == 0, result.output
    assert_mcp_config(result.stdout, setup_launcher)


def test_setup_module_prints_mcp_launcher(setup_launcher, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smartmemory_app.setup", "--for", "cursor"])
    monkeypatch.delitem(sys.modules, "smartmemory_app.setup")
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("smartmemory_app.setup", run_name="__main__", alter_sys=True)
    assert exc.value.code == 0
    assert_mcp_config(capsys.readouterr().out, setup_launcher)


def test_setup_names_missing_mcp_launcher(setup_launcher):
    setup_launcher.unlink()
    result = CliRunner().invoke(cli.cli, ["setup", "--for", "cursor"])
    assert result.exit_code == 1, result.output
    assert "smartmemory-mcp" in result.stderr
    assert "SmartMemory is ready" not in result.stdout


def test_retired_server_command_errors():
    result = CliRunner().invoke(cli.cli, ["server"])
    assert result.exit_code == 1, result.output
    assert "smartmemory-mcp" in result.stderr
    assert not result.stdout


def test_reindex_reports_failed_embeddings(tmp_path, monkeypatch):
    import numpy as np

    from smartmemory.plugins import embedding

    class FailingItemEmbedder:
        provider = "local"

        def embed(self, content):
            if content == "dimension probe":
                return np.ones(3, dtype=np.float32)
            raise RuntimeError("item embedding failed")

    monkeypatch.setattr(embedding, "EmbeddingService", FailingItemEmbedder)
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    with sqlite3.connect(tmp_path / "memory.db") as db:
        db.execute("CREATE TABLE nodes (item_id TEXT, memory_type TEXT, properties TEXT)")
        db.execute(
            "INSERT INTO nodes VALUES (?, ?, ?)",
            ("audit-cli-reindex", "episodic", json.dumps({"content": "audit item"})),
        )

    with TestClient(viewer_server.app, base_url="http://127.0.0.1") as client:
        def daemon_request(method, path):
            return client.request(method, path).raise_for_status().json()

        monkeypatch.setattr(cli, "_daemon_request", daemon_request)
        result = CliRunner().invoke(cli.cli, ["admin", "reindex"])

    assert result.exit_code == 1, result.output
    assert "1 of 1" in result.stderr
    assert "skipped" in result.stderr
    assert "Reindexed" not in result.stdout
