"""Record a decision and display its reasons on the command line."""

# === 1. IMPORTS ===
import json

import click


# === 2. PUBLIC API ===
@click.command("decide")
@click.argument("text")
@click.option("--why", required=True, help="Reason for the decision.")
@click.option(
    "--ruled-out", multiple=True, help="Option ruled out. Repeat for each option."
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the new decision ID as JSON."
)
def decide_cmd(text: str, why: str, ruled_out: tuple[str, ...], as_json: bool) -> None:
    """Record TEXT as a new decision. Replacing a decision is outside this command's scope."""
    from smartmemory_app.cli import _DAEMON_NOT_RUNNING_MSG, _memory_request

    # 1. Validate all inputs before sending the write.
    body = {
        "content": _nonblank(text, "TEXT"),
        "rationale": _nonblank(why, "--why"),
        "rejected_alternatives": [
            _nonblank(option, "--ruled-out") for option in ruled_out
        ],
    }
    # 2. Send the same create contract to the configured backend.
    result = _memory_request("POST", "/memory/decisions/create", json=body)
    if result is None:
        raise click.ClickException(_DAEMON_NOT_RUNNING_MSG)
    decision_id = result.get("decision_id") if isinstance(result, dict) else None
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise click.ClickException("SmartMemory returned a decision without an ID.")

    # 3. Identify the newly recorded decision.
    click.echo(
        json.dumps({"decision_id": decision_id})
        if as_json
        else f"Recorded decision {decision_id}"
    )


def render_reasons(decision: dict) -> None:
    """Show the stated reason and each alternative in its original order."""
    rationale = decision.get("rationale") or decision.get("reasoning")
    if rationale:
        click.echo(f"  rationale: {rationale}")
    for alternative in decision.get("rejected_alternatives") or []:
        click.echo(f"  ruled out: {alternative}")


def render_lineage_item(item: dict, match: dict) -> None:
    """Use the full matched decision and its reasons when displaying its lineage row."""
    if not isinstance(item, dict):
        return
    is_decision = match.get("memory_type") == "decision" and match[
        "item_id"
    ] == item.get("item_id")
    content = match.get("content") if is_decision else item.get("content", "")
    click.echo(
        f"[{item.get('memory_type', '?')}] {content} ({str(item.get('item_id', ''))[:8]})"
    )
    if is_decision:
        render_reasons(match.get("metadata") or {})


# === 3. INTERNAL HELPERS ===
def _nonblank(value: str, parameter: str) -> str:
    """Trim surrounding whitespace and reject empty CLI arguments."""
    if not value.strip():
        raise click.BadParameter("must contain text", param_hint=parameter)
    return value.strip()
