"""Collect the single shared L-D3 replay against this wrapper checkout."""

from pathlib import Path
from runpy import run_path

_core = Path(__file__).resolve().parents[3] / "smart-memory-core"
test_lite_lexical_fill_and_forwarding_contract = run_path(
    str(_core / "tests/remediation/test_lexical_index.py")
)["test_lite_lexical_fill_and_forwarding_contract"]


def test_installed_sm_rebuild_lexical_contract(tmp_path, monkeypatch):
    """R-D5: actual installed sm group forwards repair options and failures."""
    from importlib.metadata import entry_points
    from click.testing import CliRunner
    from smartmemory.graph.backends.sqlite import SQLiteBackend
    from smartmemory.graph.backends.falkordb import FalkorDBBackend
    from smartmemory.tools import factory

    group = next(
        ep for ep in entry_points(group="console_scripts") if ep.name == "sm"
    ).load()
    db = SQLiteBackend(str(tmp_path / "memory.db"))
    db.add_node("a", {"content": "quartz"})
    db._conn.execute("DROP TABLE lexical_fts")
    db.close()
    monkeypatch.setattr(factory, "_default_data_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        FalkorDBBackend,
        "_repair_lexical_at",
        lambda **kw: calls.append(kw) or {"backend": "falkordb"},
    )
    runner = CliRunner()
    for options in ([], ["--backend", "sqlite", "--data-dir", str(tmp_path)]):
        result = runner.invoke(group, ["rebuild", "--lexical", *options])
        assert result.exit_code == 0, result.output
        assert '"backend": "sqlite"' in result.output
    assert calls == []
    result = runner.invoke(group, ["rebuild", "--lexical", "--backend", "falkordb"])
    assert result.exit_code == 0, result.output
    assert calls == [{}]

    def failed(**kw):
        raise RuntimeError("repair refused")

    monkeypatch.setattr(FalkorDBBackend, "_repair_lexical_at", failed)
    result = runner.invoke(group, ["rebuild", "--lexical", "--backend", "falkordb"])
    assert result.exit_code != 0 and "repair refused" in result.output
    assert "RANGE" in runner.invoke(group, ["rebuild", "--help"]).output
