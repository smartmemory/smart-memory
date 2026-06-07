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

        return EmbeddingService._st_model is not None or EmbeddingService._pinned_local_model is not None
    except Exception:
        return False


def warm_models(*, reranker: bool = True) -> None:
    """Synchronously load the local embedder (and optionally the reranker).

    Never raises — a warm failure must not break the caller; the model will simply
    load lazily on first use as before.
    """
    try:
        from smartmemory.plugins.embedding import EmbeddingService

        if EmbeddingService().warm():
            logger.debug("Embedder warmed")
    except Exception as e:
        logger.debug("Embedder warm skipped: %s", e)

    if reranker:
        try:
            from smartmemory.search.rerank import CrossEncoderReranker

            # block=True here is intentional: the CLI/prefetch path WANTS to wait.
            CrossEncoderReranker.get_model(block=True)
        except Exception as e:
            logger.debug("Reranker warm skipped: %s", e)


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
