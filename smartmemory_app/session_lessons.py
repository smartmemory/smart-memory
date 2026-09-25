"""Session conclusions through core's reasoning and decision extractors."""

import logging
import os

from smartmemory.plugins.extractors.reasoning import (
    ReasoningExtractor,
    ReasoningExtractorConfig,
)

log = logging.getLogger(__name__)
ORIGIN = "import:claude_code:lesson"


class SessionLessonExtractor(ReasoningExtractor):
    """Keep the final turn; core's generic prompt only reads the first 4k chars."""

    def _likely_contains_reasoning(self, text):
        return bool(text.strip())

    def _build_extraction_prompt(self, text):
        return (
            "Extract durable engineering findings, constraints and resolved decisions "
            "from this conversation, especially its final conclusions. Ignore abandoned "
            "hypotheses and requests that were not resolved. Do not invent findings. "
            "Return a JSON array of objects with type and content. Use observation "
            "for supporting facts and conclusion for each self-contained lesson "
            "(at most 8 conclusions). Preserve exact identifiers and limitations. "
            "Quote the conclusion text where possible. Return [] if none.\n\n" + text
        )


def capture_lessons(mem, job, turns, item_ids, session_date=None):
    """A loss here degrades lessons only; a successful transcript stays done."""
    try:
        existing = mem._graph.search_nodes(
            {"memory_type": "decision", "origin": ORIGIN}
        )
        ids = []
        for item in existing:
            meta = item.metadata or {}
            context = meta.get("context_snapshot") or {}
            if (
                context.get("session_id") == job["session_id"]
                and context.get("workspace_id") == job["workspace_id"]
            ):
                ids.append(item.item_id)
        if ids:
            return {"lesson_ids": ids, "lessons_unchanged": True}
        if os.environ.get("SMARTMEMORY_CAPTURE_OFFLINE") == "1":
            raise RuntimeError("offline capture disables LLM session lessons")
        if not os.environ.get("GROQ_API_KEY"):
            raise RuntimeError("session lessons require GROQ_API_KEY")
        # Independently pin this call too: never inherit another provider/key.
        os.environ["LLM_PROVIDER"] = "groq"
        os.environ["LLM_API_KEY"] = os.environ["GROQ_API_KEY"]
        os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"
        text = "\n\n".join(
            f"Turn {i} ({turn['role']}):\n{turn['content']}"
            for i, turn in enumerate(turns)
        )
        extractor = SessionLessonExtractor(
            ReasoningExtractorConfig(
                prefer_explicit_markup=False,
                use_llm_detection=True,
                api_key_env="GROQ_API_KEY",
                model_name="openai/gpt-oss-120b",
                min_steps=1,
            )
        )
        trace = extractor.extract(text).get("reasoning_trace")
        if trace is None:
            raise ValueError("reasoning extraction returned no usable trace")
        trace.session_id = job["session_id"]
        from smartmemory.plugins.extractors.decision import DecisionExtractor

        decisions = DecisionExtractor().extract_from_trace(trace)
        if not decisions:
            raise ValueError("reasoning trace contained no lessons/decisions")
        seen = set()
        for decision in decisions:
            if decision.content in seen:
                continue
            seen.add(decision.content)
            context = {
                "workspace_id": job["workspace_id"],
                "session_id": job["session_id"],
                "transcript_path": job["transcript_path"],
                "session_date": session_date,
            }
            spans = [
                [i, i + 1]
                for i, turn in enumerate(turns)
                if decision.content in turn["content"]
            ]
            if spans:
                context["turn_range"] = spans[-1]
            stored = mem.add_decision(
                decision.content,
                decision_type=decision.decision_type,
                confidence=decision.confidence,
                source_type="reasoning",
                source_session_id=job["session_id"],
                evidence_ids=item_ids,
                context_snapshot=context,
                origin=ORIGIN,
            )
            ids.append(stored.decision_id)
            mem.update_properties(stored.decision_id, context)
            if len(ids) == 8:
                break
        return {"lesson_ids": ids}
    except Exception as exc:
        message = f"Session {job['session_id']} lost session lessons: {exc}"
        log.warning(message)
        return {"degradation": message}
