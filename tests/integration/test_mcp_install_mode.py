"""`sm mcp install` writes SMARTMEMORY_MCP_MODE=local for a local install only (MCP-INSTALL-LOCAL-MODE-1).

smartmemory-mcp defaults to its REST backend (MCP-PERF-1), so a Lite user's entry must name the in-process store.
One row per saved CLI mode and client; the config file is real, the written client config is read back.
"""

from __future__ import annotations

import json
import tomllib

import pytest
from click.testing import CliRunner

LOCAL = {"SMARTMEMORY_MCP_MODE": "local"}

# (id, config.toml text or None, SMARTMEMORY_MODE in the shell, client, expected env of the written entry)
ROWS = [
    ("local-json", '[smartmemory]\nmode = "local"\n', None, "claude-code", LOCAL),
    ("local-cursor", '[smartmemory]\nmode = "local"\n', None, "cursor", LOCAL),
    ("local-codex", '[smartmemory]\nmode = "local"\n', None, "codex", LOCAL),
    ("remote-json", '[smartmemory]\nmode = "remote"\n', None, "claude-code", {}),
    ("remote-codex", '[smartmemory]\nmode = "remote"\n', None, "codex", {}),
    # the CLI has no hosted mode: hosted is the server deployment, so an install never writes it
    ("hosted-json", '[smartmemory]\nmode = "hosted"\n', None, "claude-code", {}),
    ("hosted-codex", '[smartmemory]\nmode = "hosted"\n', None, "codex", {}),
    ("unconfigured-json", None, None, "claude-code", {}),
    # the client starts the server without this shell's env, so the shell's mode must not decide
    ("remote-file-local-shell", '[smartmemory]\nmode = "remote"\n', "local", "claude-code", {}),
    ("local-file-remote-shell", '[smartmemory]\nmode = "local"\n', "remote", "claude-code", LOCAL),
]


@pytest.mark.parametrize("config,shell_mode,client,expected", [r[1:] for r in ROWS], ids=[r[0] for r in ROWS])
def test_mcp_install_writes_mode_env(config, shell_mode, client, expected, tmp_path, monkeypatch):
    from smartmemory_app.cli import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    for name in ("SMARTMEMORY_MODE", "SMARTMEMORY_API_URL", "SMARTMEMORY_API_KEY", "SMARTMEMORY_MCP_MODE"):
        monkeypatch.delenv(name, raising=False)
    if shell_mode:
        monkeypatch.setenv("SMARTMEMORY_MODE", shell_mode)
    if config is not None:
        path = tmp_path / "xdg" / "smartmemory" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(config)

    target = tmp_path / ("codex.toml" if client == "codex" else "mcp.json")
    result = CliRunner().invoke(cli, ["mcp", "install", client, "--path", str(target)])
    assert result.exit_code == 0, result.output

    if client == "codex":
        entry = tomllib.loads(target.read_text())["mcp_servers"]["smartmemory"]
    else:
        entry = json.loads(target.read_text())["mcpServers"]["smartmemory"]
    assert entry.get("env", {}) == expected
