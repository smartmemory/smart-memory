"""SETUP-RESUMABLE-1: the saved progress of one ``sm setup`` run.

``setup-state.json`` sits next to ``config.toml`` (``config.config_path()``). It
records the choices of the run and, per step of ``setup_steps.setup_plan``, a status
(``pending | done | verified | failed``), when it changed and why a step failed. A
re-run of ``sm setup`` reads it to resume at the first step whose effect is gone.

It never holds a secret. Choices are names and paths (mode, provider names, model
names, data dir, API URL host and path, team id); an API key or LLM key stays where
setup already puts it (OS keychain, shell profile). ``save`` refuses a choice that is
not on the allow-list below, and failure reasons pass through ``redact_credentials``.

Writes are atomic: a temp file in the same folder, then ``os.replace``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA = 1
FILE_NAME = "setup-state.json"
PENDING, DONE, VERIFIED, FAILED = "pending", "done", "verified", "failed"
STATUSES = (PENDING, DONE, VERIFIED, FAILED)

# A failed optional step is a warning, not a stop (the helper-model download always
# was best effort). It does not keep a run from counting as complete.
OPTIONAL_KEYS = frozenset({"extra_models"})

# The only keys a saved choice may carry. A key added here must never hold a secret.
CHOICE_KEYS = frozenset(
    {
        "mode",
        "llm_provider",
        "llm_model",
        "embedding_provider",
        "spacy_model",
        "coreference",
        "data_dir",
        "seed_dir",
        "api_url",
        "team_id",
        "restart",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path() -> Path:
    """``setup-state.json`` in the CLI's config folder."""
    from smartmemory_app.config import config_path

    return config_path().parent / FILE_NAME


@dataclass
class StepRecord:
    key: str
    status: str = PENDING
    at: str = ""
    reason: str = ""


@dataclass
class SetupState:
    choices: dict
    steps: list[StepRecord]
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=_now)
    updated_at: str = ""
    verified_scope: str = ""
    schema: int = SCHEMA

    @classmethod
    def new(cls, choices: dict, keys: list[str]) -> "SetupState":
        return cls(choices=dict(choices), steps=[StepRecord(k) for k in keys])

    def record(self, key: str) -> StepRecord:
        for step in self.steps:
            if step.key == key:
                return step
        step = StepRecord(key)
        self.steps.append(step)
        return step

    def status(self, key: str) -> str:
        """A step's status, PENDING when it has no record (adds none)."""
        return next((s.status for s in self.steps if s.key == key), PENDING)

    def mark(self, key: str, status: str, reason: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown setup step status {status!r}")
        from smartmemory_app.diagnostics import redact_credentials

        step = self.record(key)
        step.status, step.at = status, _now()
        step.reason = redact_credentials(reason) if reason else ""

    @property
    def complete(self) -> bool:
        return bool(self.steps) and all(
            s.status == VERIFIED or (s.key in OPTIONAL_KEYS and s.status == FAILED)
            for s in self.steps
        )

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, indent=2, sort_keys=True)


def save(state: SetupState, path: Optional[Path] = None) -> Path:
    """Write the state atomically (owner-only). Raises on a choice outside CHOICE_KEYS."""
    unknown = set(state.choices) - CHOICE_KEYS
    if unknown:
        raise ValueError(
            "setup state refuses unknown choice keys: " + ", ".join(sorted(unknown))
        )
    path = path or state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.updated_at = _now()
    fd, tmp = tempfile.mkstemp(prefix=".setup-state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(state.to_json())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path: Optional[Path] = None) -> Optional[SetupState]:
    """The saved state, or None when there is none or it cannot be used.

    An unreadable file or another schema version is not resumed: setup logs a
    WARNING naming the reason and starts a fresh run.
    """
    path = path or state_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(
            "Setup progress file %s is unreadable (%s). Starting a fresh setup run.",
            path,
            type(exc).__name__,
        )
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        log.warning(
            "Setup progress file %s has schema %r, this version reads %d. "
            "Starting a fresh setup run.",
            path,
            raw.get("schema") if isinstance(raw, dict) else None,
            SCHEMA,
        )
        return None
    try:
        steps = [
            StepRecord(
                key=str(s["key"]),
                status=s["status"] if s.get("status") in STATUSES else PENDING,
                at=str(s.get("at", "")),
                reason=str(s.get("reason", "")),
            )
            for s in raw["steps"]
        ]
        choices = {k: v for k, v in dict(raw["choices"]).items() if k in CHOICE_KEYS}
        return SetupState(
            choices=choices,
            steps=steps,
            run_id=str(raw.get("run_id", "")),
            started_at=str(raw.get("started_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            verified_scope=str(raw.get("verified_scope", "")),
        )
    except Exception as exc:
        log.warning(
            "Setup progress file %s is malformed (%s). Starting a fresh setup run.",
            path,
            type(exc).__name__,
        )
        return None


def clear(path: Optional[Path] = None) -> None:
    path = path or state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
