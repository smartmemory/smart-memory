"""Strict session-lesson output validation and bounded source-backed repair."""

import json
import logging
import re

from jsonschema import validate, ValidationError

log = logging.getLogger(__name__)
SCHEMA = {
    "type": "array",
    "maxItems": 8,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "content"],
        "properties": {
            "type": {
                "enum": [
                    "thought",
                    "action",
                    "observation",
                    "decision",
                    "conclusion",
                    "reflection",
                ]
            },
            "content": {"type": "string", "minLength": 1},
        },
    },
}
_TURN = re.compile(r"^Turn \d+ \((user|assistant)\):\n", re.MULTILINE)
_STANDING = re.compile(
    r"\b(?:in this project|for this project|from now on|for (?:all )?future)\b", re.I
)
_TASK = re.compile(
    r"^(?:(?:in|for) this project,\s*)?(?:add|implement|build|fix|create|change|update)\b",
    re.I,
)
_ACK = re.compile(
    r"(?:Understood\.|Got it\.|OK\.|I will follow this standing rule\.)\s*", re.I
)
_PREFIX = "CONSTRAINT: User-stated rule: "
_ATTRIBUTION = re.compile(
    r"^(?:CONSTRAINT:\s*)?(?:User-stated rule|Owner policy)\s*(?::|[-–—])\s*(.*)$",
    re.S | re.I,
)


def _tagged_rule(row):
    return row["content"].startswith("CONSTRAINT:") and bool(
        _ATTRIBUTION.match(row["content"])
    )


def _turns(text):
    matches = list(_TURN.finditer(text))
    return [
        (
            m[1],
            text[
                m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ].strip(),
        )
        for i, m in enumerate(matches)
    ]


def isolated_rule(text):
    """Return a single complete user policy only when all replies are acknowledgments."""
    turns = _turns(text)
    users = [content for role, content in turns if role == "user"]
    assistants = [content for role, content in turns if role == "assistant"]
    if (
        len(users) == 1
        and "\n" not in users[0]
        and "?" not in users[0]
        and len(re.split(r"(?<=[.!?])\s+(?=[A-Z])", users[0])) == 1
        and _STANDING.search(users[0])
        and not _TASK.match(users[0])
        and assistants
        and all(not _ACK.sub("", content).strip() for content in assistants)
    ):
        return users[0]
    return None


def postprocess(rows, text):
    """Validate first, then one repair/dedupe pass. Never infer semantic similarity."""
    validate(rows, {key: value for key, value in SCHEMA.items() if key != "maxItems"})
    if any(not row["content"].strip() for row in rows):
        raise ValueError("empty lesson content")
    users = [content for role, content in _turns(text) if role == "user"]
    source = isolated_rule(text)
    if source is not None:
        canonical = [{"type": "conclusion", "content": _PREFIX + source}]
        if rows != canonical:
            log.warning(
                "Session lesson deterministic repair: replaced paraphrase/acknowledgment rows with source rule"
            )
        return canonical
    result = []
    seen = set()
    for row in rows:
        content = row["content"]
        attributed = _ATTRIBUTION.match(content)
        if attributed and not any(attributed[1] in user for user in users):
            raise ValueError("attributed rule is not verbatim user source")
        if attributed and content.startswith("CONSTRAINT:"):
            row = {"type": "conclusion", "content": content}
        if row["content"] in seen:
            log.warning(
                "Session lesson deterministic repair: removed duplicate content"
            )
            continue
        seen.add(row["content"])
        result.append(row)
    if len(result) > SCHEMA["maxItems"]:
        rules = [row for row in result if _tagged_rule(row)]
        if len(rules) > SCHEMA["maxItems"]:
            raise ValueError("too many explicit rules to preserve without loss")
        findings = [row for row in result if not _tagged_rule(row)]
        keep = {id(row) for row in rules + findings[: SCHEMA["maxItems"] - len(rules)]}
        log.warning(
            "Session lesson deterministic repair: eight-lesson limit discarded redundant/excess findings"
        )
        result = [row for row in result if id(row) in keep]
    validate(result, SCHEMA)
    return result


def parse_output(response, text):
    """One parse and post-step, otherwise refuse. No model retry or JSON guessing."""
    try:
        return postprocess(json.loads(response), text)
    except (ValueError, TypeError, ValidationError):
        log.warning(
            "Session lesson output refused: invalid JSON/schema or unsupported attribution, lessons lost"
        )
        return None
