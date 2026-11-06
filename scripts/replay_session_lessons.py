"""Replay historical processed lesson rows, not raw provider responses, offline."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from smartmemory.importers.agent_transcript import parse_transcript
from smartmemory_app.lesson_output import parse_output
from smartmemory_app.session_lessons import _normalize_lesson_content


def replay(results, transcripts):
    grouped = defaultdict(list)
    for row in results:
        if "transcript" in row:
            grouped[Path(row["transcript"]).stem].append(row)
    output = []
    for key, rows in grouped.items():
        path = Path(transcripts) / (key + ".jsonl")
        conversation, _ = parse_transcript(str(path), min_turns=1)
        text = "\n\n".join(
            f"Turn {i} ({turn['role']}):\n{turn['content']}"
            for i, turn in enumerate(conversation.turns)
        )
        # The malformed raw JSON was not retained. An error marker must refuse,
        # never manufacture provider output or silently recover missing evidence.
        payload = (
            None
            if any("error" in row for row in rows)
            else [
                {
                    "type": row["type"],
                    "content": ("CONSTRAINT: " if row.get("tagged") else "")
                    + row["content"],
                }
                for row in rows
                if "content" in row
            ]
        )
        parsed = parse_output(json.dumps(payload), text)
        if parsed is None:
            output.append(
                {"transcript": str(path), "error": "historical output refused"}
            )
            continue
        for row in parsed:
            content, tagged, attributed = _normalize_lesson_content(row["content"])
            output.append(
                {
                    "transcript": str(path),
                    "type": row["type"],
                    "content": content,
                    "kind": "constraint" if tagged and attributed else "finding",
                }
            )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("transcripts", type=Path)
    args = parser.parse_args()
    for row in replay(
        [json.loads(line) for line in args.results.read_text().splitlines()],
        args.transcripts,
    ):
        print(json.dumps(row))
