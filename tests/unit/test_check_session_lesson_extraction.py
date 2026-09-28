"""Recorded-shape output checks for the read-only lesson probe."""

import json

import pytest
from types import SimpleNamespace

from scripts import check_session_lesson_extraction as probe


def test_multiple_transcripts_report_per_file_and_total(monkeypatch, capsys):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only")
    rule = "User-stated rule – The original workspace slug must be retained."
    detail = "Added helper X that converts other workspace slugs."

    def parse(path, min_turns):
        assert min_turns == 1
        return SimpleNamespace(turns=[{"role": "user", "content": path}]), None

    def extract(text):
        contents = [rule, detail] if "S1.jsonl" in text else [detail]
        steps = [
            SimpleNamespace(type="conclusion", content="CONSTRAINT: " + content)
            for content in contents
        ]
        return {"reasoning_trace": SimpleNamespace(steps=steps)}

    monkeypatch.setattr(probe, "parse_transcript", parse)
    monkeypatch.setattr(
        probe,
        "SessionLessonExtractor",
        lambda config: SimpleNamespace(extract=extract),
    )
    monkeypatch.setattr(probe, "_promote_rules", lambda trace: None)
    monkeypatch.setattr(
        probe,
        "DecisionExtractor",
        lambda: SimpleNamespace(
            extract_from_trace=lambda trace: [
                SimpleNamespace(content=step.content) for step in trace.steps
            ]
        ),
    )
    monkeypatch.setattr(
        probe,
        "classify_lessons",
        lambda texts, receipt: ["finding"] * len(texts),
    )

    probe.main(["/tmp/S1.jsonl", "/tmp/S2.jsonl"])
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["transcript"], row["kind"]) for row in rows if "kind" in row] == [
        ("/tmp/S1.jsonl", "constraint"),
        ("/tmp/S1.jsonl", "finding"),
        ("/tmp/S2.jsonl", "finding"),
    ]
    assert rows[0]["content"] == (
        "User-stated rule: The original workspace slug must be retained."
    )
    assert [(row["tagged"], row["kind_source"]) for row in rows if "kind" in row] == [
        (True, "attributed_rule"),
        (True, "classifier"),
        (True, "classifier"),
    ]
    assert rows[2] == {
        "transcript": "/tmp/S1.jsonl",
        "counts": {"constraint": 1, "finding": 1},
    }
    assert rows[4] == {
        "transcript": "/tmp/S2.jsonl",
        "counts": {"constraint": 0, "finding": 1},
    }
    assert rows[-1] == {"total": {"constraint": 1, "finding": 2, "errors": 0}}


@pytest.mark.parametrize("failure", ["parse", "empty", "classifier"])
def test_failure_continues_and_deltas_match_repeated_runs(
    monkeypatch, capsys, tmp_path, failure
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only")
    baseline = tmp_path / "base.jsonl"
    baseline.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"transcript": "repeat", "counts": {"constraint": 0, "finding": 7}},
                {"transcript": "bad", "counts": {"constraint": 0, "finding": 8}},
                {"transcript": "repeat", "counts": {"constraint": 1, "finding": 2}},
                {"total": {"constraint": 1, "finding": 17}},
            ]
        )
    )

    def parse(path, min_turns):
        if path == "bad" and failure == "parse":
            raise OSError("unreadable transcript")
        return SimpleNamespace(turns=[{"role": "user", "content": path}]), None

    def extract(text):
        contents = (
            []
            if text.endswith("bad") and failure == "empty"
            else ["Implemented helper for transcript " + text]
        )
        return {
            "reasoning_trace": SimpleNamespace(
                trace_id="trace",
                session_id="session",
                steps=[
                    SimpleNamespace(type="conclusion", content=content)
                    for content in contents
                ],
            )
        }

    def classify(texts, receipt):
        if texts[0].endswith("bad") and failure == "classifier":
            raise ValueError("invalid classifier response")
        return ["finding"]

    monkeypatch.setattr(probe, "parse_transcript", parse)
    monkeypatch.setattr(
        probe, "SessionLessonExtractor", lambda config: SimpleNamespace(extract=extract)
    )
    monkeypatch.setattr(probe, "classify_lessons", classify)
    probe.main(["--baseline-json", str(baseline), "repeat", "bad", "repeat", "missing"])
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.out.splitlines()]
    assert [row["transcript"] for row in rows if "error" in row] == ["bad"]
    assert rows[-1] == {"total": {"constraint": 0, "finding": 3, "errors": 1}}
    assert "repeat\t1\t0/7\t0/1\t+0/-6" in output.err
    assert "bad\t1\t0/8\tERROR\tN/A" in output.err
    assert "repeat\t2\t1/2\t0/1\t-1/-1" in output.err
    assert "missing\t1\tN/A\t0/1\tN/A" in output.err


def test_baseline_error_row_preserves_occurrence(tmp_path):
    baseline = tmp_path / "base.jsonl"
    baseline.write_text(
        '{"transcript": "a", "error": "failed"}\n'
        '{"transcript": "a", "counts": {"constraint": 1, "finding": 0}}\n'
    )
    assert probe.load_baseline(baseline)["a"] == [None, {"constraint": 1, "finding": 0}]
