"""Thin wrapper adapters for the core-owned local work graph."""

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

STATUS_UNAVAILABLE = (
    "local status unavailable: installed core has no smartmemory.save.local_status"
)


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


# -- Lite save status: live / retained projection (N-02.activate.outer-readers; G53, G137, G148, G44) ----------


class LocalStatusUnavailable(RuntimeError):
    """The installed core cannot answer local save status. Nothing was inferred."""


def _local_status_module():
    try:
        from smartmemory.save import local_status
    except ImportError as exc:
        log.warning("%s; status replay refused, no status returned", STATUS_UNAVAILABLE)
        raise LocalStatusUnavailable(STATUS_UNAVAILABLE) from exc
    return local_status


def local_status_capability() -> dict[str, Any]:
    """Explicit Lite status capability (G137): what this placement supports and what is hosted-only.

    ``available`` is False, with the reason, when the installed core predates the status adapter, so a caller
    never mistakes a missing reader for "no runs".
    """
    try:
        module = _local_status_module()
    except LocalStatusUnavailable:
        return {"available": False, "reason": STATUS_UNAVAILABLE}
    return {"available": True, **module.local_capabilities()}


def _read_only_fence(request):
    """Status reads never take the authority fence (core docstring); a call here is a wiring bug, so it denies."""
    from smartmemory.authority import AuthorityVerdict

    log.warning("Local status fence called on a read-only status adapter; denied")
    return AuthorityVerdict.unavailable


def open_local_status(
    *,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    workspace_hidden: bool,
    pending,
    fetch_subject,
):
    """Core's one scoped status adapter over the Lite ``memory.db``, for one actor in one workspace.

    Every argument is explicit. ``workspace_hidden`` is the caller's authoritative revocation fact (True hides
    every source), ``pending`` is the submitter's own admission-row port, and ``fetch_subject(kind, id)`` is the
    authoritative row read the visibility gate hydrates through. Nothing here defaults to a grant.
    """
    import sqlite3

    from smartmemory.ownership import OwnershipContext
    from smartmemory.pipeline.work_graph.port import Scope, WorkGraphContractError
    from smartmemory.pipeline.work_graph.v2 import bind_sqlite_v2
    from smartmemory.save.read_port import InvalidRow, ScopeKeys
    from smartmemory.visibility import ReadVisibilityGate

    module = _local_status_module()
    try:
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id)
        caller = ScopeKeys(tenant_id, workspace_id, user_id)
    except (WorkGraphContractError, InvalidRow, TypeError, ValueError) as exc:
        log.warning(
            "Local status refused: caller identity is not readable (%s); nothing returned",
            type(exc).__name__,
        )
        raise module.InvalidArgument("caller identity is not readable") from exc
    gate = ReadVisibilityGate(
        OwnershipContext(
            workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id
        ),
        {},
        workspace_hidden=workspace_hidden,
    )
    try:
        store = get_work_graph()
    except (OSError, sqlite3.Error, WorkGraphContractError) as exc:
        log.warning(
            "Local status refused: the work graph could not be opened (%s); nothing returned",
            type(exc).__name__,
        )
        raise module.LocalStatusRefused("the work graph could not be opened") from exc
    try:
        lifecycle = bind_sqlite_v2(store, scope=scope, fence=_read_only_fence)
    # core's bind logs its own refusal (one WARNING), so there is no second line here
    except WorkGraphContractError as exc:
        raise module.LocalStatusRefused("the work graph could not be opened") from exc
    except (OSError, sqlite3.Error) as exc:
        log.warning(
            "Local status refused: the work graph could not be opened (%s); nothing returned",
            type(exc).__name__,
        )
        raise module.LocalStatusRefused("the work graph could not be opened") from exc
    return module.LocalStatusAdapter(
        lifecycle, store, pending, gate, fetch_subject, caller=caller
    )


def project_local_status(result) -> dict[str, Any]:
    """The JSON-able projection of one status read: a receipt (live or retained) or a display-only legacy row."""
    module = _local_status_module()
    if isinstance(result, module.LegacyRunProjection):
        return {
            "arm": "legacy",
            "status_text": module.status_text(result),
            "legacy": {
                "run_id": result.run_id,
                "item_id": result.item_id,
                "status": result.status,
                "current_stage": result.current_stage,
                "placement": result.placement,
                "provenance": result.provenance,
                "status_url": result.status_url,
            },
        }
    return {
        "arm": "receipt",
        "status_text": module.status_text(result),
        "receipt": result.to_dict(),
    }


def get_local_status(run_id: str, **actor) -> dict[str, Any]:
    """Status of ``run_id`` for the actor (see ``open_local_status``). Unknown, hidden and aged-out runs raise
    core's one ``RunUnknown``; a damaged or unreadable source raises ``LocalStatusRefused``. Each logs once in core."""
    return project_local_status(open_local_status(**actor).get_receipt(run_id))
