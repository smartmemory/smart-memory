"""Hook capture, injection, and failure contracts for DEMO-CC-UPLIFT-1 T1."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartmemory_app import recall_format, storage
from smartmemory_app.lifecycle import MemoryLifecycle
from smartmemory_app.lifecycle_config import LifecycleConfig


@pytest.fixture(autouse=True)
def isolated_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SMARTMEMORY_HOOK_TRACE", str(tmp_path / "trace.jsonl"))
    monkeypatch.delenv("SMARTMEMORY_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("SMARTMEMORY_RECALL_STRICT", raising=False)
    monkeypatch.delenv("SMARTMEMORY_RECALL_FLOOR", raising=False)
    monkeypatch.setattr(
        "smartmemory.plugins.embedding.create_embeddings", lambda _: [1.0]
    )


def traces(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()
    ]


def memory(name: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(
        item_id=name,
        content=body,
        memory_type="semantic",
        origin="cli:add",
        metadata={},
        confidence=1.0,
        reference=False,
    )


@pytest.mark.parametrize("phase", ["orient", "recall"])
def test_final_trace_once_with_full_payload(
    phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        memory("first", "First fact. " * 25),
        memory("second", "Second fact. " * 30),
        memory("third", "Small fact."),
    ]
    backend = SimpleNamespace(
        search=lambda *args, **kw: rows[: kw["top_k"]], embed=lambda _: [1.0]
    )
    monkeypatch.setattr(storage, "get_memory", lambda: backend)
    lc = MemoryLifecycle(
        "trace-session", LifecycleConfig(orient_budget=160, recall_budget=160)
    )
    payload = (
        lc.orient(str(tmp_path))
        if phase == "orient"
        else lc.recall("Explain the facts", str(tmp_path))
    )
    records = traces(tmp_path)
    assert (
        len(records) == 1
    )  # includes BOTH orient storage calls, not two partial records
    record = records[0]
    assert record["phase"] == phase and record["session_id"] == "trace-session"
    assert record["workspace_id"] == recall_format.derive_workspace_id(str(tmp_path))
    assert record["query"] == (None if phase == "orient" else "Explain the facts")
    assert record["ranked_ids"][:3] == ["first", "second", "third"]
    assert (
        record["injected_ids"]
        == ["first", "third"]
        == recall_format.payload_ids(payload)
    )
    assert record["payload"] == payload
    assert record["payload_tokens"] == (len(payload) + 3) // 4 <= 160
    assert record["error"] is None and record["ts"]
    assert "First fact. " * 24 in payload


@pytest.mark.parametrize("kind", ["gated", "deduped", "empty", "disabled"])
def test_skipped_recall_is_traced(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = Mock(return_value="")
    monkeypatch.setattr(storage, "recall", lookup)
    lc = MemoryLifecycle("skip", LifecycleConfig(enabled=kind != "disabled"))
    prompt = "yes" if kind == "gated" else "Explain the facts"
    if kind == "deduped":
        lc._last_recalled_prompt = prompt
    assert lc.recall(prompt) == ""
    (record,) = traces(tmp_path)
    assert record["skipped_reason"] == kind
    assert record["payload"] == "" and record["injected_ids"] == []
    assert record["error"] is None
    assert lookup.call_count == (1 if kind == "empty" else 0)


@pytest.mark.parametrize("phase", ["orient", "recall"])
def test_search_failure_warns_and_traces(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        storage, "recall", Mock(side_effect=RuntimeError("storage unavailable"))
    )
    lc = MemoryLifecycle("failure")
    assert (lc.orient() if phase == "orient" else lc.recall("Explain the facts")) == ""
    (record,) = traces(tmp_path)
    assert "storage unavailable" in record["error"]
    assert record["payload"] == ""
    assert any(
        r.levelno == logging.WARNING and "lost" in r.message for r in caplog.records
    )


def test_orient_pattern_failure_preserves_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    block = recall_format.format_recall_lines(
        [{"item_id": "a", "content": "A fact."}], 1
    )
    monkeypatch.setattr(
        storage,
        "recall",
        Mock(side_effect=[block, RuntimeError("patterns unavailable")]),
    )
    payload = MemoryLifecycle("patterns").orient(str(tmp_path))
    assert payload == block
    (record,) = traces(tmp_path)
    assert "patterns unavailable" in "; ".join(record["degradations"]) and record[
        "injected_ids"
    ] == ["a"]
    assert "lost patterns context" in caplog.text


@pytest.mark.parametrize("phase", ["observe", "distill", "learn", "persist"])
def test_capture_failure_warns(
    phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(storage, "ingest", Mock(side_effect=RuntimeError("disk full")))
    lc = MemoryLifecycle("capture-failure")
    lc._last_assistant_message = "A summary."
    args = {
        "observe": ("Bash", {}, "done"),
        "distill": ("A response.",),
        "learn": ("Bash", "error"),
        "persist": (),
    }
    if phase == "persist":
        monkeypatch.setattr(
            "smartmemory_app.capture_queue.enqueue",
            Mock(side_effect=OSError("disk full")),
        )
    getattr(lc, phase)(*args[phase])
    if phase == "persist":
        assert "could not start" in caplog.text and "disk full" in caplog.text
        return
    assert (
        f"{phase.title()} ingest failed" in caplog.text and "disk full" in caplog.text
    )


@pytest.mark.parametrize("budget_kind", ["orient", "recall", "formatter"])
def test_budget_keeps_whole_items_and_continues(
    budget_kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    rows = [
        {"item_id": "first", "content": "x" * 250},
        {"item_id": "overflow", "content": "y" * 250},
        {"item_id": "last", "content": "Last complete sentence."},
    ]
    block = recall_format.format_recall_lines(rows, 3)
    lc = MemoryLifecycle(
        "budget", LifecycleConfig(orient_budget=130, recall_budget=130)
    )
    out = (
        lc._format_orient_block(block, "")
        if budget_kind == "orient"
        else lc._trim_to_budget(block)
        if budget_kind == "recall"
        else recall_format.format_recall_lines(rows, 3, budget=130)
    )
    assert "x" * 250 in out and "y" * 250 not in out
    assert recall_format.payload_ids(out) == ["first", "last"]
    assert "overflow for budget" in caplog.text
    assert len(out) <= 520


def test_oversized_item_sentence_boundary_and_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = "Keep this sentence. " + "Unfinished clause " * 100
    out = recall_format.format_recall_lines(
        [{"item_id": "huge", "content": body}], 1, budget=40
    )
    assert "Keep this sentence.…[truncated, mem:huge]" in out
    assert "Unfinished" not in out and len(out) <= 160
    assert "lost full content for huge" in caplog.text


def test_oversized_no_sentence_and_tiny_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = {"item_id": "huge", "content": "x" * 500}
    assert "[mem:huge] …[truncated, mem:huge]" in recall_format.format_recall_lines(
        [row], 1, budget=30
    )
    assert recall_format.format_recall_lines([row], 1, budget=1) == ""


def test_multiline_item_atomic_and_markers() -> None:
    body = "First sentence.\n- embedded bullet\nLast sentence."
    row = {
        "item_id": "multi",
        "content": body,
        "stale": True,
        "confidence": 0.2,
        "memory_type": "semantic",
    }
    full = recall_format.format_recall_lines([row], 1, budget=100)
    assert "- ⚠~[semantic] [mem:multi]" in full
    assert body.replace("\n", "\n  ") in full
    assert recall_format.payload_ids(full) == ["multi"]


@pytest.mark.parametrize(
    "phase", ["orient", "recall", "observe", "learn", "distill", "persist"]
)
@pytest.mark.parametrize("default_dir", [False, True])
def test_hook_script_logs_stderr_and_exits_zero(
    phase: str, default_dir: bool, tmp_path: Path
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "smartmemory"
    stub.write_text('#!/bin/sh\necho "failure-$2" >&2\nexit 23\n')
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", HOME=str(tmp_path))
    if default_dir:
        env.pop("SMARTMEMORY_DATA_DIR", None)
        log_dir = tmp_path / ".smartmemory"
    else:
        log_dir = tmp_path / "new-data"
        env["SMARTMEMORY_DATA_DIR"] = str(log_dir)
    script = (
        Path(__file__).resolve().parents[2]
        / "smartmemory_app"
        / "hooks"
        / f"{phase}.sh"
    )
    result = subprocess.run(
        ["bash", str(script)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert result.returncode == 0

    def wait_for_log_count(expected):
        deadline = time.monotonic() + 5
        while (log_dir / "hooks.log").read_text().count(f"failure-{phase}") < expected:
            assert time.monotonic() < deadline, "Background hook did not log failure"
            time.sleep(0.02)
        assert (log_dir / "hooks.log").read_text().count(f"failure-{phase}") == expected

    wait_for_log_count(1)
    again = subprocess.run(
        ["bash", str(script)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert again.returncode == 0
    wait_for_log_count(2)


@pytest.mark.parametrize("phase", ["learn", "distill", "persist", "observe"])
@pytest.mark.parametrize("transport", ["cli", "api"])
def test_capture_cwd_forwarded(
    phase: str, transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "session_id": "forward",
        "cwd": "/project/a",
        "last_assistant_message": "response",
        "error": "oops",
    }
    method = Mock()
    monkeypatch.setattr(MemoryLifecycle, phase, method)
    if transport == "cli":
        from smartmemory_app.cli import cli

        monkeypatch.setattr(
            "smartmemory_app.cli._lifecycle_via_daemon", lambda *a: None
        )
        monkeypatch.setattr("smartmemory_app.cli._load_lifecycle_toml", lambda: {})
        result = CliRunner().invoke(
            cli, ["lifecycle", phase], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
    else:
        from smartmemory_app.lifecycle_api import lifecycle_router

        monkeypatch.setattr(
            "smartmemory_app.lifecycle_api._load_lifecycle_config", lambda: {}
        )
        app = FastAPI()
        app.include_router(lifecycle_router)
        assert TestClient(app).post(f"/{phase}", json=payload).status_code == 200
    assert method.call_args.kwargs["cwd"] == "/project/a"


def test_trace_io_failure_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = tmp_path / "file"
    bad.write_text("not a directory")
    recall_format._trace(
        phase="recall",
        workspace_id=None,
        cwd=None,
        query="q",
        candidate_count=0,
        emitted=0,
        snapshot_used=False,
        latency_ms=0,
        trace_path=bad / "trace",
    )
    assert "lost injection record" in caplog.text


def test_embedding_fallback_warns_and_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "smartmemory.plugins.embedding.create_embeddings",
        Mock(side_effect=RuntimeError("embedding unavailable")),
    )
    block = recall_format.format_recall_lines([{"item_id": "a", "content": "Fact."}], 1)
    monkeypatch.setattr(storage, "recall", lambda *a, **kw: block)
    lc = MemoryLifecycle("embedding")
    lc._last_injection_embedding = [1.0]
    assert lc.recall("Explain the facts") == block
    (record,) = traces(tmp_path)
    assert "lost topic gating" in "; ".join(
        record["degradations"]
    ) and "lost cached topic embedding" in "; ".join(record["degradations"])
    assert record["error"] is None
    assert "embedding unavailable" in caplog.text


@pytest.mark.parametrize("failing", [False, True])
def test_remote_recall_uses_final_trace(
    failing: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from smartmemory_app.remote_backend import RemoteBackendError, RemoteMemory

    backend = object.__new__(RemoteMemory)
    backend.search = (
        Mock(side_effect=RemoteBackendError("remote offline"))
        if failing
        else Mock(
            return_value=[
                {"item_id": "remote", "content": "Remote fact.", "origin": "cli:add"}
            ]
        )
    )
    monkeypatch.setattr(storage, "get_memory", lambda: backend)
    monkeypatch.setattr(MemoryLifecycle, "_cache_embedding", lambda *args: None)
    payload = MemoryLifecycle("remote").recall("Explain the fact")
    (record,) = traces(tmp_path)
    assert record["payload"] == payload and record["phase"] == "recall"
    if failing:
        assert (
            not payload
            and "remote offline" in record["error"]
            and "lost search context" in caplog.text
        )
    else:
        assert record["injected_ids"] == ["remote"] and record["ranked_ids"] == [
            "remote"
        ]
        assert record["error"] is None


@pytest.mark.parametrize("failure", [False, True])
def test_snapshot_tag_or_failure_in_orient_trace(
    failure: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    snap = memory("snapshot", "Snapshot fact.")
    snap.memory_type = "snapshot"

    def search(*args, **kwargs) -> list:
        if kwargs.get("memory_type") == "snapshot":
            if failure:
                raise RuntimeError("snapshot offline")
            return [snap]
        return []

    monkeypatch.setattr(storage, "get_memory", lambda: SimpleNamespace(search=search))
    payload = MemoryLifecycle("snapshot").orient()
    (record,) = traces(tmp_path)
    assert record["payload"] == payload
    if failure:
        assert (
            not payload
            and "snapshot offline" in "; ".join(record["degradations"])
            and "lost snapshot" in caplog.text
        )
    else:
        assert record["injected_ids"] == ["snapshot"] and record["ranked_ids"] == [
            "snapshot"
        ]
        assert (
            record["snapshot_used"]
            and "[snapshot] [mem:snapshot] Snapshot fact." in payload
        )


def test_orient_disabled_still_traces(tmp_path: Path) -> None:
    assert MemoryLifecycle("disabled", LifecycleConfig(enabled=False)).orient() == ""
    (record,) = traces(tmp_path)
    assert record["phase"] == "orient" and record["skipped_reason"] == "disabled"


def test_orient_patterns_fit_below_old_100_token_gate() -> None:
    lc = MemoryLifecycle("patterns-budget", LifecycleConfig(orient_budget=70))
    context = recall_format.format_recall_lines(
        [{"item_id": "a", "content": "A fact."}], 1
    )
    patterns = recall_format.format_recall_lines(
        [{"item_id": "b", "content": "Use a transaction."}], 1
    )
    out = lc._format_orient_block(context, patterns)
    assert "## Patterns" in out and recall_format.payload_ids(out) == ["a", "b"]
    assert len(out) <= 280


def test_missing_embedding_warns_and_is_traced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "smartmemory.plugins.embedding.create_embeddings", lambda _: None
    )
    block = recall_format.format_recall_lines([{"item_id": "a", "content": "Fact."}], 1)
    monkeypatch.setattr(storage, "recall", lambda *a, **kw: block)
    lc = MemoryLifecycle("none-embedding")
    lc._last_injection_embedding = [1.0]
    assert lc.recall("Explain the fact") == block
    (record,) = traces(tmp_path)
    assert (
        "embedding unavailable" in "; ".join(record["degradations"])
        and "lost topic gating" in caplog.text
    )


def test_remote_snapshot_error_response_is_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from smartmemory_app.remote_backend import RemoteMemory

    backend = object.__new__(RemoteMemory)
    backend._request = Mock(return_value={"error": "snapshot request failed"})
    backend.search = Mock(
        return_value=[{"item_id": "a", "content": "Still useful.", "origin": "cli:add"}]
    )
    monkeypatch.setattr(storage, "get_memory", lambda: backend)
    out = MemoryLifecycle("remote-snapshot").orient()
    (record,) = traces(tmp_path)
    assert "Still useful." in out
    assert (
        "snapshot request failed" in "; ".join(record["degradations"])
        and "lost remote snapshot" in caplog.text
    )


def test_trace_rotation_preserves_previous_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recall_format, "TRACE_MAX_BYTES", 1)
    lc = MemoryLifecycle("rotation", LifecycleConfig(enabled=False))
    lc.orient()
    previous = (tmp_path / "trace.jsonl").read_text()
    lc.recall("ignored")
    assert (tmp_path / "trace.jsonl.1").read_text() == previous
    assert traces(tmp_path)[0]["phase"] == "recall"


def test_trace_rotation_failure_still_appends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lc = MemoryLifecycle("rotation", LifecycleConfig(enabled=False))
    lc.orient()
    monkeypatch.setattr(recall_format, "TRACE_MAX_BYTES", 1)
    monkeypatch.setattr(Path, "rename", Mock(side_effect=OSError("rotation denied")))
    lc.recall("ignored")
    assert len(traces(tmp_path)) == 2
    assert "lost rotation" in caplog.text


def test_workspace_fallback_warns_and_keeps_stable_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        recall_format.subprocess, "run", Mock(side_effect=FileNotFoundError("no git"))
    )
    first = recall_format.derive_workspace_id(str(tmp_path))
    assert first == recall_format.derive_workspace_id(str(tmp_path))
    assert first.startswith("ws_") and "lost git-root resolution" in caplog.text
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "pinned")
    caplog.clear()
    assert recall_format.derive_workspace_id(str(tmp_path)) == "pinned"
    assert not caplog.records


def test_topic_gate_uses_embedding_api_and_saves_json(tmp_path, monkeypatch):
    import numpy as np

    vectors = {
        "first topic": [1.0, 0.0],
        "same topic": [0.99, 0.01],
        "new topic": [0.0, 1.0],
    }
    monkeypatch.setattr(
        "smartmemory.plugins.embedding.create_embeddings",
        lambda text: np.array(vectors[text]),
    )
    block = recall_format.format_recall_lines([{"item_id": "a", "content": "Fact."}], 1)
    lookup = Mock(return_value=block)
    monkeypatch.setattr(storage, "recall", lookup)
    assert MemoryLifecycle("topics").recall("first topic") == block
    assert MemoryLifecycle("topics").recall("same topic") == ""
    assert MemoryLifecycle("topics").recall("new topic") == block
    assert lookup.call_count == 2
    assert all(record["error"] is None for record in traces(tmp_path))
