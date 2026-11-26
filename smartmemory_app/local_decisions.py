"""Create a structured decision in the daemon's active Lite store."""

# === 1. IMPORTS ===
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, Field, StringConstraints


# === 2. TYPES ===
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DecisionWrite(BaseModel):
    """Fields supplied by the CLI for a plain decision write."""

    content: Text
    rationale: Text
    rejected_alternatives: list[Text] = Field(default_factory=list)


# === 3. PUBLIC API ===
def create_decision(body: DecisionWrite) -> dict:
    """Persist through the same DecisionManager used by the local MCP backend."""
    from smartmemory.decisions.manager import DecisionManager
    from smartmemory_app.local_api import _get_mem, _rw_lock
    from smartmemory_app.remote_backend import RemoteMemory

    with _rw_lock:
        memory = _get_mem()
        if isinstance(memory, RemoteMemory):
            raise HTTPException(
                501, "Use the hosted service directly to record a decision."
            )
        decision = DecisionManager(memory).create(**body.model_dump())
        return {"decision_id": decision.decision_id}
