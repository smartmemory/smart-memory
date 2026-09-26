"""Session conclusions through core's reasoning and decision extractors."""

import json
import logging
import os
import re

from filelock import FileLock

from smartmemory.plugins.extractors.reasoning import (
    ReasoningExtractor,
    ReasoningExtractorConfig,
)
from smartmemory.utils.llm import get_last_usage
from smartmemory.utils.token_tracking import COST_PER_1K_TOKENS

log = logging.getLogger(__name__)
ORIGIN = "import:claude_code:lesson"


def _lesson_usage(usage, model):
    """Price only reported tokens; core does not expose SDK attempt totals."""
    record = {
        "provider": "groq",
        "model": (usage or {}).get("model") or model,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "cached_tokens": (usage or {}).get("cached_tokens"),
        "call_count": None,
        "call_count_status": "unmeasured",
        "usage_source": "smartmemory.utils.llm.get_last_usage"
        if usage is not None
        else "unmeasured",
        "usage_scope": "final_response_only",
        "cost_usd": None,
        "cost_status": "unpriced",
    }
    measured = all(
        isinstance(record[key], int)
        and not isinstance(record[key], bool)
        and record[key] >= 0
        for key in ("prompt_tokens", "completion_tokens")
    )
    if not measured:
        record["usage_source"] = "unmeasured"
        log.warning(
            "Session lesson token usage unmeasured: core returned no usable usage"
        )
    pricing = COST_PER_1K_TOKENS.get(record["model"])
    if measured and pricing and record["model"] != "default":
        record["cost_usd"] = (
            record["prompt_tokens"] * pricing["prompt"]
            + record["completion_tokens"] * pricing["completion"]
        ) / 1000
        record["cost_status"] = "repository_price"
        record["cost_source"] = {
            "table": "smartmemory.utils.token_tracking.COST_PER_1K_TOKENS",
            "model": record["model"],
            "unit": "USD/1000 tokens",
            "prompt": pricing["prompt"],
            "completion": pricing["completion"],
        }
    log.warning(
        "Session lesson usage is final-response only: core does not expose SDK "
        "attempt counts or cached-token details; retries are unmeasured"
    )
    return record


class SessionLessonExtractor(ReasoningExtractor):
    """Keep the final turn; core's generic prompt only reads the first 4k chars."""

    lesson_usage = None

    def _extract_implicit(self, text):
        # Ingestion shares this context; never attribute its last usage to lessons.
        get_last_usage()
        try:
            return super()._extract_implicit(text)
        finally:
            self.lesson_usage = _lesson_usage(get_last_usage(), self.cfg.model_name)

    def _likely_contains_reasoning(self, text):
        return bool(text.strip())

    def _build_extraction_prompt(self, text):
        return (
            "Extract durable engineering findings, constraints and resolved decisions "
            "from this conversation, especially its final conclusions. Ignore abandoned "
            "hypotheses and requests that were not resolved. Do not invent findings. "
            "Return a JSON array of objects with type and content. Every rule, "
            "constraint, requirement, limit, required format or resolved decision is "
            "its own conclusion, even when it also supports a broader conclusion. Use "
            "observation only for incidental context such as what was tried or checked "
            "(at most 8 conclusions). Make each conclusion self-contained: name the "
            "system, counterparty or component it applies to and the task it matters "
            "for (for example 'Stripe webhook retries: ...'), because it will be read "
            "and searched without this conversation. "
            "Preserve exact identifiers and limitations. "
            "Quote the conclusion text where possible. Return [] if none.\n\n" + text
        )


# A stated rule survives even when the model files it as an observation: live Groq
# output typed "a reversal's E2E ID must be exactly RV + the original ID" as
# `observation`, which DecisionExtractor discards (DEMO-CC-UPLIFT-1 Stage 2a).
_RULE = re.compile(
    r"\b(?:must|never|exactly|only|required?|requires|not allowed|cannot|can't"
    r"|at (?:most|least)|rejects?|rejected)\b|(?-i:\b[A-Z]{2,}-\d{2,}\b)",
    re.IGNORECASE,
)


def _promote_rules(trace):
    promoted = 0
    for step in trace.steps:
        if step.type == "observation" and _RULE.search(step.content or ""):
            step.type = "conclusion"
            promoted += 1
    return promoted


def capture_lessons(mem, job, turns, item_ids, session_date=None):
    from smartmemory_app.lesson_lifecycle import journal_path

    try:
        path = journal_path(job)
        with FileLock(str(path) + ".lock"):
            return _capture_lessons(mem, job, turns, item_ids, session_date, path)
    except Exception as exc:
        message = f"Session {job['session_id']} lost session lessons: {exc}"
        log.warning(message)
        return {"degradation": message}


def _capture_lessons(mem, job, turns, item_ids, session_date, path):
    """A loss here degrades lessons only; a successful transcript stays done."""
    from smartmemory_app.lesson_lifecycle import apply_plan, classify, save_plan

    receipt = {}
    try:
        if path.exists():
            plan = json.loads(path.read_text())
            receipt = dict(plan["receipt"])
            receipt.pop("lesson_usage", None)  # retry performs no new LLM calls
            plan["receipt"] = receipt
            return apply_plan(mem, plan, path)
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
        try:
            trace = extractor.extract(text).get("reasoning_trace")
        finally:
            if extractor.lesson_usage is not None:
                receipt["lesson_usage"] = extractor.lesson_usage
        if trace is None:
            raise ValueError("reasoning extraction returned no usable trace")
        trace.session_id = job["session_id"]
        promoted = _promote_rules(trace)
        if promoted:
            receipt["rules_promoted"] = promoted
        from smartmemory.plugins.extractors.decision import DecisionExtractor

        decisions = DecisionExtractor().extract_from_trace(trace)
        if not decisions:
            raise ValueError("reasoning trace contained no lessons/decisions")
        decisions = list({d.content: d for d in decisions}.values())[:8]
        try:
            relations = classify(mem, decisions, job, turns, receipt)
        except Exception as exc:
            message = f"Session {job['session_id']} lost lesson lifecycle classification: {exc}"
            log.warning(message)
            receipt["degradation"] = message
            relations = []
        entries = []
        for index, decision in enumerate(decisions):
            context = {
                "workspace_id": job["workspace_id"],
                "session_id": job["session_id"],
                "transcript_path": job["transcript_path"],
                "session_date": session_date,
                "lesson_operation": f"{path.stem}:{index}",
            }
            spans = [
                [i, i + 1]
                for i, turn in enumerate(turns)
                if decision.content in turn["content"]
            ]
            if spans:
                context["turn_range"] = spans[-1]
            entries.append(
                {
                    "context_snapshot": context,
                    "decision": dict(
                        decision_id=decision.generate_id(),
                        content=decision.content,
                        decision_type=decision.decision_type,
                        confidence=decision.confidence,
                        source_type="reasoning",
                        source_session_id=job["session_id"],
                        evidence_ids=item_ids,
                        origin=ORIGIN,
                    ),
                }
            )
        plan = {
            "receipt": receipt,
            "entries": entries,
            "relations": relations,
            "origin": ORIGIN,
        }
        save_plan(path, plan)
        return apply_plan(mem, plan, path)
    except Exception as exc:
        message = f"Session {job['session_id']} lost session lessons: {exc}"
        log.warning(message)
        return {**receipt, "degradation": message}
