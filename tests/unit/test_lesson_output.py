"""Offline producer contract and historical post-processing regressions."""

import json
from pathlib import Path

import pytest

from smartmemory_app.lesson_output import SCHEMA, parse_output
from smartmemory_app.session_lessons import SessionLessonExtractor
from smartmemory.plugins.extractors.reasoning import ReasoningExtractorConfig


def conversation(rule, assistant="Understood. I will follow this standing rule."):
    return [
        {"role": "user", "content": rule},
        {"role": "assistant", "content": assistant},
    ]


def test_contract_matches_documented_schema():
    contract = json.loads(
        (Path(__file__).parents[2] / "docs/session-lesson-contract.json").read_text()
    )
    assert {
        k: v for k, v in contract.items() if k not in {"$schema", "title"}
    } == SCHEMA


@pytest.mark.parametrize(
    "response",
    [
        '[{"type":',
        "{}",
        "null",
        '[{"type":"bogus","content":"x"}]',
        '[{"type":"conclusion","content":"x","extra":1}]',
        '[{"type":"conclusion","content":"   "}]',
        "```json\n[]\n```",
    ],
)
def test_refusal_logs_warning_without_repair(response, caplog):
    assert (
        parse_output(
            response, conversation("In this project, never send live email from tests.")
        )
        is None
    )
    assert "refused" in caplog.text and "lessons lost" in caplog.text
    assert all(record.levelname == "WARNING" for record in caplog.records)


def test_source_repair_keeps_long_rule_and_exception(caplog):
    rule = (
        "In this project, never discard "
        + "completed checkpoint " * 75
        + "except after owner approval."
    )
    extractor = SessionLessonExtractor(ReasoningExtractorConfig(min_steps=1))
    extractor.trusted_turns = conversation(rule)
    trace = extractor._parse_llm_response(
        '[{"type":"decision","content":"Assistant will comply."}]',
        json.dumps(extractor.trusted_turns),
    )
    assert len(trace.steps) == 1
    assert trace.steps[0].type == "conclusion"
    assert trace.steps[0].content == "CONSTRAINT: User-stated rule: " + rule
    assert "deterministic repair" in caplog.text


def test_does_not_displace_independent_finding():
    rule = "In this project, never retry 409."
    rows = [
        {"type": "conclusion", "content": "CONSTRAINT: User-stated rule: " + rule},
        {
            "type": "conclusion",
            "content": "The retry client now returns 409 without retry.",
        },
    ]
    assert (
        parse_output(
            json.dumps(rows), conversation(rule, "Implemented the retry guard.")
        )
        == rows
    )


def test_unsupported_attribution_refuses(caplog):
    rows = [{"type": "conclusion", "content": "User-stated rule: Never retry."}]
    assert (
        parse_output(
            json.dumps(rows), conversation("Fix the retry helper.", "Implemented.")
        )
        is None
    )
    assert "unsupported attribution" in caplog.text


def test_exact_dedup_keeps_distinct_negation():
    rows = [
        {"type": "conclusion", "content": "HTTP 409 is not retried."},
        {"type": "conclusion", "content": "HTTP 429 is retried."},
    ]
    assert (
        parse_output(
            json.dumps(rows + rows[:1]), conversation("Fix retries.", "Implemented.")
        )
        == rows
    )


def test_prompt_requires_exact_rule_and_strict_output():
    prompt = SessionLessonExtractor()._build_extraction_prompt("source sentinel")
    for requirement in (
        "strict JSON",
        "exactly type and content",
        "Do not add quote marks",
        "One conclusion per explicit standing rule",
        "bare acknowledgment",
        "negation",
        "exceptions",
        "identifier category",
        "source sentinel",
    ):
        assert requirement in prompt


def test_historical_20_outputs_replay_offline(tmp_path, caplog):
    import runpy

    root = Path(__file__).parents[2]
    gate = runpy.run_path(str(root / "scripts/check_rule_quality.py"))
    replay = runpy.run_path(str(root / "scripts/replay_session_lessons.py"))["replay"]
    gate["emit_transcripts"](tmp_path)
    rows = [
        json.loads(line)
        for line in (root / "tests/fixtures/hook_rule_quality_recorded.jsonl")
        .read_text()
        .splitlines()
    ]
    output = replay(rows, tmp_path)
    assert len(output) == 20
    assert gate["evaluate"](output) == [
        "sm-hooks-rule-14: extraction error, rule lost",
        "sm-hooks-rule-14: expected one conclusion, got 0, rule missing or ambiguous",
    ]
    assert "refused" in caplog.text
    assert "deterministic repair" in caplog.text


def test_multiple_rules_and_questions_are_not_collapsed():
    rows = [
        {"type": "conclusion", "content": "First distinct finding."},
        {"type": "conclusion", "content": "Second distinct finding."},
    ]
    for source in (
        "In this project, never retry. Always preserve errors.",
        "In this project, should we retry?",
        "Add date validation to this helper in this project.",
        "In this project, implement the export helper.",
    ):
        assert parse_output(json.dumps(rows), conversation(source)) == rows


def test_rule_only_probe_uses_source_kind_without_classifier(
    tmp_path, monkeypatch, capsys
):
    import runpy
    from scripts import check_session_lesson_extraction as probe

    root = Path(__file__).parents[2]
    gate = runpy.run_path(str(root / "scripts/check_rule_quality.py"))
    gate["emit_transcripts"](tmp_path)
    # Transport stub only. Real transcript parser, producer parser, trace and
    # DecisionExtractor run. This verifies wiring, not live model quality.
    monkeypatch.setattr(
        "smartmemory.plugins.extractors.reasoning.call_llm",
        lambda **kwargs: (
            None,
            json.dumps(
                [
                    {
                        "type": "conclusion",
                        "content": "CONSTRAINT: User-stated rule: "
                        + json.loads(kwargs["user_content"].split("\n\n")[-1])[0][
                            "content"
                        ],
                    }
                ]
            ),
        ),
    )

    classified = []

    def classifier(contents, receipt):
        classified.extend(contents)
        return ["constraint"] * len(contents)

    monkeypatch.setattr(probe, "classify_lessons", classifier)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only")
    for transcript in sorted(tmp_path.glob("*.jsonl")):
        assert probe.inspect_transcript(str(transcript)) == {
            "constraint": 1,
            "finding": 0,
        }
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert gate["evaluate"](rows) == []
    assert classified == []  # Scoped definitions use the standing-policy bypass too.


def test_overflow_keeps_rule_and_warns_about_findings(caplog):
    rule = "For future exports, retain the original slug."
    rows = [
        {"type": "conclusion", "content": f"Distinct finding {i}."} for i in range(10)
    ]
    rows.append(
        {"type": "conclusion", "content": "CONSTRAINT: User-stated rule: " + rule}
    )
    output = parse_output(
        json.dumps(rows), conversation(rule, "Implemented export helper.")
    )
    assert len(output) == 8
    assert output[-1]["content"] == "CONSTRAINT: User-stated rule: " + rule
    assert "discarded" in caplog.text
