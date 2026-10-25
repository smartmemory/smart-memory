"""Detached, serial transcript importer. The OS releases worker.lock on a crash."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from filelock import FileLock, Timeout

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
                "; ".join(chunk.error for chunk in result.chunk_results if chunk.error)
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


# ── Hook write queue drain (HOOK-WRITE-QUEUE-1) ────────────────────────────────

# HOOK-LOAD-CAP-1's per-phase hook caps (observe 3, learn 2, distill 2) moved here:
# the queue is drained by at most this many concurrent jobs, nothing is dropped.
DEFAULT_HOOK_PARALLEL = 3
DEFAULT_HOOK_MAX_ATTEMPTS = 5
DEFAULT_HOOK_IDLE_EXIT = 20.0


def _env_number(name: str, default, cast, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("%s=%r is not a number, using %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%r is below %s, using %s", name, raw, minimum, default)
        return default
    return value


def run_hook_job(job: dict) -> None:
    """Run one queued phase through the same code the ``smartmemory lifecycle <phase>`` CLI runs.

    Raises PermanentHookError for a payload that can never succeed; any other
    exception (a store or daemon failure, surfaced by ``strict``) is retried.
    """
    try:
        raw = Path(job["payload_path"]).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise queue.PermanentHookError(f"payload file missing: {exc}") from exc
    except (OSError, UnicodeDecodeError, TypeError, KeyError) as exc:
        raise queue.PermanentHookError(f"payload unreadable: {exc}") from exc
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise queue.PermanentHookError(f"payload is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise queue.PermanentHookError("payload is not a JSON object")
    user_turn = None
    if job["phase"] == "distill":
        # The prompt this response answers, as the session state held it when the
        # Stop hook fired. Reading live state here would pair it with a later prompt.
        user_turn = ""
        if job.get("state_snapshot"):
            try:
                state = json.loads(
                    Path(job["state_snapshot"]).read_text(encoding="utf-8")
                )
                user_turn = (
                    state.get("current_user_turn") if isinstance(state, dict) else None
                ) or ""
            except (OSError, ValueError) as exc:
                log.warning(
                    "Distill prompt snapshot unreadable, pairing the response with no prompt: %s",
                    exc,
                )
    from smartmemory_app.cli import run_lifecycle_phase

    run_lifecycle_phase(
        job["phase"],
        body,
        strict=True,
        workspace_id=job.get("workspace_id"),
        user_turn=user_turn,
    )


def _write_pid(root: Path) -> None:
    pid_path = root / "worker.pid"
    temporary = pid_path.with_name(f".worker.pid.{os.getpid()}.tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="utf-8")
    temporary.replace(pid_path)


def _clear_pid(root: Path) -> None:
    pid_path = root / "worker.pid"
    try:
        if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink()
    except (OSError, ValueError):
        pass


def _holder_alive(root: Path) -> bool:
    from smartmemory.utils.process import (
        pid_alive,
    )  # never signals (os.kill(pid, 0) kills on Windows)

    try:
        pid = int((root / "worker.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def run_hooks(
    root: str | Path | None = None,
    *,
    parallel: int | None = None,
    max_attempts: int | None = None,
    idle_exit: float | None = None,
    poll: float = 0.1,
) -> int:
    """Drain the hook queue until it stays empty for ``idle_exit`` seconds. Returns jobs finished.

    One drainer per queue (worker.lock); jobs of one session run strictly in
    queue order, at most ``parallel`` sessions at once. Handshake with the hook
    scripts, which enqueue first and then spawn a drainer only when worker.pid
    names no live process: the holder unlinks worker.pid, then rescans before it
    exits, so a record published before a hook saw worker.pid is never stranded.
    """
    root = queue.hook_queue_dir(root)
    parallel = parallel or _env_number(
        "SMARTMEMORY_HOOK_DRAIN_MAX", DEFAULT_HOOK_PARALLEL, int, 1
    )
    max_attempts = max_attempts or _env_number(
        "SMARTMEMORY_HOOK_MAX_ATTEMPTS", DEFAULT_HOOK_MAX_ATTEMPTS, int, 1
    )
    if idle_exit is None:
        idle_exit = _env_number(
            "SMARTMEMORY_HOOK_WORKER_IDLE", DEFAULT_HOOK_IDLE_EXIT, float, 0.0
        )
    # The drainer serves every session, so the spawning session's pin is not the
    # job's: each record carries the pin its own hook saw (workspace_id).
    os.environ.pop("SMARTMEMORY_WORKSPACE_ID", None)
    lock = FileLock(str(root / "worker.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if _holder_alive(root):
            return 0  # the live holder rescans after our hook's record was published
        lock.acquire()  # the holder is exiting; take over once it lets go
        idle_exit = 0.0 if not queue.hook_jobs(root) else idle_exit
    finished = 0
    try:
        queue.recover_running(root, max_attempts=max_attempts)
        _write_pid(root)
        inflight: dict = {}
        idle_since = None
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            while True:
                for future in [f for f in inflight if f.done()]:
                    job = inflight.pop(future)
                    finished += 1
                    try:
                        future.result()
                    except queue.PermanentHookError as exc:
                        queue.fail_hook_job(
                            root,
                            job,
                            str(exc),
                            max_attempts=max_attempts,
                            permanent=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - every other failure is retried
                        queue.fail_hook_job(
                            root,
                            job,
                            f"{type(exc).__name__}: {exc}",
                            max_attempts=max_attempts,
                        )
                    else:
                        queue.complete_hook_job(job)
                pending = queue.hook_jobs(root)
                busy = {job["session_id"] for job in inflight.values()}
                now = time.time()
                for job in pending:
                    if len(inflight) >= parallel:
                        break
                    session = job["session_id"]
                    if session in busy:
                        continue
                    busy.add(session)  # only the oldest job of a session may start
                    if job.get("not_before", 0) > now:
                        continue
                    claimed = queue.claim_hook_job(root, job)
                    if claimed is not None:
                        inflight[pool.submit(run_hook_job, claimed)] = claimed
                if inflight:
                    idle_since = None
                    wait(list(inflight), timeout=poll, return_when=FIRST_COMPLETED)
                    continue
                if pending:
                    idle_since = None
                    time.sleep(poll)  # only backoff-delayed jobs remain
                    continue
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since < idle_exit:
                    time.sleep(poll)
                    continue
                _clear_pid(root)
                if queue.hook_jobs(root):
                    _write_pid(root)
                    idle_since = None
                    continue
                return finished
    finally:
        _clear_pid(root)
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hooks", action="store_true", help="drain the hook write queue"
    )
    parser.add_argument(
        "--dir", help="hook queue directory (default <data dir>/hook-queue)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.hooks:
        run_hooks(args.dir)
    else:
        run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
