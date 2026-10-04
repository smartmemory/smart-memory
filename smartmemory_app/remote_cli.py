"""Remote CLI routing through the existing hosted backend and credential resolver."""

import re

import click

from smartmemory_app.config import load_config
from smartmemory_app.remote_backend import RemoteBackendError
from smartmemory_app.storage import RESERVED_INGEST_PROPERTIES, _get_remote_memory


# Hosted authority fields complement the shared producer-owned property set.
# Lifecycle workspace hints still pass through storage.ingest(), as before.
_HOSTED_SCOPE_PROPERTIES = frozenset(
    {
        "user_id",
        "tenant_id",
        "workspace_id",
        "team_id",
        "scope",
        "source_agent_id",
        "source_agent_name",
        "isolation_level",
        "created_by",
        "security_scope",
    }
)


def require_local(command: str) -> None:
    """Refuse local store operations before they read or mutate local state."""
    if load_config().mode == "remote":
        raise click.ClickException(f"{command} is only available in local mode.")


def request_memory(cfg, method: str, path: str, **kwargs):
    """Use hosted contracts, adapting the few daemon-specific request envelopes."""
    mem = _get_remote_memory(cfg)
    try:
        if path == "/memory/ask":
            return mem.ask(**kwargs["json"])
        if path == "/memory/ingest":
            body = kwargs.pop("json")
            properties = body.get("properties") or {}
            reserved = (
                RESERVED_INGEST_PROPERTIES | _HOSTED_SCOPE_PROPERTIES
            ).intersection(properties)
            if reserved:
                raise click.ClickException(
                    "Reserved remote add properties cannot be set: "
                    + ", ".join(sorted(reserved))
                    + ". Use --type for memory type. Hosted scope comes from authentication."
                )
            invalid = [
                key
                for key in properties
                if not re.fullmatch(r"[a-zA-Z_]\w*", key) or "__" in key
            ]
            if invalid:
                raise click.ClickException(
                    "Remote add property keys must be simple identifiers without '__': "
                    + ", ".join(sorted(invalid))
                    + "."
                )
            kwargs["json"] = {
                "content": body["content"],
                "context": {
                    **properties,
                    **body.get("context", {}),
                    "memory_type": body["memory_type"],
                },
            }
        if path == "/memory/search":
            body = kwargs["json"]
            filters = body.get("filters") or {}
            if filters and body["query"].strip() != "*":
                raise click.ClickException(
                    "Property filters on remote semantic search are unsupported: "
                    "the hosted search API has no arbitrary property predicates. "
                    "Use sm search '*' with one property filter for exact-match listing."
                )
            if body["query"].strip() == "*":
                unsupported = set(body) - {"query", "top_k", "offset", "filters"}
                if unsupported:
                    flags = ", ".join(
                        "--" + name.replace("_", "-") for name in sorted(unsupported)
                    )
                    raise click.ClickException(
                        f"Remote wildcard listing does not support search options: {flags}."
                    )
                if len(filters) > 1:
                    raise click.ClickException(
                        "Remote exact-match listing supports only one property filter. "
                        "The hosted list API accepts one metadata_key/metadata_value pair."
                    )
                options = {}
                if filters:
                    key, value = next(iter(filters.items()))
                    if key == "memory_type":
                        options["memory_type"] = value
                    else:
                        options.update(metadata_key=key, metadata_value=value)
                    click.echo(
                        "Remote property search uses exact-match listing, without semantic ranking.",
                        err=True,
                    )
                return mem.list_memories(
                    limit=body["top_k"], offset=body.get("offset", 0), **options
                )
        if path == "/memory/recall":
            # Hosted recall already composes search + formatting in RemoteMemory.
            # There is no hosted /memory/recall endpoint.
            params = kwargs["params"]
            return {
                "context": mem.recall(
                    params["cwd"] or None,
                    params["top_k"],
                    query=params["query"] or None,
                    workspace_id=params["workspace_id"] or None,
                    include_snapshot=params["include_snapshot"] == "true",
                    strict=params["strict"] == "true",
                    raise_on_error=True,
                )
            }
        result = mem.request(method, path, **kwargs)
        if path == "/memory/ingest" and (
            not isinstance(result, dict) or not result.get("item_id")
        ):
            raise click.ClickException(
                "SmartMemory service returned an invalid ingest response."
            )
        if path == "/memory/search" and not isinstance(result, (dict, list)):
            raise click.ClickException(
                "SmartMemory service returned an invalid search response."
            )
        if path == "/memory/search" and isinstance(result, dict):
            rows = result.get("results")
            if not isinstance(rows, list):
                raise click.ClickException(
                    "SmartMemory service returned an invalid search response."
                )
            return {"items": rows}
        return {} if result is None else result
    except RemoteBackendError as exc:
        raise click.ClickException(str(exc)) from exc


def show_status(cfg) -> None:
    """Verify hosted access and show scoped counts, without probing a local daemon."""
    summary = request_memory(cfg, "GET", "/memory/summary")
    click.echo("SmartMemory service: connected")
    click.echo("  Mode:       remote")
    click.echo(f"  API:        {cfg.api_url}")
    click.echo(f"  Team:       {_get_remote_memory(cfg)._team_id}")
    click.echo(f"  Memories:   {summary.get('total_items', '?')}")
