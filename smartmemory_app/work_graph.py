"""Thin wrapper adapters for the core-owned local work graph."""

import importlib
import logging
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

ADAPTER_MODULE = "smartmemory.save.local_status"
STATUS_UNAVAILABLE = f"local status unavailable: installed core has no {ADAPTER_MODULE}"
STATUS_REFUSED = "status replay refused, no status returned"
WORK_GRAPH_REFUSED = "the local work graph was not opened"

#: Every name the local status path takes from another package, as (module, symbol, what is lost without it).
#: The path is ``open_local_status``, ``project_local_status``, ``local_status_capability``, the read-only fence
#: and ``get_work_graph``. ``_core_names`` is the one boundary that resolves these rows, module and symbol, before
#: any use; none of those functions imports anything itself. A guard test re-derives this table from the code.
STATUS_IMPORTS = (
    (ADAPTER_MODULE, "LocalStatusAdapter", "the scoped status adapter"),
    (ADAPTER_MODULE, "InvalidArgument", "the typed refusal for an unreadable caller"),
    (
        ADAPTER_MODULE,
        "LocalStatusRefused",
        "the typed refusal for an unopenable work graph",
    ),
    (ADAPTER_MODULE, "LegacyRunProjection", "the display-only legacy run projection"),
    (ADAPTER_MODULE, "status_text", "the status line"),
    (ADAPTER_MODULE, "local_capabilities", "the Lite capability statement"),
    ("smartmemory.authority", "AuthorityVerdict", "the read-only fence denial"),
    ("smartmemory.ownership", "OwnershipContext", "the caller's ownership context"),
    ("smartmemory.pipeline.work_graph.port", "Scope", "the work graph scope"),
    (
        "smartmemory.pipeline.work_graph.port",
        "WorkGraphContractError",
        "the work graph contract refusal",
    ),
    (
        "smartmemory.pipeline.work_graph.sqlite_store",
        "SQLiteWorkGraph",
        "the Lite work graph store",
    ),
    (
        "smartmemory.pipeline.work_graph.v2",
        "bind_sqlite_v2",
        "the v2 lifecycle binding",
    ),
    ("smartmemory.save.read_port", "InvalidRow", "the read port row refusal"),
    ("smartmemory.save.read_port", "ScopeKeys", "the caller scope keys"),
    ("smartmemory.visibility", "ReadVisibilityGate", "the read visibility gate"),
    ("smartmemory_app.storage", "_resolve_data_dir", "the Lite data directory"),
)

#: The modules of that table other than the adapter itself, derived from it (the round-1 name, still read by callers).
OPEN_DEPENDENCIES = tuple(
    sorted({module for module, _, _ in STATUS_IMPORTS} - {ADAPTER_MODULE})
)

#: The rows ``get_work_graph`` needs. Its legacy callers (CLI, daemon, viewer) still get an ImportError.
WORK_GRAPH_IMPORTS = ("SQLiteWorkGraph", "_resolve_data_dir")


class LocalStatusUnavailable(RuntimeError):
    """The installed core cannot answer local save status. Nothing was inferred."""


class WorkGraphUnavailable(ImportError):
    """The installed core cannot open the Lite work graph. An ImportError, as ``get_work_graph`` always raised."""


def _core_names(
    symbols=None, *, refused=STATUS_REFUSED, error=LocalStatusUnavailable
) -> SimpleNamespace:
    """Resolve ``STATUS_IMPORTS`` (or the rows naming ``symbols``): a module that does not import, an ImportError
    raised while importing it, or a module without the symbol is ``error`` with exactly one WARNING."""
    names = {}
    for module_name, symbol, lost in STATUS_IMPORTS:
        if symbols is not None and symbol not in symbols:
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            if exc.name == module_name:  # the module itself is absent
                if module_name == ADAPTER_MODULE:
                    log.warning("%s; %s", STATUS_UNAVAILABLE, refused)
                    raise error(STATUS_UNAVAILABLE) from exc
                log.warning(
                    "Local status dependency %s is not importable; %s",
                    module_name,
                    refused,
                )
                raise error(
                    f"local status unavailable: installed core has no {module_name}"
                ) from exc
            # raised inside the module's own import (one of its dependencies or names is missing): name that cause
            cause = f"{type(exc).__name__}: {exc}"
            log.warning(
                "Local status dependency %s is not importable (%s); %s",
                module_name,
                cause,
                refused,
            )
            raise error(
                f"local status unavailable: {module_name} is not importable ({cause})"
            ) from exc
        try:
            names[symbol] = getattr(module, symbol)
        except AttributeError as exc:
            log.warning(
                "Local status dependency %s has no %s (%s); %s",
                module_name,
                symbol,
                lost,
                refused,
            )
            raise error(
                f"local status unavailable: installed core has no {module_name}.{symbol}"
            ) from exc
    return SimpleNamespace(**names)


def get_work_graph():
    core = _core_names(
        WORK_GRAPH_IMPORTS, refused=WORK_GRAPH_REFUSED, error=WorkGraphUnavailable
    )
    path = core._resolve_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return core.SQLiteWorkGraph(str(path / "memory.db"))


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
    from smartmemory_app.config import load_config
    from smartmemory_app.storage import apply_runtime_config, get_memory

    if load_config().mode == "remote":
        return None
    # Apply the saved provider settings before asking core for a route, as the
    # memory below would, so a local provider with no model answers no here too.
    # Import the resolver after that: the hold replaces the module attribute.
    apply_runtime_config()
    from smartmemory.utils.llm import llm_route_available

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


def local_status_capability() -> dict[str, Any]:
    """Explicit Lite status capability (G137): what this placement supports and what is hosted-only.

    ``available`` is False, with the reason, when the installed core predates the status adapter, so a caller
    never mistakes a missing reader for "no runs".
    """
    try:
        core = _core_names()
    except LocalStatusUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, **core.local_capabilities()}


def _read_only_fence(request):
    """Status reads never take the authority fence (core docstring); a call here is a wiring bug, so it denies."""
    denied = _core_names(("AuthorityVerdict",)).AuthorityVerdict.unavailable
    log.warning("Local status fence called on a read-only status adapter; denied")
    return denied


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
    core = _core_names()
    try:
        scope = core.Scope(tenant_id=tenant_id, workspace_id=workspace_id)
        caller = core.ScopeKeys(tenant_id, workspace_id, user_id)
    except (core.WorkGraphContractError, core.InvalidRow, TypeError, ValueError) as exc:
        log.warning(
            "Local status refused: caller identity is not readable (%s); nothing returned",
            type(exc).__name__,
        )
        raise core.InvalidArgument("caller identity is not readable") from exc
    gate = core.ReadVisibilityGate(
        core.OwnershipContext(
            workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id
        ),
        {},
        workspace_hidden=workspace_hidden,
    )
    try:
        store = get_work_graph()
    except (OSError, sqlite3.Error, core.WorkGraphContractError) as exc:
        log.warning(
            "Local status refused: the work graph could not be opened (%s); nothing returned",
            type(exc).__name__,
        )
        raise core.LocalStatusRefused("the work graph could not be opened") from exc
    try:
        lifecycle = core.bind_sqlite_v2(store, scope=scope, fence=_read_only_fence)
    # core's bind logs its own refusal (one WARNING), so there is no second line here
    except core.WorkGraphContractError as exc:
        raise core.LocalStatusRefused("the work graph could not be opened") from exc
    except (OSError, sqlite3.Error) as exc:
        log.warning(
            "Local status refused: the work graph could not be opened (%s); nothing returned",
            type(exc).__name__,
        )
        raise core.LocalStatusRefused("the work graph could not be opened") from exc
    return core.LocalStatusAdapter(
        lifecycle, store, pending, gate, fetch_subject, caller=caller
    )


def project_local_status(result) -> dict[str, Any]:
    """The JSON-able projection of one status read: a receipt (live or retained) or a display-only legacy row."""
    core = _core_names()
    if isinstance(result, core.LegacyRunProjection):
        return {
            "arm": "legacy",
            "status_text": core.status_text(result),
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
        "status_text": core.status_text(result),
        "receipt": result.to_dict(),
    }


def get_local_status(run_id: str, **actor) -> dict[str, Any]:
    """Status of ``run_id`` for the actor (see ``open_local_status``). Unknown, hidden and aged-out runs raise
    core's one ``RunUnknown``; a damaged or unreadable source raises ``LocalStatusRefused``. Each logs once in core."""
    return project_local_status(open_local_status(**actor).get_receipt(run_id))
