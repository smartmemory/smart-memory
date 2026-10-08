"""`sm code index` indexes every supported language by default (CODE-INDEXER-HARDEN-1 F32)."""

import shutil

import pytest
from click.testing import CliRunner

from smartmemory.code.models import IndexResult
from smartmemory_app.cli_code import code_group
from smartmemory_app.hosted_code import prepare_code_index


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SMARTMEMORY_CODE_CHECKPOINT_DIR", str(tmp_path / "test_lang_checkpoints")
    )
    root = tmp_path / "test_lang_checkout"
    (root / "web").mkdir(parents=True)
    (root / "app.py").write_text("def py_fn():\n    return 1\n")
    (root / "web" / "app.ts").write_text("export function tsFn() {}\n")
    (root / "web" / "mod.mjs").write_text("export function mjsFn() {}\n")
    try:
        yield root
    finally:
        shutil.rmtree(tmp_path)


class _Memory:
    def __init__(self):
        self.calls = []

    def ingest_code(self, **kwargs):
        self.calls.append(kwargs)
        return IndexResult(repo=kwargs["repo"], replaced=True, files_parsed=1)


@pytest.mark.parametrize(
    "flags, expected, label",
    [
        ([], None, "langs=all"),
        (["--language", "python"], ["python"], "langs=python"),
        (["--languages", "typescript"], ["typescript"], "langs=typescript"),
    ],
    ids=["default-all", "narrow-language", "languages-alias"],
)
def test_cli_passes_default_or_narrowed_languages(
    checkout, monkeypatch, flags, expected, label
):
    memory = _Memory()
    monkeypatch.setattr("smartmemory_app.storage.get_memory", lambda: memory)
    result = CliRunner().invoke(
        code_group, ["index", str(checkout), "--repo", "test_lang_repo", *flags]
    )
    assert result.exit_code == 0, result.output
    assert memory.calls[0]["languages"] == expected
    assert label in result.output


def test_default_bundle_contains_every_supported_language(checkout):
    body, result = prepare_code_index(str(checkout), "test_lang_repo", "", None, None)
    names = {entity["name"] for entity in body["entities"]}
    assert {"py_fn", "tsFn", "mjsFn"} <= names
    assert result.files_failed == 0
    narrowed, _ = prepare_code_index(
        str(checkout), "test_lang_repo", "", None, ["python"]
    )
    assert "tsFn" not in {entity["name"] for entity in narrowed["entities"]}
