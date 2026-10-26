"""Detached, serial transcript importer. The OS releases worker.lock on a crash."""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
from contextlib import closing
from pathlib import Path

from filelock import FileLock, Timeout

from smartmemory_app import capture_hold as hold
from smartmemory_app import capture_queue as queue
from smartmemory_app import capture_slices as slices
from smartmemory_app.capture_slices import rss_mb

log = logging.getLogger(__name__)


def _ai_opted_in() -> bool:
    """MEM-SIZE-1: AI on the capture path is a per-source opt-in, default OFF (owner: "captures too").

    Session lessons run only on the opt-in (offline capture then records them as degraded, as before). The Groq pin
    and the LLM pipeline profile need the opt-in AND an online capture (``_memory``).
    """
    from smartmemory.ingest.admission import CLAUDE_CODE_TRANSCRIPT, ai_opted_in

    return ai_opted_in(CLAUDE_CODE_TRANSCRIPT)


def _memory():
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    # Capture is costless by default: rules extraction, local embeddings, keyword index, local enrichers, no LLM
    # route at all (``llm_allowed=False``). Only the AI opt-in pins Groq, never subscription billing.
    ai = _ai_opted_in() and os.environ.get("SMARTMEMORY_CAPTURE_OFFLINE") != "1"
    if ai:
        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError(
                "AI transcript capture (SMARTMEMORY_INGEST_AI_SUMMARY) requires GROQ_API_KEY"
            )
        os.environ["LLM_PROVIDER"] = "groq"
        os.environ["LLM_API_KEY"] = os.environ["GROQ_API_KEY"]
        os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"
        os.environ["SMARTMEMORY_LLM_MODEL"] = "openai/gpt-oss-120b"
    os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = "local"
    return create_lite_memory(
        data_dir=str(queue.capture_dir().parent),
        pipeline_profile=PipelineConfig.lite() if ai else PipelineConfig.costless(),
    )


def _source_ref(
    job: dict, tail: slices.Slice | None, byte_range: tuple[int, int] | None
):
    """The pointer target for this capture: the real transcript and the byte range parsed (MEM-SIZE-1)."""
    from datetime import datetime, timezone

    from smartmemory.ingest import SourceRef
    from smartmemory.ingest.admission import CLAUDE_CODE_TRANSCRIPT

    if tail is not None:
        start, end = tail.start, tail.end
    elif byte_range is not None:
        start, end = byte_range
    else:
        start, end = 0, Path(job["transcript_path"]).stat().st_size
    return SourceRef(
        kind=CLAUDE_CODE_TRANSCRIPT,
        source_path=str(job["transcript_path"]),
        byte_start=start,
        byte_end=end,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        refetchable=True,
    )


def import_capture(
    job: dict,
    tail: slices.Slice | None = None,
    byte_range: tuple[int, int] | None = None,
) -> dict:
    """Import a transcript, or only the ``tail`` byte range of it (an incremental capture).

    Each chunk is stored as a summary plus a pointer to ``byte_range`` (or the tail's range) of the transcript, never
    as raw text (MEM-SIZE-1). The pointer is passed outside the fingerprinted kwargs, so the fingerprint is unchanged.
    """
    from smartmemory.importers.agent_transcript import (
        iter_transcript_ingest_kwargs,
        parse_transcript,
    )

    conversation, stats = parse_transcript(
        tail.path if tail else job["transcript_path"], min_turns=1
    )
    if conversation is None:
        if tail is not None:
            return {"empty_tail": True}  # A byte range with no spoken turns.
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
            if (
                previous.get("lessons_complete")
                or (previous.get("lesson_ids") and not previous.get("degradation"))
                or (previous.get("lessons_skipped") and not _ai_opted_in())
            ):
                return {
                    **cached,
                    **{
                        key: previous[key]
                        for key in (
                            "lesson_ids",
                            "lesson_transitions",
                            "lessons_complete",
                            "lessons_skipped",
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
            if not _ai_opted_in():
                # Session lessons are an LLM call: skipped on the costless path. Not a degradation, so the
                # whole-file retry never re-runs it.
                log.warning(
                    "capture %s: session lessons skipped (costless capture because AI opt-in is off)",
                    job.get("capture_id") or job.get("session_id"),
                )
                return {"lesson_ids": [], "lessons_skipped": True}
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
        result = mem.ingest_conversation_sync(
            **kwargs, source_ref=_source_ref(job, tail, byte_range)
        )
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


def _over_rss() -> str | None:
    limit, used = slices.max_rss_mb(), rss_mb()
    if used is not None and used > limit:
        return f"worker RSS {used:.0f} MB over CAPTURE_MAX_RSS_MB={limit:g}"
    return None


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
    "lessons_skipped",
    "degradation",
)


def _last_done(path: str) -> dict:
    for previous in reversed(queue.jobs()):
        if previous["status"] == "done" and previous.get("transcript_path") == path:
            return previous
    return {}


def capture_transcript(job: dict) -> dict:
    """Capture what the transcript gained since the stored offset, one slice per job.

    A range over the cap is captured one slice at a time: after a slice, a continuation
    is queued for the same path behind every other waiting session, so one huge file
    never starves the queue and never holds more than one slice in memory.
    """
    path = job["transcript_path"]
    try:
        size = Path(path).stat().st_size
    except (OSError, TypeError):
        return import_capture(job)  # Fails the usual way for a missing transcript.
    root, cap = queue.capture_dir(), slices.max_bytes()
    start = slices.read_offset(root, path)
    if start is None:
        start = 0  # Never captured: from the first byte, however large.
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
            return {
                **import_capture(job, byte_range=(0, size)),
                "tail_start": 0,
                "tail_end": size,
            }
        return {
            "unchanged": True,
            "no_new_bytes": True,
            "item_ids": previous.get("item_ids", []),
            **{k: previous[k] for k in _RECEIPT_LESSON_KEYS if k in previous},
            "tail_start": start,
            "tail_end": size,
        }
    if start == 0 and size <= cap:
        receipt = import_capture(job, byte_range=(0, size))
        slices.write_offset(root, path, size)
        return {**_merge([receipt]), "tail_start": 0, "tail_end": size}
    with closing(slices.iter_slices(path, start, size, cap, root)) as pieces:
        piece = next(pieces, None)
        if piece is None:  # The file shrank to the offset under us.
            return {"unchanged": True, "no_new_bytes": True, "item_ids": []}
        receipt = import_capture(job, piece)
        slices.write_offset(root, path, piece.end)  # After success only.
    gc.collect()
    receipt = {**_merge([receipt]), "tail_start": start, "tail_end": piece.end}
    if piece.end < size:
        nxt = queue.enqueue(
            job["session_id"],
            path,
            job["workspace_id"],
            job.get("cwd"),
            force=queue.forced(job),
        )
        log.warning(
            "Transcript %s captured to byte %d of %d, the rest is queued as %s",
            path,
            piece.end,
            size,
            nxt["capture_id"],
        )
        receipt.update(continued_by=nxt["capture_id"], bytes_left=size - piece.end)
    return receipt


def _pending(root) -> list[dict]:
    return [j for j in queue.jobs(root) if j["status"] in {"queued", "running"}]


def _drain(root, fresh: bool = True) -> str | None:
    """Drain FIFO. Return the reason when RSS passed the limit before an item.

    The item is then left ``queued`` for a fresh process (memory is never given back
    between items). A fresh process always takes its first item, so recycling ends.
    """
    while pending := _pending(root):
        if hold.hold_on():
            # Before anything is imported or read: park every pending event and stop,
            # except the ones an operator released with --force.
            for job in pending:
                if not queue.forced(job):
                    queue.hold_event(job, hold.HELD, f"{hold.HOLD_ENV} is on")
            pending = [job for job in pending if queue.forced(job)]
            if not pending:
                return None
        job = pending[0]
        if (mb := hold.over_limit(job.get("transcript_path"))) is not None:
            if not queue.forced(job):
                queue.hold_event(
                    job,
                    hold.LARGE,
                    f"transcript is {mb:.0f} MB, over {hold.OVER_ENV}={hold.over_mb():g}",
                )
                continue
            log.warning(
                "Capture %s for session %s was released with --force, capturing its "
                "%.0f MB transcript past %s=%g",
                job["capture_id"],
                job.get("session_id"),
                mb,
                hold.OVER_ENV,
                hold.over_mb(),
            )
        if not fresh and (reason := _over_rss()):
            return reason
        fresh = False
        job = queue.transition(job, "running")
        try:
            receipt = capture_transcript(job)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.warning("Transcript capture %s failed: %s", job["capture_id"], message)
            queue.transition(job, "error", error=message)
        else:
            queue.transition(job, "done", **receipt)
    return None


def run() -> None:
    root = queue.capture_dir()
    # One holder drains the queue. A worker that cannot take the lock at once exits:
    # the holder re-scans after releasing it, so an enqueue racing its last scan is
    # picked up by the holder (or by whoever takes the lock next), never stranded.
    lock = FileLock(str(root / "worker.lock"), timeout=0)
    fresh = True
    while True:
        try:
            lock.acquire()
        except Timeout:
            log.info("Capture worker lock is held, the holder drains the queue")
            return
        try:
            recycle = _drain(root, fresh)
        finally:
            lock.release()
        fresh = False
        if recycle:
            # After the release: the successor's timeout=0 lock attempt must succeed.
            log.warning("Capture worker recycles itself: %s", recycle)
            try:
                queue.spawn_successor()
            except OSError as exc:
                log.warning(
                    "Capture worker could not start its successor, the queue waits "
                    "for the next SessionEnd: %s",
                    exc,
                )
            return
        if not _pending(root):
            return


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run()
