"""Hosted CLI bundle preserves parser identity and evidence without a store."""

import pytest


import shutil


from smartmemory_app.hosted_code import prepare_code_index


def test_edge_bundle_keeps_identity_spans_and_calls(tmp_path):
    source = 'text = "你好"\nclass A:\n    def run(self):\n        missing()\nclass B:\n    def run(self):\n        pass\n'
    file = tmp_path / "test_edge_bundle.py"
    file.write_bytes(source.encode())
    body, result = prepare_code_index(
        str(tmp_path), "test_edge_repo", "test_edge_commit", None, ["python"]
    )
    methods = [e for e in body["entities"] if e["name"].endswith("run")]
    assert {e["qualified_name"] for e in methods} == {"A.run", "B.run"}
    for entity in methods:
        assert source.encode()[entity["byte_start"] : entity["byte_end"]].startswith(
            b"def run"
        )
        assert entity["end_line_number"] >= entity["line_number"]
        assert len(entity["content_hash"]) == 64
    caller = next(e for e in methods if e["qualified_name"] == "A.run")
    call = caller["call_evidence"][0]
    assert call["source_id"] == next(
        e.item_id for e in result.entities if e.qualified_name == "A.run"
    )
    assert call["properties"]["unresolved"] is True
    assert call["properties"]["resolution"] == "name_only"


@pytest.fixture(autouse=True)
def cleanup_edge_files(tmp_path):
    """Remove task-owned source files and SQLite stores on success and failure."""
    try:
        yield
    finally:
        shutil.rmtree(tmp_path)
