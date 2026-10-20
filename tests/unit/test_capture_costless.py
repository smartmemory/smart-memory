"""MEM-SIZE-1: one real capture run on the costless path (temp Lite store, keys blank, no LLM, no network).

The reviewer probe at the wrapper level: a 36-turn transcript (3 chunks) with a unique sentence planted in the middle
of chunk 2. ``capture_transcript`` stores summaries plus pointers, never the raw text: the sentence is in no file or
SQLite row of the store, every chunk carries a pointer to the real transcript, and rehydrating chunk 2's pointer
returns the sentence. Session lessons (an LLM call) are skipped, not degraded. The fingerprint is computed exactly as
before the unit (the pointer is passed outside the fingerprinted kwargs).
"""

import hashlib
import json
import logging
import socket
import sqlite3
from pathlib import Path

import pytest

from smartmemory_app import capture_queue as queue
from smartmemory_app import capture_worker as worker

PLANTED = "the vermilion abacus whispers its riddles to the sleepy harbor"
OPENING = "We need a migration plan for the Orion billing cluster before Friday."
PROVIDER_VARS = (
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_API_KEY",
    "LLM_PROVIDER",
    "SMARTMEMORY_LLM_MODEL",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "SMARTMEMORY_INGEST_AI_SUMMARY",
)
JOB = {
    "capture_id": "cap-mem-size-1",
    "session_id": "sess-mem-size-1",
    "workspace_id": "ws-mem-size-1",
    "cwd": "/repo/widget",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Online capture with the opt-in off: the default the hold release runs on.
    monkeypatch.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
    for name in ("SMARTMEMORY_EMBEDDING_PROVIDER", "LLM_PROVIDER"):
        monkeypatch.setenv(
            name, ""
        )  # _memory() writes os.environ: setenv first so teardown restores.
    monkeypatch.setattr(queue, "spawn_worker", lambda: None)
    monkeypatch.setattr(queue, "spawn_successor", lambda: None)
    monkeypatch.setattr(worker, "rss_mb", lambda: 10.0)


@pytest.fixture
def no_llm_no_network(monkeypatch):
    """Every LLM client constructor and every non-local connect fails loudly and is recorded.

    Copied from core ``tests/integration/test_capture_admission.py``. The Claude Agent SDK route is reported
    AVAILABLE, so the costless profile, not a keyless machine, is what keeps every LLM call out. Detached worker
    spawns are recorded too: deferred boxes run in this process via ``run_pending`` instead.
    """
    calls: list[str] = []
    for var in PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "smartmemory.utils.llm._claude_agent_sdk_available", lambda: True
    )

    def forbid(name):
        def _raise(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not run on the costless capture path")

        return _raise

    monkeypatch.setattr("smartmemory.utils.llm.call_llm", forbid("call_llm"))
    for target in (
        "smartmemory.utils.llm_client.openai_chat.call_openai_chat",
        "smartmemory.utils.llm_client.openai.call_openai_responses",
        "smartmemory.utils.llm_client.litellm.call_litellm",
        "smartmemory_app.session_lessons.capture_lessons",
    ):
        monkeypatch.setattr(target, forbid(target.rsplit(".", 1)[-1]))
    for module, cls in (("openai", "OpenAI"), ("anthropic", "Anthropic")):
        try:
            klass = getattr(__import__(module), cls)
        except (ImportError, AttributeError):
            continue
        monkeypatch.setattr(klass, "__init__", forbid(f"{module}.{cls}"))
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            calls.append(f"connect {host}")
            raise AssertionError(
                f"network connect to {host} on the costless capture path"
            )
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    spawned: list[str] = []
    monkeypatch.setattr(
        "smartmemory.pipeline.work_graph.spawn.spawn_worker_if_idle",
        lambda *a, **k: spawned.append("spawn"),
    )
    return calls


def write_transcript(folder: Path) -> Path:
    """36 turns, no timestamps, so the chunker splits by count: [0,15) [15,30) [30,36)."""
    lines = []
    for n in range(36):
        role = "user" if n % 2 == 0 else "assistant"
        text = f"Turn {n} discusses step {n} of the rollout checklist in some detail."
        if n == 0:
            text = OPENING + " " + text
        if n == 22:  # the middle of chunk 2, never its first or last turn
            text = f"Some preamble for turn 22 goes first. Then {PLANTED}. And a closing remark follows."
        record = {
            "type": role,
            "sessionId": JOB["session_id"],
            "cwd": JOB["cwd"],
            "uuid": f"u-{n}",
            "message": {"role": role, "content": text},
        }
        lines.append(json.dumps(record))
    path = folder / "-repo-widget" / f"{JOB['session_id']}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def pre_change_fingerprint(job: dict) -> str:
    """The fingerprint as ``import_capture`` computed it before MEM-SIZE-1 (wrapper 323827d, whole-file arm)."""
    from smartmemory.importers.agent_transcript import (
        iter_transcript_ingest_kwargs,
        parse_transcript,
    )

    conversation, stats = parse_transcript(job["transcript_path"], min_turns=1)
    kwargs = next(iter_transcript_ingest_kwargs([(conversation, "claude_code", stats)]))
    kwargs["context"].update(
        workspace_id=job["workspace_id"], session_id=job["session_id"]
    )
    kwargs["context"].setdefault("cwd", job.get("cwd"))
    kwargs["write_context"] = {"workspace_id": job["workspace_id"]}
    kwargs["max_concurrent"] = 1
    return hashlib.sha256(json.dumps(kwargs, sort_keys=True).encode()).hexdigest()


def files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if needle.encode() in path.read_bytes():
            hits.append(str(path.relative_to(root)))
        if path.suffix == ".db":
            with sqlite3.connect(path) as conn:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                for table in tables:
                    try:
                        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                    except sqlite3.DatabaseError:
                        continue
                    if any(
                        needle
                        in (
                            v.decode("utf-8", "ignore")
                            if isinstance(v, bytes)
                            else str(v)
                        )
                        for row in rows
                        for v in row
                    ):
                        hits.append(f"{path.relative_to(root)}:{table}")
    return hits


def test_costless_capture_stores_pointers_and_skips_lessons(
    tmp_path, no_llm_no_network, caplog
):
    from smartmemory.ingest import STORE_SUMMARY_POINTER, rehydrate
    from smartmemory.tools.factory import create_lite_memory

    transcript = write_transcript(tmp_path / "projects")
    job = {**JOB, "transcript_path": str(transcript)}
    with caplog.at_level(logging.WARNING, logger="smartmemory_app.capture_worker"):
        receipt = worker.capture_transcript(job)

    assert not no_llm_no_network, no_llm_no_network
    assert receipt["lessons_skipped"] is True and receipt["lesson_ids"] == []
    assert "degradation" not in receipt
    assert receipt["chunks_ingested"] == 3 and len(receipt["item_ids"]) == 3
    assert (
        receipt["tail_start"] == 0 and receipt["tail_end"] == transcript.stat().st_size
    )
    assert receipt["fingerprint"] == pre_change_fingerprint(job)
    assert "session lessons skipped (costless capture" in caplog.text

    data_dir = queue.capture_dir().parent
    assert files_containing(data_dir, PLANTED) == []
    assert files_containing(data_dir, "vermilion abacus") == []
    with create_lite_memory(str(data_dir), spawn_worker=False) as memory:
        items = [memory.get(item_id) for item_id in receipt["item_ids"]]
    assert [item.metadata["source_path"].rsplit("#", 1)[1] for item in items] == [
        "chunk0",
        "chunk1",
        "chunk2",
    ]
    for item in items:
        assert item.metadata["store_mode"] == STORE_SUMMARY_POINTER
        assert PLANTED not in item.content
        pointer = item.metadata["pointer"]
        assert (
            pointer["source_path"] == str(transcript) and pointer["refetchable"] is True
        )
        assert pointer["range"]["byte_start"] == 0
        assert pointer["range"]["byte_end"] == transcript.stat().st_size
    back = rehydrate(items[1].metadata["pointer"])
    assert back.status == "ok" and PLANTED in back.text


def test_recapture_returns_the_cached_receipt_without_lessons(
    tmp_path, no_llm_no_network
):
    """A re-capture of the same bytes hits the fingerprint cache: a skipped-lessons receipt is complete."""
    transcript = write_transcript(tmp_path / "projects")
    job = {**JOB, "transcript_path": str(transcript)}
    first = worker.import_capture(job, byte_range=(0, transcript.stat().st_size))
    queue.transition(job, "done", **first)  # the worker's own done record
    again = worker.import_capture(job, byte_range=(0, transcript.stat().st_size))
    assert not no_llm_no_network, no_llm_no_network
    assert again["unchanged"] is True and again["lessons_skipped"] is True
    assert again["item_ids"] == first["item_ids"]
