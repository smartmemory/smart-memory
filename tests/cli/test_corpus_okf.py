"""CLI corpus import/export coverage against hermetic lite-memory stores."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner


def _use_lite_storage(monkeypatch, data_dir: Path):
    """Reset the CLI storage singleton and point it at a fresh local store."""
    import smartmemory_app.storage as storage

    storage._shutdown()
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    memory = storage.get_memory(data_dir=str(data_dir))
    # The real lite store has an optional Redis-backed PPR invalidation hook.
    # Corpus I/O does not exercise it, and disabling its cache instance avoids
    # Redis connection retries in this no-Docker test.
    memory._ppr_cache_instance = False
    return storage


def _seed_items(storage) -> set[str]:
    """Add deterministic items without invoking the extraction pipeline."""
    from smartmemory.models import MemoryItem

    contents = {"Ada documents the OKF bundle.", "Grace validates the import path."}
    memory = storage.get_memory()
    for content in sorted(contents):
        memory.add(
            MemoryItem(content=content, memory_type="metadata", origin="test:cli-okf")
        )
    return contents


def _assert_contents(storage, expected_contents: set[str]) -> None:
    """Verify direct import preserved each exported item's content."""
    backend = storage.get_memory().graph.backend
    actual_contents = {
        node.get("content", node.get("properties", {}).get("content"))
        for node in backend.get_all_nodes()
    }
    assert expected_contents <= actual_contents


def test_admin_okf_round_trip_and_top_level_aliases(tmp_path, monkeypatch):
    """Default CLI corpus export/import uses an OKF directory and aliases resolve."""
    from smartmemory_app.cli import cli

    runner = CliRunner()
    source_storage = _use_lite_storage(monkeypatch, tmp_path / "source")
    contents = _seed_items(source_storage)

    bundle_dir = tmp_path / "admin-bundle"
    export_result = runner.invoke(cli, ["admin", "export", str(bundle_dir)])
    assert export_result.exit_code == 0, export_result.output
    assert "OKF bundle" in export_result.output
    from smartmemory.okf import parse_okf

    index = parse_okf((bundle_dir / "index.md").read_text(encoding="utf-8"))
    assert index.okf_version == "0.1"
    assert len(list(bundle_dir.glob("*.md"))) == len(contents) + 1

    # Top-level `export`/`import` aliases must round-trip REAL data, not just
    # register — export the seeded store via the alias, then import that bundle
    # back through the alias into a fresh store and assert content parity.
    alias_bundle_dir = tmp_path / "alias-bundle"
    alias_export = runner.invoke(cli, ["export", str(alias_bundle_dir)])
    assert alias_export.exit_code == 0, alias_export.output
    assert (alias_bundle_dir / "index.md").is_file()
    assert len(list(alias_bundle_dir.glob("*.md"))) == len(contents) + 1

    alias_target = _use_lite_storage(monkeypatch, tmp_path / "alias-target")
    alias_import = runner.invoke(
        cli, ["import", str(alias_bundle_dir), "--mode", "direct"]
    )
    assert alias_import.exit_code == 0, alias_import.output
    assert "OKF bundle: 2 pages" in alias_import.output
    _assert_contents(alias_target, contents)

    target_storage = _use_lite_storage(monkeypatch, tmp_path / "admin-target")
    import_result = runner.invoke(
        cli, ["admin", "import", str(bundle_dir), "--mode", "direct"]
    )
    assert import_result.exit_code == 0, import_result.output
    assert "OKF bundle: 2 pages" in import_result.output
    _assert_contents(target_storage, contents)


def test_legacy_jsonl_round_trip(tmp_path, monkeypatch):
    """The explicit legacy JSONL export/import path remains available."""
    from smartmemory_app.cli import cli

    runner = CliRunner()
    source_storage = _use_lite_storage(monkeypatch, tmp_path / "legacy-source")
    contents = _seed_items(source_storage)

    legacy_path = tmp_path / "corpus.jsonl"
    export_result = runner.invoke(
        cli, ["admin", "export", str(legacy_path), "--legacy-jsonl"]
    )
    assert export_result.exit_code == 0, export_result.output
    assert "legacy JSONL" in export_result.output
    assert legacy_path.is_file()
    assert not legacy_path.is_dir()

    target_storage = _use_lite_storage(monkeypatch, tmp_path / "legacy-target")
    import_result = runner.invoke(
        cli, ["admin", "import", str(legacy_path), "--legacy-jsonl", "--mode", "direct"]
    )
    assert import_result.exit_code == 0, import_result.output
    _assert_contents(target_storage, contents)
