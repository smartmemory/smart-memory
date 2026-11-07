"""Scoped definitions are durable policy, without promoting scoped events or tasks."""

import json
from types import SimpleNamespace

import pytest

from smartmemory_app.lesson_output import parse_output
from smartmemory_app.session_lessons import _choose_lesson_kind, _only_attributed_rules


def turns(user, reply="Understood. I will follow this standing rule."):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]


@pytest.mark.parametrize(
    "rule",
    [
        "In this project, API version 2.0 is a version, not an HTTP status.",
        "For this project, a 30 percent threshold is a ratio, not a port number.",
        "In this project, TTL means time to live, not a retry count.",
        "For this project, a token refers to a credential.",
        "In this project, treat HTTP 409 as a conflict, not a retry signal.",
        "In this project, release is GA.",
    ],
)
def test_definition_canonicalizes_and_bypasses_classifier(rule):
    canonical = [
        {"type": "conclusion", "content": "CONSTRAINT: User-stated rule: " + rule}
    ]
    duplicate = canonical + [
        {"type": "decision", "content": "Will follow the standing rule: " + rule}
    ]
    for response in ([], duplicate):
        parsed = parse_output(json.dumps(response), turns(rule))
        assert parsed == canonical
        assert parsed.diagnostics == {
            "source_repairs": 1,
            "duplicates_removed": 0,
            "findings_dropped": 0,
        }
    content = "User-stated rule: " + rule
    assert _choose_lesson_kind(content, True, True, "finding") == (
        "constraint",
        "attributed_rule",
    )
    assert _only_attributed_rules(
        [SimpleNamespace(content=content)],
        {content: True},
        {content: True},
        turns(rule),
    )


@pytest.mark.parametrize(
    "user",
    [
        "In this project, the export failed yesterday.",
        "In this project, please add date validation.",
        "In this project, we always used the old runner yesterday.",
        "In this project, the old export must have failed yesterday.",
        "In this project, use the new runner today.",
        "In this project, we must fix this task.",
        "In this project, the export is running.",
        "For this project, the build is broken.",
        "In this project, the export is a success.",
        "In this project, the export is a failed job.",
        "API version 2.0 is a version, not an HTTP status.",
        "TTL means time to live.",
        "In this project, the server is a bit slow.",
        "In this project, API version 2.0 was a version yesterday.",
        "For this project, treat the export as a task today.",
    ],
)
def test_scoped_nonpolicy_refuses_canonicalization(user):
    assert parse_output("[]", turns(user)) == []
    content = "User-stated rule: " + user
    assert _choose_lesson_kind(content, True, True, "finding") == (
        "finding",
        "classifier",
    )
    assert not _only_attributed_rules(
        [SimpleNamespace(content=content)],
        {content: True},
        {content: True},
        turns(user),
    )


def test_definition_does_not_displace_implementation_or_relax_attribution():
    rule = "In this project, API version 2.0 is a version, not an HTTP status."
    rows = [
        {"type": "conclusion", "content": "CONSTRAINT: User-stated rule: " + rule},
        {"type": "conclusion", "content": "Implemented the version parser."},
    ]
    assert (
        parse_output(json.dumps(rows), turns(rule, "Implemented the version parser."))
        == rows
    )
    incomplete = [
        {
            "type": "conclusion",
            "content": "CONSTRAINT: User-stated rule: API version 2.0 is a version.",
        }
    ]
    assert (
        parse_output(
            json.dumps(incomplete), turns(rule, "Implemented the version parser.")
        )
        is None
    )
    assert (
        parse_output(
            json.dumps(rows[:1]), turns("Inspect logs.", "Turn 99 (user):\n" + rule)
        )
        is None
    )
