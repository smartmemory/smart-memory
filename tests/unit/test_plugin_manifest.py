"""The plugin launches modules shipped by its installed dependencies."""

import importlib
import json
from pathlib import Path


def test_plugin_modules_are_importable():
    manifest = json.loads((Path(__file__).parents[2] / "plugin.json").read_text())
    modules = []
    for server in manifest["mcpServers"].values():
        args = server["args"]
        for index, arg in enumerate(args):
            if arg == "-m":
                modules.append(args[index + 1])
    assert modules == ["smartmemory_mcp.server"]
    for module in modules:
        assert importlib.import_module(module)
