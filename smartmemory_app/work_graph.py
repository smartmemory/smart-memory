"""Thin wrapper adapters for the core-owned local work graph."""

import logging
import sys

log = logging.getLogger(__name__)


def get_work_graph():
    from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph
    from smartmemory_app.storage import _resolve_data_dir

    path = _resolve_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return SQLiteWorkGraph(str(path / "memory.db"))


def get_work_status() -> dict:
    from smartmemory.pipeline.work_graph.spawn import worker_is_running
    from smartmemory_app.storage import _resolve_data_dir

    return {
        **get_work_graph().stats(),
        "worker_running": worker_is_running(_resolve_data_dir()),
    }


def blocked_work_warning(work: dict) -> str | None:
    """Explain a known prerequisite failure without constructing Lite memory."""
    if not work["pending"]:
        return None
    import spacy

    if spacy.util.is_package("en_core_web_sm"):
        return None
    return (
        f"WARNING: {work['pending']} queued work box(es) cannot drain: spaCy model "
        f"'en_core_web_sm' is missing from {sys.executable}. "
        "Run sm setup to install it in this environment, then run sm worker."
    )


def get_reextract_offer() -> str | None:
    from smartmemory.pipeline.work_graph.reextract import reextract_offer
    from smartmemory.utils.llm import llm_route_available
    from smartmemory_app.config import load_config
    from smartmemory_app.storage import get_memory

    if load_config().mode == "remote":
        return None
    graph = get_work_graph()
    if graph.get_meta("reextract_decision") is not None or not llm_route_available()[0]:
        return None
    return reextract_offer(get_memory(), graph)


def show_reextract_offer(emit) -> None:
    """Offer failures must not prevent setup or daemon startup."""
    try:
        offer = get_reextract_offer()
        if offer:
            emit(offer)
    except Exception:
        log.warning(
            "Re-extraction notice unavailable; run sm admin reextract to inspect the backlog",
            exc_info=True,
        )
