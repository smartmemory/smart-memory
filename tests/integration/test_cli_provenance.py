"""CORE-CODE-PROVENANCE-1 Phase 2a — S06 `provenance import-codex` CLI (real lite backend).

CliRunner over a fixture ~/.codex/sessions tree: the import persists evidence rows
+ a :Session anchor with exact line_no/tool_ref, re-running is idempotent (no
duplication), --dry-run persists nothing, and --since filters by path-date.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from smartmemory.provenance.evidence import session_item_id


@pytest.fixture
def codex_tree(tmp_path):
    """A ~/.codex/sessions tree with one rollout (session_meta + completed apply_patch)."""
    day = tmp_path / "codex" / "2026" / "01" / "15"
    day.mkdir(parents=True)
    rollout = day / "rollout-2026-01-15T10-00-00-abc.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "cdx-import-1", "cwd": "/proj"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "call_id": "c1", "status": "completed",
            "input": "*** Begin Patch\n*** Add File: imported.py\n+def f():\n+    return 7\n*** End Patch\n"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c1", "output": '{"metadata": {"exit_code": 0}}'}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return tmp_path / "codex"


def _fresh_mem(tmp_path, monkeypatch):
    import smartmemory_app.storage as st

    data_dir = tmp_path / "smdata"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    st._memory = None
    st._data_path = None
    try:
        mem = st.get_memory(data_dir=str(data_dir))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"lite memory unavailable: {exc}")
    if not hasattr(mem, "ingest_structured"):
        pytest.skip("active backend lacks ingest_structured")
    return mem


def _prov(mem):
    return [n for n in mem._graph.search_nodes({"memory_type": "code_provenance"})
            if n.metadata.get("session_id") == "cdx-import-1"]


def test_import_codex_persists_and_is_idempotent(codex_tree, tmp_path, monkeypatch):
    mem = _fresh_mem(tmp_path, monkeypatch)
    from smartmemory_app.cli import cli

    runner = CliRunner()
    r1 = runner.invoke(cli, ["provenance", "import-codex", "--codex-dir", str(codex_tree)])
    assert r1.exit_code == 0, r1.output
    assert "Imported 1 evidence rows across 1 sessions" in r1.output

    prov = _prov(mem)
    assert len(prov) == 1
    node = prov[0]
    assert node.metadata["source"] == "codex"
    assert node.metadata["tool_ref"] == "c1"  # Codex import has the call_id
    assert node.metadata["line_no"] == 2  # exact record index
    assert node.content == "def f():\n    return 7"
    assert mem.get(session_item_id("codex", "cdx-import-1")) is not None  # :Session anchor

    # idempotent re-run — deterministic ids + MERGE → no duplication
    r2 = runner.invoke(cli, ["provenance", "import-codex", "--codex-dir", str(codex_tree)])
    assert r2.exit_code == 0, r2.output
    assert len(_prov(mem)) == 1


def test_import_codex_dry_run_persists_nothing(codex_tree, tmp_path, monkeypatch):
    mem = _fresh_mem(tmp_path, monkeypatch)
    from smartmemory_app.cli import cli

    res = CliRunner().invoke(cli, ["provenance", "import-codex", "--codex-dir", str(codex_tree), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Would import 1 evidence rows" in res.output
    assert _prov(mem) == []  # nothing persisted


def test_import_codex_since_filters_by_path_date(codex_tree, tmp_path, monkeypatch):
    mem = _fresh_mem(tmp_path, monkeypatch)
    from smartmemory_app.cli import cli

    # rollout is under 2026/01/15; --since after that date → imports nothing
    res = CliRunner().invoke(cli, ["provenance", "import-codex", "--codex-dir", str(codex_tree),
                                   "--since", "2027-01-01"])
    assert res.exit_code == 0, res.output
    assert "Imported 0 evidence rows across 0 sessions" in res.output
    assert _prov(mem) == []
