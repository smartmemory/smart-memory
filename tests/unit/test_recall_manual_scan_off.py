"""SM-RECALL-MANUAL-SCAN-OFF-1: hook recall opts out of core's per-item scan."""

import inspect
from types import SimpleNamespace

import pytest

from smartmemory_app import recall_format, storage


@pytest.fixture
def recording_mem(monkeypatch):
    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    calls = []

    def search(text, top_k=5, **kwargs):
        calls.append((text, kwargs))
        return []

    mem = SimpleNamespace(
        search=search,
        _graph=SimpleNamespace(search_nodes=lambda _q: []),
    )
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    return calls


def _recall(**kw):
    return storage.recall(
        include_snapshot=False, workspace_id="test", strict=False, **kw
    )


def _search(**kw):
    return storage.search(**kw)


# (branch, runner, expected recorded (text, kwargs) per mem.search call)
ROWS = [
    (
        "relevance query",
        lambda: _recall(query="auth flow", cwd="/p", top_k=3),
        [("auth flow", {"manual_scan": False})],
    ),
    (
        "orient recency + semantic",
        lambda: _recall(cwd="/p", top_k=3),
        [
            ("", {"sort_by": "recency", "manual_scan": False}),
            ("/p", {"manual_scan": False}),
        ],
    ),
    (
        "no-query recency",
        lambda: _recall(top_k=3),
        [("", {"sort_by": "recency", "manual_scan": False})],
    ),
    (
        "non-recall caller (storage.search) does not pass it",
        lambda: _search(query="auth flow", top_k=3),
        [("auth flow", {})],
    ),
]


def test_recall_branches_pass_manual_scan_false(recording_mem):
    for name, run, expected in ROWS:
        recording_mem.clear()
        run()
        assert recording_mem == expected, name


def _lite_or_skip(tmp_path):
    from smartmemory.smart_memory import SmartMemory

    if "manual_scan" not in inspect.signature(SmartMemory.search).parameters:
        pytest.skip("needs smartmemory-core >= 1.5.33 (manual_scan)")
    from smartmemory.tools.factory import create_lite_memory

    try:
        return create_lite_memory(data_dir=str(tmp_path / "lite"), spawn_worker=False)
    except Exception as exc:  # missing local models: no network in tests
        pytest.skip(f"Lite models unavailable: {type(exc).__name__}")


def test_recall_runs_on_real_core_lite(tmp_path, monkeypatch):
    mem = _lite_or_skip(tmp_path)
    monkeypatch.setattr(storage, "get_memory", lambda: mem)
    for kw in ({"query": "banana smoothie"}, {"cwd": "/p"}, {}):
        assert isinstance(_recall(top_k=3, **kw), str)
