"""Console entries preserve wildcard queries under Click's Windows semantics."""

import importlib
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import click.core
import pytest
from smartmemory.models.memory_item import MemoryItem
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import cli, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS

SCRIPTS = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
)["project"]["scripts"]


@pytest.fixture
def memory(tmp_path, monkeypatch):
    root = tmp_path / "test_G_isolated"
    root.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(root))
    for name, directory in (
        ("APPDATA", "test_G_appdata"),
        ("XDG_CONFIG_HOME", "test_G_config"),
        ("SMARTMEMORY_DATA_DIR", "test_G_data"),
    ):
        monkeypatch.setenv(name, str(root / directory))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    for key in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(key, "")
    cwd = root / "test_G_cwd"
    cwd.mkdir()
    for name in ("test_G_a", "test_G_b"):
        (cwd / name).write_text("wildcard expansion must not reach search")
    monkeypatch.chdir(cwd)
    mem = None
    try:
        mem = create_lite_memory(
            str(root / "test_G_data"),
            pipeline_profile=PipelineConfig.lite_hermetic(llm_enabled=False),
            spawn_worker=False,
        )
        mem.add(
            MemoryItem(
                item_id="test_G_memory",
                content="test_G saved memory",
                memory_type="episodic",
                origin="cli:add",
                metadata={"project": "test_G_?"},
            )
        )
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        # Isolate transport and startup prerequisites, retaining real storage search.
        monkeypatch.setattr(cli, "_daemon_request", lambda *args, **kwargs: None)
        monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kwargs: None)
        yield mem
    finally:
        if mem is not None:
            mem.close()
        monkeypatch.chdir(tmp_path)
        shutil.rmtree(root)
        assert not root.exists(), "test_G store or profile leaked"


@pytest.mark.parametrize("script", ["sm", "smartmemory"])
@pytest.mark.parametrize("property_value", [None, "test_G_?"])
def test_console_entry_preserves_windows_wildcards(
    memory, monkeypatch, capsys, script, property_value
):
    module_name, entry_name = SCRIPTS[script].split(":")
    entry = getattr(importlib.import_module(module_name), entry_name)
    argv = [script, "search", "*", "--json"]
    if property_value is not None:
        argv.extend(["--project", property_value])
    monkeypatch.setattr(sys, "argv", argv)
    # Replace only Click's os binding, avoiding WindowsPath on the host platform.
    click_os = SimpleNamespace(**vars(os))
    click_os.name = "nt"
    monkeypatch.setattr(click.core, "os", click_os)
    queries = []
    real_search = storage.search

    def record_search(query, *args, **kwargs):
        queries.append(query)
        return real_search(query, *args, **kwargs)

    monkeypatch.setattr(storage, "search", record_search)
    with pytest.raises(SystemExit) as exited:
        entry()
    output = capsys.readouterr()
    assert "Unsupported property option" not in output.err, output.err
    assert exited.value.code == 0, output.err
    assert queries == ["*"]
    assert [row["item_id"] for row in json.loads(output.out)] == ["test_G_memory"]
    print(
        f"{script}: query={queries[0]!r}, property={property_value!r}, saved memory returned"
    )


def test_console_scripts_use_main():
    assert SCRIPTS["sm"] == "smartmemory_app.cli:main"
    assert SCRIPTS["smartmemory"] == "smartmemory_app.cli:main"
