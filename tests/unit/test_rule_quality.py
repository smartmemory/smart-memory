"""G10: fixed rule sample and offline acceptance checker, no model calls."""

import json

import pytest

from scripts import check_rule_quality as gate
from smartmemory_app.session_lessons import SessionLessonExtractor


def test_prompt_preserves_identifier_category():
    prompt = SessionLessonExtractor.__new__(
        SessionLessonExtractor
    )._build_extraction_prompt("sentinel")
    assert "ports are not error codes" in prompt
    assert "Quote each explicit standing rule verbatim" in prompt
    assert "negation, exceptions, scope and units" in prompt
    assert prompt.endswith("sentinel")


def rows():
    return [
        {
            "transcript": f"/isolated/{s['id']}.jsonl",
            "kind": s["kind"],
            "content": "User-stated rule: " + s["rule"],
        }
        for s in gate.load_sample()
    ]


def test_fixed_twenty_rules_pass_with_exact_meaning():
    sample = gate.load_sample()
    assert len(sample) == len({s["id"] for s in sample}) == 20
    assert gate.evaluate(rows()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "ports",
        "negation",
        "scope",
        "kind",
        "missing",
        "duplicate",
        "extra",
        "error",
        "unknown",
    ],
)
def test_semantic_and_receipt_failures_are_rejected(mutation):
    result = rows()
    if mutation == "ports":
        result[0]["content"] = result[0]["content"].replace("ports", "error codes")
    elif mutation == "negation":
        result[0]["content"] = result[0]["content"].replace("Never", "Always")
    elif mutation == "scope":
        result[0]["content"] = result[0]["content"].replace("in this project", "")
    elif mutation == "kind":
        result[0]["kind"] = "finding"
    elif mutation == "missing":
        result.pop()
    elif mutation == "duplicate":
        result.append(dict(result[0]))
    elif mutation == "extra":
        result.append({**result[0], "content": "Invented a different rule"})
    elif mutation == "error":
        result.append(
            {"transcript": result[0]["transcript"], "error": "extraction failed"}
        )
    else:
        result.append({**result[0], "transcript": "/unexpected.jsonl"})
    assert gate.evaluate(result)


def test_emitted_transcripts_roundtrip_through_real_parser(tmp_path):
    from smartmemory.importers.agent_transcript import parse_transcript

    gate.emit_transcripts(tmp_path)
    for sample in gate.load_sample():
        path = tmp_path / (sample["id"] + ".jsonl")
        conversation, _ = parse_transcript(str(path), min_turns=1)
        assert conversation.turns[0]["content"] == sample["rule"]
    with pytest.raises(FileExistsError):
        gate.emit_transcripts(tmp_path)


def test_cli_is_fail_closed_and_warns(tmp_path, capsys):
    path = tmp_path / "results.jsonl"
    path.write_text("")
    assert gate.main(["--results", str(path)]) == 1
    output = capsys.readouterr()
    assert "0/20" in output.out and "WARNING" in output.err
    assert "distill quality" in output.err
    path.write_text("\n".join(json.dumps(r) for r in rows()))
    assert gate.main(["--results", str(path)]) == 0
    assert "20/20" in capsys.readouterr().out


def test_malformed_results_do_not_pass(tmp_path, capsys):
    path = tmp_path / "results.jsonl"
    path.write_text("not json")
    assert gate.main(["--results", str(path)]) == 1
    assert "WARNING" in capsys.readouterr().err
