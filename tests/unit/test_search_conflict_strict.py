"""BUG-FAIL-IF-CONFLICTED-SOURCE-DATE-1: the local search path honours ``fail_if_conflicted`` and hands on the marker.

A real Lite core (SQLite in a pytest tmp dir, LLM off, no model call) holds frozen pair 4 of ``units/BUG-FAIL-IF-
CONFLICTED-SOURCE-DATE-1/notes-and-answers.md`` (two prices of one product, worded differently, no proven conflict).
The marker rides on a list subclass that ``search()`` flattens, so it travels through ``storage.last_conflict_check()``.
"""

import pytest
from smartmemory.errors import ConflictCheckUndecidedError
from smartmemory.models.memory_item import MemoryItem
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import storage

NOTE_A = "Lena, 3 March 2026: the distributor price list says the KX-410 costs 1,200 EUR."
NOTE_B = "From Marco's call with the reseller on 18 April 2026 - KX-410 price is 1,450 EUR."


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "JEV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    mem = create_lite_memory(
        str(tmp_path / "s"), pipeline_profile=PipelineConfig.lite(llm_enabled=False), spawn_worker=False
    )
    for text in (NOTE_A, NOTE_B):
        mem.add(MemoryItem(content=text, memory_type="semantic"))
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    yield mem
    mem.close()


def test_the_flag_is_forwarded_and_the_undecided_error_arrives_as_itself(lite, monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_CONFLICT_CHECK_UNSURE", raising=False)
    with pytest.raises(ConflictCheckUndecidedError) as error:
        storage.search("KX-410", 10, fail_if_conflicted=True)
    assert len(error.value.pairs) == 1
    assert storage.last_conflict_check() is None


def test_mark_returns_the_rows_and_the_marker_unchanged(lite, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CONFLICT_CHECK_UNSURE", "mark")
    rows = storage.search("KX-410", 10, fail_if_conflicted=True)
    assert len(rows) == 2
    marker = storage.last_conflict_check()
    assert marker["undecided_pairs"][0]["item_ids"] and marker["method"]


def test_no_flag_means_no_error_and_a_reset_marker(lite, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CONFLICT_CHECK_UNSURE", "mark")
    storage.search("KX-410", 10, fail_if_conflicted=True)
    assert storage.last_conflict_check() is not None
    storage.search("KX-410", 10)
    assert storage.last_conflict_check() is None
