"""Detached, serial transcript importer. The OS releases worker.lock on a crash."""

from __future__ import annotations

import hashlib
import json
import logging
import os

from filelock import FileLock

from smartmemory_app import capture_queue as queue

log = logging.getLogger(__name__)


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


def import_capture(job: dict) -> dict:
    from smartmemory.importers.agent_transcript import (
        iter_transcript_ingest_kwargs,
        parse_transcript,
    )

    conversation, stats = parse_transcript(job["transcript_path"], min_turns=1)
    if conversation is None:
        raise ValueError("Transcript contains no conversational turns")
    kwargs = next(iter_transcript_ingest_kwargs([(conversation, "claude_code", stats)]))
    kwargs["context"].update(
        workspace_id=job["workspace_id"], session_id=job["session_id"]
    )
    kwargs["context"].setdefault("cwd", job.get("cwd"))
    kwargs["write_context"] = {"workspace_id": job["workspace_id"]}
    kwargs["max_concurrent"] = 1
    fingerprint = hashlib.sha256(
        json.dumps(kwargs, sort_keys=True).encode()
    ).hexdigest()
    for previous in reversed(queue.jobs()):
        if previous["status"] == "done" and previous.get("fingerprint") == fingerprint:
            return {
                "fingerprint": fingerprint,
                "item_ids": previous.get("item_ids", []),
                "unchanged": True,
            }
    mem = _memory()
    try:
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
                "; ".join(chunk.error for chunk in result.chunk_results if chunk.error)
            )
        return {
            "fingerprint": fingerprint,
            "chunks_ingested": result.chunks_ingested,
            "item_ids": [chunk.item_id for chunk in result.chunk_results],
        }
    finally:
        mem.close()


def run() -> None:
    root = queue.capture_dir()
    # A second worker waits rather than exits: an enqueue racing the previous
    # worker's final scan is then guaranteed another scan. All sessions are FIFO.
    with FileLock(str(root / "worker.lock")):
        while True:
            pending = [
                j for j in queue.jobs(root) if j["status"] in {"queued", "running"}
            ]
            if not pending:
                return
            job = queue.transition(pending[0], "running")
            try:
                receipt = import_capture(job)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "Transcript capture %s failed: %s", job["capture_id"], message
                )
                queue.transition(job, "error", error=message)
            else:
                queue.transition(job, "done", **receipt)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run()
