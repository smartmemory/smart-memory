"""The store-free CLI emits the shared engine contract."""

import json
import shutil
import pytest

from click.testing import CliRunner

from smartmemory_app.cli_code import code_group
from smartmemory.code.effects import scan_effects


@pytest.fixture(autouse=True)
def cleanup_effects_test_files(tmp_path):
    """Remove this test's source/checkpoint fixtures even after a failure."""
    try:
        yield
    finally:
        shutil.rmtree(tmp_path)


def test_effects_matches_shared_engine(tmp_path):
    source = tmp_path / "test_fx_repo"
    source.mkdir()
    (source / "app.py").write_text(
        'def write():\n    with open("test_fx_file", "w") as f:\n        f.write("hello")\n'
    )
    result = CliRunner().invoke(
        code_group, ["effects", str(source), "--repo", "test_fx_repo"]
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output == scan_effects(source, "test_fx_repo")
    assert any(
        a["kind"] == "write" and a["store_or_service"] == "fs" for a in output["atoms"]
    )
