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

NOTE_A = (
    "Lena, 3 March 2026: the distributor price list says the KX-410 costs 1,200 EUR."
)
NOTE_B = (
    "From Marco's call with the reseller on 18 April 2026 - KX-410 price is 1,450 EUR."
)


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "JEV_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    mem = create_lite_memory(
        str(tmp_path / "s"),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    for text in (NOTE_A, NOTE_B):
        mem.add(MemoryItem(content=text, memory_type="semantic"))
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    yield mem
    mem.close()


def test_the_flag_is_forwarded_and_the_undecided_error_arrives_as_itself(
    lite, monkeypatch
):
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
    # Fix r2 (adjudication-r1 M2+M3): each row carries the pair on its own conflict field too.
    assert all(row["conflict"]["undecided_with_item_ids"] for row in rows)


def test_no_flag_means_no_error_and_a_reset_marker(lite, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CONFLICT_CHECK_UNSURE", "mark")
    storage.search("KX-410", 10, fail_if_conflicted=True)
    assert storage.last_conflict_check() is not None
    storage.search("KX-410", 10)
    assert storage.last_conflict_check() is None


def test_w4_remote_mode_keeps_the_marker_and_the_rows_conflict(lite, monkeypatch):
    """W4 (fix r2, M3): a marked service envelope through the real remote branch of ``storage.search``.

    The envelope is what the service sends for frozen pair 4: core's rows (``to_dict``) and the top-level marker,
    produced by the real Lite core under ``mark``. It is fed to ``RemoteMemory._request`` in-process (no HTTP).
    """
    from smartmemory_app.remote_backend import RemoteMemory

    monkeypatch.setenv("SMARTMEMORY_CONFLICT_CHECK_UNSURE", "mark")
    hits = lite.search("KX-410", top_k=10, fail_if_conflicted=True)
    envelope = {
        "results": [h.to_dict() for h in hits],
        "conflict_check": hits.diagnostics.conflict_check,
    }
    assert envelope["conflict_check"][
        "undecided_pairs"
    ]  # precondition: the envelope really is marked

    remote = RemoteMemory.__new__(RemoteMemory)  # no __init__: no login, no network
    sent = []
    monkeypatch.setattr(
        remote,
        "_request",
        lambda method, path, **kw: sent.append((method, path, kw)) or envelope,
    )
    monkeypatch.setattr(storage, "get_memory", lambda: remote)
    rows = storage.search("KX-410", 10, fail_if_conflicted=True)
    assert sent and sent[0][2]["json"]["fail_if_conflicted"] is True
    assert len(rows) == 2
    assert (
        storage.last_conflict_check() == envelope["conflict_check"]
    )  # was None before fix r2
    assert all(row["conflict"]["undecided_with_item_ids"] for row in rows)
    envelope.pop("conflict_check")  # the next (unmarked) search resets it
    storage.search("KX-410", 10)
    assert storage.last_conflict_check() is None
