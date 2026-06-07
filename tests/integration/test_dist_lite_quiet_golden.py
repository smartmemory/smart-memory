"""DIST-LITE-QUIET-1 — golden FREE-tier first-impression flow.

Acceptance criterion (plan.md): capturing stderr/warnings + logs over a fresh
local ingest()+search() shows NONE of the four noise lines that made the
zero-account viral path read as "broken":

  1. UserWarning: No API key found …            (config.get_api_key, remote-only now)
  2. PatternStore.load returned legacy 2-tuples  (DEBUG on local store now)
  3. PatternStore … has no load_conflict_map     (DEBUG on local store now)
  4. No origin set … storing with origin='unknown' (origin threaded now)

Real local backend (SQLite + usearch). No Docker. No mocks.
"""

from __future__ import annotations

import logging
import warnings

import pytest


_NOISE_SUBSTRINGS = [
    "No API key found",
    "legacy 2-tuples",
    "load_conflict_map",
    "No origin set",
]


@pytest.fixture()
def fresh_local_storage(tmp_path, monkeypatch):
    """Point smartmemory_app.storage at a fresh temp data dir in local mode."""
    import smartmemory_app.storage as storage

    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    # Reset the module singletons so get_memory() rebuilds against tmp_path.
    monkeypatch.setattr(storage, "_memory", None, raising=False)
    monkeypatch.setattr(storage, "_data_path", None, raising=False)
    storage.get_memory(data_dir=str(tmp_path))
    return storage


def test_free_path_first_run_is_quiet(fresh_local_storage, caplog):
    storage = fresh_local_storage

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with caplog.at_level(logging.WARNING):
            item_id = storage.ingest(
                "Alice leads Project Atlas.",
                memory_type="episodic",
                origin="cli:add",
            )
            storage.search("Atlas", 5)

    assert item_id, "ingest returned no id"

    warning_text = [str(w.message) for w in caught]
    warning_text += [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    offenders = [
        m for m in warning_text
        if any(sub in m for sub in _NOISE_SUBSTRINGS)
    ]
    assert not offenders, (
        "FREE/local first-run surfaced noise the user cannot act on:\n  "
        + "\n  ".join(offenders)
    )


def test_free_path_ingest_origin_attributed(fresh_local_storage):
    """The threaded origin reaches the stored item (cli:add, tier 1)."""
    storage = fresh_local_storage
    item_id = storage.ingest("Bob ships the daemon.", memory_type="episodic", origin="cli:add")
    if isinstance(item_id, dict):
        item_id = item_id.get("item_id")

    mem = storage.get_memory()
    stored = mem.get(item_id)
    assert stored is not None
    assert getattr(stored, "origin", None) == "cli:add", (
        f"expected origin 'cli:add', got {getattr(stored, 'origin', None)!r}"
    )


def test_user_properties_cannot_override_producer_origin(fresh_local_storage):
    """Forcing function (Codex review): origin drives tier visibility AND precedence
    guards, so a user-supplied properties['origin'] must NOT override the producer's
    declared origin (else a caller could claim a privileged origin like import:vault).
    """
    storage = fresh_local_storage
    item_id = storage.ingest(
        "Privilege escalation attempt.",
        memory_type="episodic",
        origin="cli:add",
        properties={"origin": "import:vault"},
    )
    if isinstance(item_id, dict):
        item_id = item_id.get("item_id")

    stored = storage.get_memory().get(item_id)
    assert stored is not None
    assert getattr(stored, "origin", None) == "cli:add", (
        f"user properties must not override producer origin; got "
        f"{getattr(stored, 'origin', None)!r} (expected the producer's 'cli:add')"
    )


# ---------------------------------------------------------------------------
# Daemon /memory/ingest is producer-NEUTRAL (Codex review, 2026-06-07):
# each producer declares its own origin; the endpoint must not blanket-label
# every caller as cli:add.
# ---------------------------------------------------------------------------

def test_daemon_ingest_is_producer_neutral(monkeypatch):
    import smartmemory_app.local_api as local_api

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    captured = {}

    def _fake_ingest(content, memory_type="episodic", sync=True, properties=None, origin=None):
        captured["origin"] = origin
        return "id-x"

    import smartmemory_app.storage as storage
    monkeypatch.setattr(storage, "ingest", _fake_ingest)

    # CLI declares its producer → cli:add threads through.
    captured.clear()
    local_api.ingest_endpoint(local_api.IngestRequest(content="x", context={"origin": "cli:add"}))
    assert captured["origin"] == "cli:add"

    # A generic caller that omits origin is NOT mislabeled cli:add (stays neutral/None).
    captured.clear()
    local_api.ingest_endpoint(local_api.IngestRequest(content="x"))
    assert captured["origin"] is None, (
        f"daemon must be producer-neutral; got origin={captured['origin']!r}"
    )
