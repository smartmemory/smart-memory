"""Offline G10 gate for output of check_session_lesson_extraction.py.

This validates evidence, never calls an LLM or opens a memory store. Generate the
fixed transcripts, run the existing read-only probe in the acceptance lane, then
validate its JSONL here. Passing unit-test fixtures are not model-quality evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SAMPLE = Path(__file__).resolve().parents[1] / "tests/fixtures/hook_rule_quality.json"


def load_sample():
    return json.loads(SAMPLE.read_text())


def emit_transcripts(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    samples = load_sample()
    paths = [directory / (s["id"] + ".jsonl") for s in samples]
    # Preflight before creating anything, including on an accidental repeated run.
    if any(p.exists() for p in paths):
        raise FileExistsError("fixed sample transcripts already exist")
    created = []
    try:
        for sample, path in zip(samples, paths):
            messages = []
            for i, (role, text) in enumerate(
                (
                    ("user", sample["rule"]),
                    ("assistant", "Understood. I will follow this standing rule."),
                )
            ):
                messages.append(
                    {
                        "type": role,
                        "sessionId": sample["id"],
                        "uuid": f"turn-{i}",
                        "parentUuid": f"turn-{i - 1}" if i else None,
                        "isSidechain": False,
                        "timestamp": f"2026-11-06T00:00:0{i}Z",
                        "cwd": "/isolated/sm-hooks-rule-quality",
                        "message": {
                            "role": role,
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                )
            with path.open("x") as output:
                created.append(path)
                output.write("\n".join(json.dumps(m) for m in messages) + "\n")
    except Exception:
        for path in created:
            path.unlink()
        raise


def evaluate(rows):
    samples = {s["id"]: s for s in load_sample()}
    conclusions = {key: [] for key in samples}
    failures = []
    for row in rows:
        if not isinstance(row, dict):
            failures.append("malformed output row")
            continue
        if "total" in row and "transcript" not in row:
            continue
        key = Path(str(row.get("transcript", ""))).stem
        if key not in samples:
            failures.append(f"unknown sample {key}")
            continue
        if "error" in row:
            failures.append(f"{key}: extraction error, rule lost")
        elif "counts" not in row:
            conclusions[key].append(row)
    for key, sample in samples.items():
        found = conclusions[key]
        if len(found) != 1:
            failures.append(
                f"{key}: expected one conclusion, got {len(found)}, rule missing or ambiguous"
            )
            continue
        row = found[0]
        content = row.get("content")
        if not isinstance(content, str):
            failures.append(f"{key}: missing rule text")
            continue
        expected = "User-stated rule: " + sample["rule"]
        if row.get("kind") != sample["kind"] or " ".join(content.split()) != " ".join(
            expected.split()
        ):
            failures.append(
                f"{key}: rule wording, category, negation, scope or kind lost"
            )
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-transcripts", type=Path)
    mode.add_argument("--results", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.emit_transcripts is not None:
            emit_transcripts(args.emit_transcripts)
            print(f"Emitted 20 fixed transcripts in {args.emit_transcripts}")
            return 0
        rows = [
            json.loads(line)
            for line in args.results.read_text().splitlines()
            if line.strip()
        ]
        failures = evaluate(rows)
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"WARNING: distill quality unverified, unusable evidence: {exc}",
            file=sys.stderr,
        )
        return 1
    digest = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    for failure in failures:
        print(f"WARNING: distill quality: {failure}", file=sys.stderr)
    # Fail closed on unknown/error/duplicate output as well as missing rules.
    passed = sum(
        not any(f.startswith(s["id"] + ":") for f in failures) for s in load_sample()
    )
    status = "HOLD" if failures else "PASS"
    print(
        f"SM-HOOKS-2-0-1 rule quality {passed}/20 gate={status} sample_sha256={digest}"
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
