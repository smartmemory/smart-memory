"""Enrichment worker — separate process that drains the SQLite queue.

Polls enrichment_queue table, runs Tier 2 LLM extraction for each job,
writes results back to the graph. Runs as its own launchd-managed process.

Usage:
    python -m smartmemory_app.enrichment_worker          # run once (drain queue)
    python -m smartmemory_app.enrichment_worker --loop    # poll continuously
"""

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_MAX_ITEM_NOT_FOUND_ATTEMPTS = 3
_observed_store_generation: str | None = None
_generation_initialized = False


def _get_worker_memory():
    """Refresh the process-local SmartMemory singleton after ``sm clear``."""
    global _generation_initialized, _observed_store_generation

    from smartmemory_app import storage
    from smartmemory_app.store_generation import read_store_generation

    generation = read_store_generation()
    if not _generation_initialized:
        _observed_store_generation = generation
        _generation_initialized = True
    elif generation != _observed_store_generation:
        logger.warning(
            "Lite store generation changed (%s -> %s); refreshing worker memory handle",
            _observed_store_generation or "legacy",
            generation or "legacy",
        )
        storage._shutdown()
        _observed_store_generation = generation
    return storage.get_memory()


def process_one_job(job: dict) -> dict:
    """Run Tier 2 LLM extraction for a single job."""
    from smartmemory.background.extraction_worker import (
        _get_content_from_item,
        _run_llm_extraction,
        process_extract_job,
    )

    if "_store_generation" in job and not _same_store_generation(job):
        return {"status": "store_replaced", "new_entities": 0, "new_relations": 0}
    item_id = job["item_id"]
    mem = _get_worker_memory()
    item = mem.get(item_id)

    if item is None:
        return {"status": "item_not_found", "new_entities": 0, "new_relations": 0}

    content = _get_content_from_item(item)
    if not content.strip():
        return {"status": "no_text", "new_entities": 0, "new_relations": 0}

    llm_result = _run_llm_extraction(content)
    if llm_result["status"] != "ok":
        return {"status": "llm_failed", "new_entities": 0, "new_relations": 0}

    # A clear can happen during the slow LLM request. Re-check the generation
    # before writing and re-fetch from the current store.
    if "_store_generation" in job and not _same_store_generation(job):
        return {"status": "store_replaced", "new_entities": 0, "new_relations": 0}
    mem = _get_worker_memory()
    fresh_item = mem.get(item_id)
    if fresh_item is None:
        return {"status": "item_not_found", "new_entities": 0, "new_relations": 0}

    with mem._di_context():
        result = process_extract_job(
            mem,
            job,
            redis_client=None,
            item_override=fresh_item,
            extraction_override=llm_result.get("extraction"),
        )

    return result


def _notify_daemon(result: dict) -> bool:
    """Send successful Tier-2 graph mutations to the daemon's event sink."""
    events = [
        {"operation": "add_node", "data": node}
        for node in result.get("new_entity_nodes", [])
    ]
    events.extend(
        {"operation": "add_edge", "data": edge}
        for edge in result.get("new_relation_edges", [])
    )
    if not events:
        return True

    try:
        import httpx

        from smartmemory_app.config import load_config

        port = load_config().daemon_port
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{port}/memory/_internal/events",
                json={"events": events},
                timeout=3,
            )
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning(
            "Tier-2 writes succeeded but live viewer notification was lost: %s",
            exc,
        )
        return False


def _same_store_generation(job: dict) -> bool:
    from smartmemory_app.store_generation import read_store_generation

    return job.get("_store_generation") == read_store_generation()


def drain_queue() -> int:
    """Process all pending jobs. Returns count of jobs processed."""
    from smartmemory_app.enrichment_queue import (
        dequeue,
        mark_done,
        mark_failed,
        mark_retry,
    )

    processed = 0
    while True:
        jobs = dequeue(batch_size=1)
        if not jobs:
            break

        for job in jobs:
            item_id = job["item_id"]
            queue_id = job["queue_id"]
            if not _same_store_generation(job):
                logger.warning(
                    "Store was cleared before enriching %s; discarded stale queue job",
                    item_id,
                )
                continue
            try:
                result = process_one_job(job)
                processed += 1
                status = result.get("status", "?")
                new_e = result.get("new_entities", 0)
                new_r = result.get("new_relations", 0)
                if not _same_store_generation(job):
                    logger.warning(
                        "Store was cleared while enriching %s; discarded stale queue completion",
                        item_id,
                    )
                    continue
                if status == "ok":
                    _notify_daemon(result)
                    mark_done(queue_id)
                    if new_e or new_r:
                        logger.info(
                            "Enriched %s: +%d entities, +%d relations",
                            item_id,
                            new_e,
                            new_r,
                        )
                    else:
                        logger.info("Enrichment %s: status=ok", item_id)
                    continue

                warning = (
                    f"Tier-2 entities and relations were not written for {item_id}: "
                    f"status={status}"
                )
                attempts = int(job.get("attempts", 1))
                if (
                    status == "item_not_found"
                    and attempts < _MAX_ITEM_NOT_FOUND_ATTEMPTS
                ):
                    mark_retry(queue_id, status)
                    logger.warning(
                        "%s (attempt %d/%d); job remains retryable",
                        warning,
                        attempts,
                        _MAX_ITEM_NOT_FOUND_ATTEMPTS,
                    )
                    # Avoid immediately consuming all bounded retries in one drain.
                    return processed

                mark_failed(queue_id, status)
                logger.warning("%s; job marked failed", warning)
            except Exception as e:
                if _same_store_generation(job):
                    mark_failed(queue_id, str(e))
                    logger.warning("Enrichment failed for %s: %s", item_id, e)
                else:
                    logger.warning(
                        "Store was cleared while enrichment %s failed; ignored stale queue id: %s",
                        item_id,
                        e,
                    )

    return processed


def run_loop(poll_interval: float = 2.0) -> None:
    """Poll the queue continuously."""
    logger.info("Enrichment worker started (poll every %.1fs)", poll_interval)
    while True:
        try:
            n = drain_queue()
            if n > 0:
                logger.info("Processed %d jobs", n)
        except Exception:
            logger.warning("Drain cycle failed", exc_info=True)
        time.sleep(poll_interval)


def main():
    loop = "--loop" in sys.argv
    if loop:
        run_loop()
    else:
        n = drain_queue()
        logger.info("Drained %d jobs", n)


if __name__ == "__main__":
    main()
