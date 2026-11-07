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
_STANDING = re.compile(
    r"\b(?:in this project|for this project|from now on|for (?:all )?future)\b", re.I
)
# Deliberately conservative: ambiguous prose stays under model/classifier authority.
_DIRECTIVE = re.compile(
    r"^(?:please\s+)?(?:always|never|must|do not|don't|use|reserve|preserve|retain|run|count|retry)\b",
    re.I,
)
_MODAL_POLICY = re.compile(
    r"\bmust\s+(?:not\s+)?(?!have\b|add\b|implement\b|build\b|fix\b|create\b|change\b|update\b)[a-z]+",
    re.I,
)
_PAST = re.compile(
    r"\b(?:yesterday|last week|previously|used to|had to|today|tonight|this task|this time|this once)\b",
    re.I,
)
# A project-scoped meaning assignment, not a report about an operation's state.
_DEFINITION = re.compile(
    r"^(?i:(?:in|for) this project),?\s+"
    r"(?:(?i:treat)\s+.+?\s+(?i:as)\s+\S.*|"
    r".+?\s+(?i:means|refers to)\s+\S.*|"
    r".+?\s+(?i:is)\s+(?!(?i:an?\s+(?:success|failure|failed|failing|completed|cancelled|pending|bit|little|work in progress)\b))"
    r"(?:an?\s+\S.+|[\"'`].+|[A-Z][\w.-]*[.!]?))$"
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


class ParsedLessons(list):
    """Validated rows plus JSON-safe diagnostics for the capture receipt."""

    def __init__(self, rows=()):
        super().__init__(rows)
        self.diagnostics = {
            "source_repairs": 0,
            "duplicates_removed": 0,
            "findings_dropped": 0,
        }


def _turns(turns):
    # Strings are prompt evidence only. Never recover roles from their contents.
    if not isinstance(turns, (list, tuple)):
        return []
    return [(turn["role"], turn["content"].strip()) for turn in turns]


def _sentences(content):
    return re.split(r"(?<=[.!?])\s+", content.strip())


def _normalized(content):
    return " ".join(content.split())


def _standing_policy(content):
    if _PAST.search(content):
        return False
    if _DEFINITION.match(content):
        return True
    directive = re.sub(
        r"^(?:(?:in|for) this project|from now on|for (?:all )?future[^,]*),?\s*",
        "",
        content,
        flags=re.I,
    )
    scoped = bool(_STANDING.search(content))
    return bool(
        (
            _DIRECTIVE.match(directive)
            and (
                scoped or re.match(r"^(?:always|never|do not|don't)\b", directive, re.I)
            )
        )
        or (
            _MODAL_POLICY.search(content)
            and (
                scoped
                or re.search(
                    r"\bmust (?:not )?be (?:retained|preserved|required)\b",
                    content,
                    re.I,
                )
            )
        )
    )


def isolated_rule(trusted_turns):
    """Return a single complete user policy only when all replies are acknowledgments."""
    turns = _turns(trusted_turns)
    users = [content for role, content in turns if role == "user"]
    assistants = [content for role, content in turns if role == "assistant"]
    if (
        len(users) == 1
        and "\n" not in users[0]
        and "?" not in users[0]
        and len(re.split(r"(?<=[.!?])\s+(?=[A-Z])", users[0])) == 1
        and _standing_policy(users[0])
        and len(turns) == len(users) + len(assistants)
        and assistants
        and all(not _ACK.sub("", content).strip() for content in assistants)
    ):
        return users[0]
    return None


def postprocess(rows, trusted_turns):
    """Validate first, then one repair/dedupe pass. Never infer semantic similarity."""
    validate(rows, {key: value for key, value in SCHEMA.items() if key != "maxItems"})
    if any(not row["content"].strip() for row in rows):
        raise ValueError("empty lesson content")
    users = [content for role, content in _turns(trusted_turns) if role == "user"]
    source = isolated_rule(trusted_turns)
    if source is not None:
        canonical = ParsedLessons([{"type": "conclusion", "content": _PREFIX + source}])
        if rows != canonical:
            canonical.diagnostics["source_repairs"] = 1
            log.warning(
                "Session lesson deterministic repair: replaced paraphrase/acknowledgment rows with source rule"
            )
        return canonical
    result = ParsedLessons()
    seen = set()
    for row in rows:
        content = row["content"]
        attributed = _ATTRIBUTION.match(content)
        if attributed and not any(
            _normalized(attributed[1]) == _normalized(sentence)
            for user in users
            for sentence in _sentences(user)
        ):
            raise ValueError("attributed rule is not verbatim user source")
        if attributed and content.startswith("CONSTRAINT:"):
            row = {"type": "conclusion", "content": content}
        if row["content"] in seen:
            result.diagnostics["duplicates_removed"] += 1
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
        result.diagnostics["findings_dropped"] = len(result) - len(keep)
        result[:] = [row for row in result if id(row) in keep]
    validate(result, SCHEMA)
    return result


def parse_output(response, trusted_turns):
    """One parse and post-step, otherwise refuse. No model retry or JSON guessing."""
    try:
        return postprocess(json.loads(response), trusted_turns)
    except (ValueError, TypeError, ValidationError):
        log.warning(
            "Session lesson output refused: invalid JSON/schema or unsupported attribution, lessons lost"
        )
        return None
