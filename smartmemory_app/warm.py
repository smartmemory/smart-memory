"""Model pre-warming for the local FREE path (DIST-LITE-WARMSTART-1).

The first ``add()`` pays a cold embedder load (~12s, or ~38s the first time the
model is downloaded) and the first ``search()`` would pay a cold reranker load.
These helpers load the models ahead of (or in parallel with) the user's first real
call so the one-command "wow" moment isn't a multi-second hang.

- ``warm_models()`` — foreground; used by the ``smartmemory warm`` CLI and install
  prefetch, where a visible one-time wait is acceptable.
- ``warm_models_background()`` — fire-and-forget daemon thread; used at local
  construction so the embedder load overlaps construction + user think-time. Opt out
  with ``SMARTMEMORY_NO_WARM=1``.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_warm_started = False
_warm_lock = threading.Lock()


def is_warm() -> bool:
    """True if the local embedder model is already resident in this process.

    Used to decide whether a foreground op is about to pay a cold load, so the
    caller can show a progress notice instead of a silent hang.
    """
    try:
        from smartmemory.plugins.embedding import EmbeddingService

        return (
            EmbeddingService._st_model is not None
            or EmbeddingService._pinned_local_model is not None
        )
    except Exception:
        return False


def warm_models(*, reranker: bool = True, strict: bool = False) -> None:
    """Synchronously load the local embedder (and optionally the reranker).

    Background callers retain lazy loading. The explicit CLI uses strict mode
    so a failed loader cannot announce successful model warmup.
    """
    failures = []
    try:
        from smartmemory.plugins.embedding import EmbeddingService

        service = EmbeddingService()
        needs_local = service.provider in {"local", "huggingface"} or (
            service.provider == "openai" and not service.api_key
        )
        if service.warm():
            logger.debug("Embedder warmed")
        elif needs_local:
            raise RuntimeError(
                "Embedding model could not be loaded. Run smartmemory setup to repair the local runtime/model cache."
            )
    except Exception as e:
        logger.warning("Embedder warm failed: %s", e)
        failures.append(e)

    if reranker:
        try:
            from smartmemory.search.rerank import CrossEncoderReranker

            # block=True here is intentional: the CLI/prefetch path WANTS to wait.
            if CrossEncoderReranker.get_model(block=True) is None:
                raise RuntimeError(
                    "Reranker model could not be loaded. Run smartmemory setup to repair the local runtime/model cache."
                )
        except Exception as e:
            logger.warning("Reranker warm failed: %s", e)
            failures.append(e)

    if strict and failures:
        raise failures[
            0
        ]  # Preserve the real loader class and message for the handled report.


def warm_models_background(*, reranker: bool = False) -> None:
    """Kick a one-time daemon thread to warm models without blocking the caller.

    Defaults to embedder-only (every ``add()`` needs it; the reranker already
    background-loads lazily on first search). Opt out with ``SMARTMEMORY_NO_WARM=1``.
    Idempotent — only the first call per process starts a thread.
    """
    global _warm_started
    if os.environ.get("SMARTMEMORY_NO_WARM"):
        return
    with _warm_lock:
        if _warm_started:
            return
        _warm_started = True
    threading.Thread(
        target=warm_models,
        kwargs={"reranker": reranker},
        name="smartmemory-warm",
        daemon=True,
    ).start()
