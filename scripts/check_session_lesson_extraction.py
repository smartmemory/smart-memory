"""Live, read-only lesson extraction check for agent transcript JSONL files."""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from smartmemory.importers.agent_transcript import parse_transcript
from smartmemory.plugins.extractors.decision import DecisionExtractor
from smartmemory.plugins.extractors.reasoning import ReasoningExtractorConfig

from smartmemory_app.lesson_classifier import classify_lessons
from smartmemory_app.session_lessons import (
    SessionLessonExtractor,
    _choose_lesson_kind,
    _normalize_lesson_content,
    _prepare_lesson_decisions,
    _promote_rules,
)


def inspect_transcript(transcript):
    conversation, _ = parse_transcript(transcript, min_turns=1)
    if conversation is None:
        raise ValueError(f"{transcript}: transcript contains no conversational turns")
    turns = conversation.turns
    text = "\n\n".join(
        f"Turn {i} ({turn['role']}):\n{turn['content']}" for i, turn in enumerate(turns)
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
        raise ValueError(f"{transcript}: reasoning extraction returned no usable trace")
    _promote_rules(trace)
    decisions = DecisionExtractor().extract_from_trace(trace)
    types = {}
    for step in trace.steps:
        content = (step.content or "").strip()
        if step.type in {"decision", "conclusion"}:
            types[_normalize_lesson_content(content)[0]] = step.type
    decisions, tagged_by_content, attributed_by_content = _prepare_lesson_decisions(
        decisions
    )
    if not decisions:
        raise ValueError(
            f"{transcript}: reasoning trace contained no lessons/decisions"
        )
    # A failed classifier is a failed live measurement, not a successful fallback.
    kinds = classify_lessons([d.content for d in decisions], {})
    counts = {"constraint": 0, "finding": 0}
    for decision, classifier_kind in zip(decisions, kinds):
        kind, kind_source = _choose_lesson_kind(
            decision.content,
            tagged_by_content[decision.content],
            attributed_by_content[decision.content],
            classifier_kind,
        )
        counts[kind] += 1
        print(
            json.dumps(
                {
                    "transcript": transcript,
                    "type": types[decision.content],
                    "kind": kind,
                    "tagged": tagged_by_content[decision.content],
                    "kind_source": kind_source,
                    "content": decision.content,
                },
                ensure_ascii=False,
            )
        )
    print(json.dumps({"transcript": transcript, "counts": counts}))
    return counts


def load_baseline(path):
    """Match repeated transcript measurements by path and occurrence, in order."""
    runs = defaultdict(list)
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        if "transcript" in row and ("counts" in row or "error" in row):
            counts = row.get("counts")
            if counts is not None and any(
                type(counts.get(kind)) is not int or counts[kind] < 0
                for kind in ("constraint", "finding")
            ):
                raise ValueError("baseline counts must be nonnegative integers")
            runs[row["transcript"]].append(counts)
    return runs


def print_deltas(runs, baseline):
    # Keep stdout JSONL reusable as the next baseline; the table goes to stderr.
    print("Transcript\tRun\tBase C/F\tNew C/F\tDelta C/F", file=sys.stderr)
    occurrences = defaultdict(int)
    for transcript, counts in runs:
        index = occurrences[transcript]
        occurrences[transcript] += 1
        previous = baseline.get(transcript, [])
        before = previous[index] if index < len(previous) else None
        old_text = f"{before['constraint']}/{before['finding']}" if before else "N/A"
        new_text = f"{counts['constraint']}/{counts['finding']}" if counts else "ERROR"
        delta = (
            f"{counts['constraint'] - before['constraint']:+d}/"
            f"{counts['finding'] - before['finding']:+d}"
            if counts is not None and before is not None
            else "N/A"
        )
        print(
            f"{transcript}\t{index + 1}\t{old_text}\t{new_text}\t{delta}",
            file=sys.stderr,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "transcripts", nargs="+", help="Claude Code or Codex transcript JSONL files"
    )
    parser.add_argument(
        "--baseline-json", help="Previous probe JSONL; print per-run deltas to stderr"
    )
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline_json) if args.baseline_json else None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(f"invalid baseline: {exc}")
    if not os.environ.get("GROQ_API_KEY"):
        parser.error("GROQ_API_KEY is required")
    os.environ["LLM_PROVIDER"] = "groq"
    os.environ["LLM_API_KEY"] = os.environ["GROQ_API_KEY"]
    os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"
    total = {"constraint": 0, "finding": 0, "errors": 0}
    runs = []
    for transcript in args.transcripts:
        try:
            counts = inspect_transcript(transcript)
        except Exception as exc:
            total["errors"] += 1
            runs.append((transcript, None))
            print(
                json.dumps(
                    {"transcript": transcript, "error": f"{type(exc).__name__}: {exc}"}
                )
            )
            continue
        runs.append((transcript, counts))
        for kind in ("constraint", "finding"):
            total[kind] += counts[kind]
    print(json.dumps({"total": total}))
    if baseline is not None:
        print_deltas(runs, baseline)


if __name__ == "__main__":
    main()
