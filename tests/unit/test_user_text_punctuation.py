"""CLI-POSTLAND-FU-1 H3: no em dash and no semicolon in text a user can read.

Two nets. Every registered command's ``--help`` is rendered and read. Every string
literal in ``smartmemory_app/`` is scanned (messages, warnings, log lines that reach
stderr, HTTP ``detail``, Click help and command docstrings), except what is not text
for a person: non-command docstrings, ``logger.debug``/``info`` calls, and the rows
of ``NOT_USER_TEXT`` below, each with its reason.
"""

import ast
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import smartmemory_app

APP = Path(smartmemory_app.__file__).parent
FORBIDDEN = ("—", ";")

# (file, literal starts with) -> reason. A row that matches nothing fails the test.
NOT_USER_TEXT = {
    ("diagnostics.py", "(?i)"): "regex",
    ("report_privacy.py", "(?i)"): "regex",
    ("viewer_server.py", "(?<!"): "regex",
    ("session_lessons.py", "^(User-stated"): "regex",
    ("setup_tui.py", "\n    #welcome-box"): "Textual CSS",
    ("tour.py", "\n    #tour-box"): "Textual CSS",
    ("tui/app.py", "\n    Screen {"): "Textual CSS",
    ("cli.py", "from importlib.metadata import"): "python -c source",
    ("daemon.py", "from smartmemory_app.viewer_server import"): "python -c source",
    ("tour.py", "from smartmemory_app.viewer_server import"): "python -c source",
    ("lesson_lifecycle.py", "Classify lesson pairs"): "prompt sent to a model",
    ("lesson_lifecycle.py", "Extract explicit rule changes"): "prompt sent to a model",
    ("lesson_lifecycle.py", "Match candidate old rules"): "prompt sent to a model",
    (
        "session_lessons.py",
        "Extract durable engineering findings",
    ): "prompt sent to a model",
}


def _is_command(node: ast.AST) -> bool:
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in {"command", "group"}:
            return True
    return False


def _skipped(tree: ast.AST) -> set[int]:
    skip = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        is_scope = isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        if is_scope and body and not _is_command(node):
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"debug", "info"}
        ):
            skip.update(id(child) for child in ast.walk(node))
    return skip


def _offending_literals() -> list[tuple[str, int, str]]:
    """(file, line, text) for every literal with a forbidden character."""
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        skip = _skipped(tree)
        rel = path.relative_to(APP).as_posix()
        for node in ast.walk(tree):
            is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
            if (
                is_text
                and id(node) not in skip
                and any(c in node.value for c in FORBIDDEN)
            ):
                found.append((rel, node.lineno, node.value))
    return found


def test_no_string_literal_in_the_app_has_an_em_dash_or_semicolon():
    offenders = [
        f"{rel}:{line}: {text[:60]!r}"
        for rel, line, text in _offending_literals()
        if not any(rel == f and text.startswith(p) for f, p in NOT_USER_TEXT)
    ]
    assert not offenders, offenders


def test_every_not_user_text_row_still_matches_a_literal():
    literals = _offending_literals()
    stale = [
        key
        for key in NOT_USER_TEXT
        if not any(
            key[0] == rel and text.startswith(key[1]) for rel, _, text in literals
        )
    ]
    assert not stale, stale


def _paths(group: click.Group, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out = []
    for name, cmd in sorted(group.commands.items()):
        out.append((*prefix, name))
        if isinstance(cmd, click.Group):
            out.extend(_paths(cmd, (*prefix, name)))
    return out


def test_every_registered_command_help_has_no_em_dash_or_semicolon():
    from smartmemory_app.cli import cli

    runner = CliRunner()
    paths = [(), *_paths(cli)]
    assert len(paths) > 30  # the walk really reached the command tree
    bad = []
    for path in paths:
        result = runner.invoke(cli, [*path, "--help"])
        assert result.exit_code == 0, (path, result.output)
        if any(c in result.output for c in FORBIDDEN):
            bad.append(" ".join(path) or "<root>")
    assert not bad, bad


@pytest.mark.parametrize("text", ["a — b", "a; b"])
def test_the_scan_catches_a_new_offender(text, tmp_path, monkeypatch):
    """Control: the literal scan flags a fresh one in a new file under the app."""
    (tmp_path / "fresh.py").write_text(f"MESSAGE = {text!r}\n")
    monkeypatch.setitem(globals(), "APP", tmp_path)
    assert [t for _, _, t in _offending_literals()] == [text]
