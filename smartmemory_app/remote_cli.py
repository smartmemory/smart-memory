"""Remote CLI routing through the existing hosted backend and credential resolver."""

import click

from smartmemory_app.config import load_config
from smartmemory_app.remote_backend import RemoteBackendError
from smartmemory_app.storage import _get_remote_memory


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
            if body.get("properties"):
                raise click.ClickException(
                    "Property flags on sm add are not supported in remote mode."
                )
            kwargs["json"] = {
                "content": body["content"],
                "context": {
                    **body.get("context", {}),
                    "memory_type": body["memory_type"],
                },
            }
        if path == "/memory/search":
            body = kwargs["json"]
            if body.get("filters"):
                raise click.ClickException(
                    "Property filters are not supported in remote mode."
                )
            if body["query"].strip() == "*":
                unsupported = set(body) - {"query", "top_k", "offset"}
                if unsupported:
                    flags = ", ".join(
                        "--" + name.replace("_", "-") for name in sorted(unsupported)
                    )
                    raise click.ClickException(
                        f"Remote wildcard listing does not support search options: {flags}."
                    )
                return mem.list_memories(
                    limit=body["top_k"], offset=body.get("offset", 0)
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
