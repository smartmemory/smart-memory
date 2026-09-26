"""Automatic memory lifecycle engine for DIST-AGENT-HOOKS-1.

Manages the 6-phase lifecycle: Orient, Recall, Observe, Distill, Learn, Persist.
Each CLI invocation creates a short-lived instance that loads/saves session-scoped
state from $SMARTMEMORY_DATA_DIR/sessions/<session_id>.json.

All storage operations delegate to smartmemory_app.storage.* functions.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any

from smartmemory_app import recall_format
from smartmemory_app.lifecycle_config import LifecycleConfig, RecallStrategy
from smartmemory_app.recall_format import (
    budget_blocks,
    derive_workspace_id,
    record_hook_error,
    record_hook_degradation,
    skip_injection,
)


def _traced_injection(method: Callable) -> Callable:
    """Trace the final hook payload once, including gated and failing calls."""

    @wraps(method)
    def wrapped(self: MemoryLifecycle, *args: Any, **kwargs: Any) -> str:
        bound = signature(method).bind(self, *args, **kwargs)
        cwd = bound.arguments.get("cwd")
        query = bound.arguments.get("prompt")
        started = recall_format.time_ms()
        diagnostic = {
            "ranked_ids": [],
            "errors": [],
            "degradations": [],
            "snapshot_used": False,
        }
        token = recall_format._ACTIVE_TRACE.set(diagnostic)
        output = ""
        workspace_id = None
        try:
            workspace_id = derive_workspace_id(cwd)
            output = method(self, *args, **kwargs)
        except Exception as exc:
            record_hook_error(f"{method.__name__} lost injected context", exc)
        finally:
            recall_format._ACTIVE_TRACE.reset(token)
            ranked_ids = diagnostic["ranked_ids"] or recall_format.payload_ids(output)
            recall_format._trace(
                phase=method.__name__,
                session_id=self.session_id,
                workspace_id=workspace_id,
                cwd=cwd,
                query=query,
                candidate_count=len(ranked_ids),
                emitted=len(recall_format.payload_ids(output)),
                snapshot_used=diagnostic["snapshot_used"],
                latency_ms=recall_format.time_ms() - started,
                ranked_ids=ranked_ids,
                payload=output,
                error="; ".join(diagnostic["errors"]) or None,
                degradations=diagnostic["degradations"],
                excluded_non_memory=diagnostic.get("excluded_non_memory", 0),
                skipped_reason=diagnostic.get("skipped_reason")
                or ("empty" if not output else None),
            )
        return output

    return wrapped


def as_text(value: Any) -> str:
    """Coerce a hook payload field to text.

    Claude Code sends `tool_response` (and some `error` payloads) as a JSON
    OBJECT, not a string. The lifecycle phases build their memory text by
    slicing this value, and slicing a dict raises
    ``KeyError: slice(None, N, None)`` — which fires BEFORE the defensive
    try/except inside those phases, so the "Observe ingest failed" warning
    never runs. The hook wrapper then swallows it (`2>/dev/null`, `&`,
    `exit 0`), and the phase writes nothing while reporting success.

    That is why `hook:observe` / `hook:learn` items were absent from every
    graph: the path was crashing, not disabled.

    Canonical home (was private to cli.py). The daemon's lifecycle_api took the
    same payloads RAW, so routing hooks through the warm daemon would have
    reintroduced that exact crash on the other side of the wire. Both entry
    points now share this one coercion.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        log.warning("Hook payload lost JSON representation; using string: %s", exc)
        return str(value)


log = logging.getLogger(__name__)

# Trivial prompts that never trigger recall
_SKIP_TOKENS = {"yes", "no", "ok", "sure", "y", "n", "k", "yep", "nope", "yeah"}
_MAX_SKIP_TOKENS = 3

# Approximate token counting: 1 token ≈ 4 chars (conservative)
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


class MemoryLifecycle:
    """Automatic memory lifecycle for agent sessions.

    Not a singleton — each CLI invocation creates an instance, loads session
    state, executes one phase, saves state.
    """

    def __init__(self, session_id: str, config: LifecycleConfig | None = None):
        self.session_id = session_id
        self._config = config or LifecycleConfig()

        # Session state (persisted to sessions/<session_id>.json)
        self._current_user_turn: str | None = None
        self._last_assistant_message: str | None = None
        self._last_injection_embedding: list[float] | None = None
        self._last_recalled_prompt: str | None = None
        self._turn_count: int = 0
        self._observation_count: int = 0
        self._config_overrides: dict = {}
        self._rules_card_ids: list[str] = []
        self._rules_card_workspace: str | None = None

        self._load_state()

        # Apply session overrides if any
        if self._config_overrides:
            self._config = self._config.apply_overrides(self._config_overrides)

    # ── Phase methods ──────────────────────────────────────────────────

    @_traced_injection
    def orient(self, cwd: str | None = None) -> str:
        """Phase 1: Session start — recall previous context with progressive disclosure.

        Returns the separately budgeted rules card followed by orient_budget context.
        Clears stale session state for fresh start.
        """
        if not self._config.enabled:
            skip_injection("disabled")
            return ""

        # Clear state for new session
        self._current_user_turn = None
        self._last_assistant_message = None
        self._last_injection_embedding = None
        self._last_recalled_prompt = None
        self._turn_count = 0
        self._observation_count = 0

        from smartmemory_app.storage import recall

        self._rules_card_ids = []
        self._rules_card_workspace = derive_workspace_id(cwd)
        card = ""
        if self._config.rules_card_enabled:
            try:
                from smartmemory_app.rules_card import build_rules_card

                card = build_rules_card(cwd, budget=self._config.rules_card_budget)
                self._rules_card_ids = recall_format.payload_ids(card)
            except Exception as exc:
                record_hook_degradation("Orient lost rules card", exc)

        # Get recent + relevant memories
        exclusions = (
            {"exclude_ids": self._rules_card_ids} if self._rules_card_ids else {}
        )
        context = recall(cwd, top_k=10, **exclusions)

        # Also look for patterns and decisions if cwd provided. This MUST go
        # through the workspace-scoped recall path, not storage.search: search
        # takes no cwd, so in remote mode it sends only the config team_id and
        # returns the shared team's memories regardless of which project this
        # session is in (the sibling of the recall-hook leak fixed in 7bbeb63).
        patterns = ""
        if cwd:
            try:
                patterns = recall(
                    cwd,
                    top_k=5,
                    query=f"patterns conventions decisions for {os.path.basename(cwd)}",
                    include_snapshot=False,
                    **exclusions,
                )
            except Exception as exc:
                record_hook_degradation("Orient lost patterns context", exc)

        output = "\n\n".join(
            part
            for part in (card, self._format_orient_block(context, patterns))
            if part
        )
        self._save_state()
        return output

    @_traced_injection
    def recall(self, prompt: str, cwd: str | None = None) -> str:
        """Phase 2: Per-prompt recall — always captures prompt, optionally injects context.

        Scoped by `cwd` → workspace (HOOK-RECALL-RELEVANCE-1). Routing through
        storage.recall (not storage.search) matters in remote mode: search sends
        only the config team_id, so every session recalled from the shared team
        (the Cabbage demo corpus leaked into forge sessions, 2026-08-23..27).

        Always stores prompt as _current_user_turn for distill pairing.
        Returns formatted context if recall gate passes, empty string otherwise.
        """
        if not self._config.enabled:
            skip_injection("disabled")
            return ""

        # Always capture prompt for distill pairing (unconditional)
        self._current_user_turn = prompt
        self._turn_count += 1

        # Check if recall should fire
        if not self._should_recall(prompt):
            skip_injection(
                "deduped" if prompt.strip() == self._last_recalled_prompt else "gated"
            )
            self._save_state()
            return ""

        from smartmemory_app.storage import recall as scoped_recall

        try:
            exclusions = {}
            if (
                self._rules_card_ids
                and self._rules_card_workspace == derive_workspace_id(cwd)
            ):
                exclusions["exclude_ids"] = self._rules_card_ids
            block = scoped_recall(
                cwd, top_k=5, query=prompt, include_snapshot=False, **exclusions
            )
        except Exception as e:
            record_hook_error("Recall lost search context", e)
            self._save_state()
            return ""

        if not block:
            self._save_state()
            return ""

        output = self._trim_to_budget(block)

        # Cache prompt for dedup and topic comparison
        self._last_recalled_prompt = prompt
        self._cache_embedding(prompt)

        self._save_state()
        return output

    def observe(
        self,
        tool_name: str,
        tool_input: dict,
        tool_result: str,
        transcript_path: str | None = None,
        cwd: str | None = None,
    ) -> None:
        """Phase 3: Capture tool call as observation. Async via storage.ingest().

        Augmented (CORE-CODE-PROVENANCE-1 Phase 2a): for Edit/Write/MultiEdit, ALSO
        persist full-payload code-authorship evidence (tier-4, hidden) anchored to
        this session, so code can be traced back to the conversation that wrote it.
        The two writes are failure-isolated — a provenance error never regresses the
        episodic capture. (PostToolUse fires post-success; failures route to learn.)"""
        if not self._config.enabled or not self._config.observe_tool_calls:
            return

        input_summary = json.dumps(tool_input)[:200] if tool_input else ""
        result_summary = (tool_result or "")[:300]
        text = f"Tool `{tool_name}` called. Input: {input_summary}. Result: {result_summary}"

        from smartmemory_app.storage import ingest

        try:
            # origin MUST be the explicit kwarg — it is a reserved key stripped from
            # `properties` (DIST-LITE-QUIET-1), so the old properties= form silently
            # stored origin="unknown". (CORE-CODE-PROVENANCE-1 Phase 2a fix.)
            ingest(
                text,
                memory_type="episodic",
                origin="hook:observe",
                properties={"workspace_id": derive_workspace_id(cwd)},
            )
            self._observation_count += 1
            self._save_state()
        except Exception as e:
            log.warning("Observe ingest failed; tool observation lost: %s", e)

        # CORE-CODE-PROVENANCE-1 Phase 2a — durable code-authorship evidence, in its
        # OWN try/except so a failure here cannot regress the episodic write above.
        try:
            if tool_name in ("Edit", "Write", "MultiEdit"):
                if not transcript_path:
                    # No transcript_path -> we cannot form a read-back source handle, so
                    # provenance is skipped. Surface it once (no-silent-degradation) rather
                    # than dropping the capture invisibly.
                    if not getattr(self, "_warned_no_transcript", False):
                        log.warning(
                            "Provenance capture skipped: no transcript_path on %s event; "
                            "code-authorship evidence will not be persisted for this session.",
                            tool_name,
                        )
                        self._warned_no_transcript = True
                else:
                    from smartmemory.provenance.evidence import SessionEdits
                    from smartmemory.provenance.extract import cc_live_evidence
                    from smartmemory_app.storage import persist_provenance

                    rows = cc_live_evidence(
                        tool_name=tool_name,
                        tool_input=tool_input or {},
                        session_id=self.session_id,
                        source_path=transcript_path,
                    )
                    if rows:
                        persist_provenance(
                            SessionEdits(
                                source="cc",
                                source_path=transcript_path,
                                session_id=self.session_id,
                                cwd=cwd,
                                repo=None,
                                edits=rows,
                            )
                        )
        except Exception as e:
            log.warning(
                "Provenance persist failed; code-authorship evidence lost: %s", e
            )

    def distill(self, response: str, cwd: str | None = None) -> None:
        """Phase 4: Pair assistant response with stored prompt, save turn pair.

        Called by Stop hook with last_assistant_message.
        """
        if not self._config.enabled or not self._config.distill_turns:
            return

        self._last_assistant_message = response

        user_turn = self._current_user_turn or ""
        if not user_turn and not response:
            self._save_state()
            return

        # Format as distill pair
        pair = f"User: {user_turn[:500]}\nAssistant: {response[:1000]}"

        from smartmemory_app.storage import ingest

        try:
            ingest(
                pair,
                memory_type="pending",
                origin="lifecycle:distill",
                properties={"workspace_id": derive_workspace_id(cwd)},
            )
        except Exception as e:
            log.warning("Distill ingest failed; turn pair lost: %s", e)

        # Clear current turn (consumed)
        self._current_user_turn = None
        self._save_state()

    def learn(self, tool_name: str, error: str, cwd: str | None = None) -> None:
        """Phase 5: Capture tool failure as episodic memory."""
        if not self._config.enabled or not self._config.learn_from_errors:
            return

        text = f"Error in `{tool_name}`: {error[:500]}"

        from smartmemory_app.storage import ingest

        try:
            ingest(
                text,
                memory_type="episodic",
                origin="hook:learn",
                properties={"workspace_id": derive_workspace_id(cwd)},
            )
        except Exception as e:
            log.warning("Learn ingest failed; error memory lost: %s", e)

    def persist(
        self, cwd: str | None = None, transcript_path: str | None = None
    ) -> None:
        """Phase 6: durably enqueue the session transcript, then clean up state."""
        if not self._config.enabled:
            self._delete_state()
            return

        from smartmemory_app import capture_queue

        try:
            job = capture_queue.enqueue(
                self.session_id, transcript_path, derive_workspace_id(cwd), cwd
            )
            if job["status"] == "queued":
                capture_queue.spawn_worker()
        except Exception as exc:
            log.warning("Persist transcript capture could not start: %s", exc)

        # The full transcript contains the final response. Even without a path,
        # Stop/Distill already captured _last_assistant_message; writing its
        # truncated summary again duplicates it and would block exit on ingest.
        self._delete_state()

    # ── Recall gate ────────────────────────────────────────────────────

    def _should_recall(self, prompt: str) -> bool:
        """Decide whether to fire recall for this prompt."""
        strategy = self._config.recall_strategy

        # Session-only: never recall per-prompt
        if strategy == RecallStrategy.SESSION_ONLY:
            return False

        # Trivial-skip rules (apply to topic_change and every_prompt)
        stripped = prompt.strip()
        if not stripped:
            return False
        if (
            len(stripped.split()) <= _MAX_SKIP_TOKENS
            and stripped.lower() in _SKIP_TOKENS
        ):
            return False
        if stripped.startswith("/"):
            return False  # slash commands
        if stripped == self._last_recalled_prompt:
            return False  # dedup

        # Every-prompt: always recall after trivial gate
        if strategy == RecallStrategy.EVERY_PROMPT:
            return True

        # Topic-change: compare embedding similarity
        if strategy == RecallStrategy.TOPIC_CHANGE:
            if self._last_injection_embedding is None:
                return True  # first recall in session
            return self._topic_changed(prompt)

        return False

    def _topic_changed(self, prompt: str) -> bool:
        """Check if prompt topic diverges from last injection."""
        if self._last_injection_embedding is None:
            return True
        try:
            from smartmemory.plugins.embedding import create_embeddings

            embedding = create_embeddings(prompt)
            if embedding is None:
                record_hook_degradation(
                    "Recall lost topic gating", "embedding unavailable"
                )
                return True
            similarity = self._cosine_similarity(
                embedding, self._last_injection_embedding
            )
            return similarity < self._config.topic_threshold
        except Exception as exc:
            record_hook_degradation(
                "Recall lost topic gating; recalling without similarity", exc
            )
            return True  # fail open — recall when unsure

    def _cache_embedding(self, prompt: str) -> None:
        """Cache the prompt embedding for topic comparison."""
        self._last_injection_embedding = None
        try:
            from smartmemory.plugins.embedding import create_embeddings

            embedding = create_embeddings(prompt)
            self._last_injection_embedding = (
                [float(value) for value in embedding] if embedding is not None else None
            )
            if self._last_injection_embedding is None:
                record_hook_degradation(
                    "Recall lost cached topic embedding", "embedding unavailable"
                )
        except Exception as exc:
            record_hook_degradation("Recall lost cached topic embedding", exc)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ── Formatting ─────────────────────────────────────────────────────

    def _format_orient_block(self, context: str, patterns: str) -> str:
        """Build layered Orient context block within budget.

        `patterns` is a pre-formatted recall block (header line + `- ` entries)
        from the workspace-scoped storage.recall path; its header is dropped and
        the entries re-hung under `## Patterns`.
        """
        return budget_blocks(
            [(context, None), (patterns, "## Patterns")], self._config.orient_budget
        )

    def _trim_to_budget(self, block: str) -> str:
        """Apply the recall budget to complete items, retaining later items that fit."""
        return budget_blocks(
            [(block, None)],
            self._config.recall_budget,
            query=self._current_user_turn or "",
        )

    def _format_recall_block(self, results: list[dict]) -> str:
        """Build Recall context block within budget using the shared formatter."""
        return recall_format.format_recall_lines(
            results, top_k=len(results), budget=self._config.recall_budget
        )

    # ── Session state persistence ──────────────────────────────────────

    def _state_dir(self) -> Path:
        data_dir = os.environ.get(
            "SMARTMEMORY_DATA_DIR", str(Path.home() / ".smartmemory")
        )
        d = Path(data_dir) / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _state_path(self) -> Path:
        # Sanitize session_id to prevent path traversal
        safe_id = "".join(c for c in self.session_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = "unknown"
        return self._state_dir() / f"{safe_id}.json"

    def _load_state(self) -> None:
        try:
            path = self._state_path()
            if not path.exists():
                return
            data = json.loads(path.read_text())
            self._current_user_turn = data.get("current_user_turn")
            self._last_assistant_message = data.get("last_assistant_message")
            self._last_injection_embedding = data.get("last_injection_embedding")
            self._last_recalled_prompt = data.get("last_recalled_prompt")
            self._turn_count = data.get("turn_count", 0)
            self._observation_count = data.get("observation_count", 0)
            self._config_overrides = data.get("config_overrides", {})
            self._rules_card_ids = data.get("rules_card_ids", [])
            self._rules_card_workspace = data.get("rules_card_workspace")
        except (json.JSONDecodeError, OSError) as e:
            record_hook_degradation(
                "Failed to load session state; prior session context lost", e
            )

    def _save_state(self) -> None:
        data = {
            "session_id": self.session_id,
            "current_user_turn": self._current_user_turn,
            "last_assistant_message": self._last_assistant_message,
            "last_injection_embedding": self._last_injection_embedding,
            "last_recalled_prompt": self._last_recalled_prompt,
            "turn_count": self._turn_count,
            "observation_count": self._observation_count,
            "config_overrides": self._config_overrides,
            "rules_card_ids": self._rules_card_ids,
            "rules_card_workspace": self._rules_card_workspace,
            "updated_at": time.time(),
        }
        try:
            path = self._state_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.rename(path)
        except OSError as e:
            record_hook_degradation(
                "Failed to save session state; current session updates lost", e
            )

    def _delete_state(self) -> None:
        try:
            self._state_path().unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to delete session state; stale state retained: %s", exc)

    @classmethod
    def cleanup_stale_sessions(cls, max_age_hours: int = 24) -> int:
        """Delete session state files older than max_age_hours. Returns count deleted."""
        data_dir = os.environ.get(
            "SMARTMEMORY_DATA_DIR", str(Path.home() / ".smartmemory")
        )
        sessions_dir = Path(data_dir) / "sessions"
        if not sessions_dir.exists():
            return 0
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0
        for f in sessions_dir.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except OSError as exc:
                log.warning("Failed to clean stale session state %s: %s", f, exc)
        return deleted
