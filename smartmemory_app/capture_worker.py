"""Detached, serial transcript importer. The OS releases worker.lock on a crash."""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
from pathlib import Path

from filelock import FileLock, Timeout

from smartmemory_app import capture_queue as queue
from smartmemory_app import capture_slices as slices
from smartmemory_app.capture_slices import rss_mb

log = logging.getLogger(__name__)


class CaptureSkipped(Exception):
    """The item is deliberately not (fully) captured. Recorded as status ``skipped``."""


def _memory():
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    # Explicit offline capture uses the existing Tier 1 pipeline, not another
    # extractor. Production capture is pinned to Groq, never subscription billing.
    offline = os.environ.get("SMARTMEMORY_CAPTURE_OFFLINE") == "1"
    if not offline:
        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError("Transcript capture requires GROQ_API_KEY")
        os.environ["LLM_PROVIDER"] = "groq"
        os.environ["LLM_API_KEY"] = os.environ["GROQ_API_KEY"]
        os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"
        os.environ["SMARTMEMORY_LLM_MODEL"] = "openai/gpt-oss-120b"
    os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = "local"
    return create_lite_memory(
        data_dir=str(queue.capture_dir().parent),
        pipeline_profile=PipelineConfig.tier1() if offline else PipelineConfig.lite(),
    )


def import_capture(job: dict, tail: slices.Slice | None = None) -> dict:
    """Import a transcript, or only the ``tail`` byte range of it (an incremental capture)."""
    from smartmemory.importers.agent_transcript import (
        iter_transcript_ingest_kwargs,
        parse_transcript,
    )

    conversation, stats = parse_transcript(
        tail.path if tail else job["transcript_path"], min_turns=1
    )
    if conversation is None:
        if tail is not None and tail.start > 0:
            return {"empty_tail": True}  # Appended records with no spoken turns.
        raise ValueError("Transcript contains no conversational turns")
    kwargs = next(iter_transcript_ingest_kwargs([(conversation, "claude_code", stats)]))
    if tail is not None:
        # Natural keys are store-wide per session, so each byte range needs its own.
        kwargs["context"]["transcript_path"] = job["transcript_path"]
        kwargs["context"]["source_path"] += f"@{tail.start}-{tail.end}"
    kwargs["context"].update(
        workspace_id=job["workspace_id"], session_id=job["session_id"]
    )
    kwargs["context"].setdefault("cwd", job.get("cwd"))
    kwargs["write_context"] = {"workspace_id": job["workspace_id"]}
    kwargs["max_concurrent"] = 1
    fingerprint = hashlib.sha256(
        json.dumps(kwargs, sort_keys=True).encode()
    ).hexdigest()
    cached = None
    for previous in reversed(queue.jobs()):
        if previous["status"] == "done" and previous.get("fingerprint") == fingerprint:
            cached = {
                "fingerprint": fingerprint,
                "item_ids": previous.get("item_ids", []),
                "unchanged": True,
            }
            if previous.get("lessons_complete") or (
                previous.get("lesson_ids") and not previous.get("degradation")
            ):
                return {
                    **cached,
                    **{
                        key: previous[key]
                        for key in (
                            "lesson_ids",
                            "lesson_transitions",
                            "lessons_complete",
                            "degradation",
                        )
                        if key in previous
                    },
                }
            break
    mem = _memory()
    try:
        from smartmemory_app.session_lessons import capture_lessons

        def lessons_for(item_ids):
            return capture_lessons(
                mem,
                job,
                kwargs["turns"],
                item_ids,
                (kwargs.get("session_dates") or [None])[0],
            )

        if cached is not None:
            # T2 receipts and previous degradations still need lessons, but the
            # already-imported transcript must not be written again.
            return {**cached, **lessons_for(cached["item_ids"])}
        # SQLite natural keys are store-wide. Refuse to replace another
        # workspace's transcript instead of silently changing its ownership.
        from smartmemory.conversation.bulk_ingest import ConversationChunker

        chunks = ConversationChunker().chunk(
            kwargs["turns"], kwargs["session_boundaries"], kwargs["session_dates"]
        )
        for index in range(len(chunks)):
            existing = mem._graph.find_by_source_path(
                "import:claude_code", f"{kwargs['context']['source_path']}#chunk{index}"
            )
            if (
                existing is not None
                and existing.metadata.get("workspace_id") != job["workspace_id"]
            ):
                raise ValueError("Transcript natural key belongs to another workspace")
        result = mem.ingest_conversation_sync(**kwargs)
        if result.chunks_failed:
            raise RuntimeError(
                ", ".join(chunk.error for chunk in result.chunk_results if chunk.error)
            )
        lessons = lessons_for(
            [chunk.item_id for chunk in result.chunk_results if chunk.item_id]
        )
        return {
            **lessons,
            "fingerprint": fingerprint,
            "chunks_ingested": result.chunks_ingested,
            "item_ids": [chunk.item_id for chunk in result.chunk_results],
        }
    finally:
        mem.close()


def _check_rss() -> None:
    limit, used = slices.max_rss_mb(), rss_mb()
    if used is not None and used > limit:
        raise CaptureSkipped(
            f"worker RSS {used:.0f} MB over CAPTURE_MAX_RSS_MB={limit:g}"
        )


def _merge(receipts: list[dict]) -> dict:
    merged = {**receipts[-1]}
    merged["item_ids"] = [i for r in receipts for i in r.get("item_ids", [])]
    if any("lesson_ids" in r for r in receipts):
        merged["lesson_ids"] = [i for r in receipts for i in r.get("lesson_ids", [])]
    degraded = [r["degradation"] for r in receipts if r.get("degradation")]
    if degraded:
        merged["degradation"] = " / ".join(degraded)
    merged["slices"] = len(receipts)
    return merged


_RECEIPT_LESSON_KEYS = (
    "lesson_ids",
    "lesson_transitions",
    "lessons_complete",
    "degradation",
)


def _last_done(path: str) -> dict:
    for previous in reversed(queue.jobs()):
        if previous["status"] == "done" and previous.get("transcript_path") == path:
            return previous
    return {}


def capture_transcript(job: dict) -> dict:
    """Capture only what the transcript gained since the stored offset."""
    path = job["transcript_path"]
    try:
        size = Path(path).stat().st_size
    except (OSError, TypeError):
        return import_capture(job)  # Fails the usual way for a missing transcript.
    root, cap = queue.capture_dir(), slices.max_bytes()
    start = slices.read_offset(root, path)
    if start is None:
        if size > cap:
            log.warning(
                "Transcript %s is %d bytes, over CAPTURE_MAX_MB, skipped. "
                "Later captures start at byte %d",
                path,
                size,
                size,
            )
            slices.write_offset(root, path, size)
            raise CaptureSkipped(
                f"over CAPTURE_MAX_MB: {size} bytes, offset set to {size}"
            )
        start = 0
    elif size < start:
        log.warning(
            "Transcript %s shrank from %d to %d bytes, capturing from the start",
            path,
            start,
            size,
        )
        start = 0
    if start and start == size:
        previous = _last_done(path)
        if (  # A whole-file capture that lost its lessons is retried, as before.
            size <= cap
            and previous.get("tail_start") == 0
            and previous.get("tail_end") == size
            and previous.get("degradation")
            and not previous.get("lessons_complete")
        ):
            return {**import_capture(job), "tail_start": 0, "tail_end": size}
        return {
            "unchanged": True,
            "no_new_bytes": True,
            "item_ids": previous.get("item_ids", []),
            **{k: previous[k] for k in _RECEIPT_LESSON_KEYS if k in previous},
            "tail_start": start,
            "tail_end": size,
        }
    receipts, end = [], start
    if start == 0 and size <= cap:
        _check_rss()
        receipts.append(import_capture(job))
        slices.write_offset(root, path, size)
        end = size
    else:
        for piece in slices.iter_slices(path, start, size, cap, root):
            _check_rss()
            receipts.append(import_capture(job, piece))
            slices.write_offset(root, path, piece.end)  # After success only.
            end = piece.end
            gc.collect()
    return {**_merge(receipts), "tail_start": start, "tail_end": end}


def _pending(root) -> list[dict]:
    return [j for j in queue.jobs(root) if j["status"] in {"queued", "running"}]


def _drain(root) -> None:
    while pending := _pending(root):
        job = queue.transition(pending[0], "running")
        try:
            receipt = capture_transcript(job)
        except CaptureSkipped as exc:
            log.warning("Transcript capture %s skipped: %s", job["capture_id"], exc)
            queue.transition(job, "skipped", reason=str(exc))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning("Transcript capture %s failed: %s", job["capture_id"], message)
            queue.transition(job, "error", error=message)
        else:
            queue.transition(job, "done", **receipt)


def run() -> None:
    root = queue.capture_dir()
    # One holder drains the queue. A worker that cannot take the lock at once exits:
    # the holder re-scans after releasing it, so an enqueue racing its last scan is
    # picked up by the holder (or by whoever takes the lock next), never stranded.
    lock = FileLock(str(root / "worker.lock"), timeout=0)
    while True:
        try:
            lock.acquire()
        except Timeout:
            log.info("Capture worker lock is held, the holder drains the queue")
            return
        try:
            _drain(root)
        finally:
            lock.release()
        if not _pending(root):
            return


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run()
