"""DOCS-CLI-ZSH-DOLLAR-QUOTES-1: the README warns that `$48` vanishes inside double quotes."""

from pathlib import Path

README = (Path(__file__).resolve().parents[2] / "README.md").read_text()

ROWS = [
    ("single-quoted form shown", "sm add 'The plan cost $48'"),
    ("double-quoted before shown", 'sm add "The plan cost $48"'),
    ("reason named", "expands `$48` inside double quotes"),
]


def test_readme_dollar_quote_tip_rows():
    missing = [name for name, needle in ROWS if needle not in README]
    assert not missing, missing
    # external prose rule: the tip line carries no em dash or semicolon
    tip = next(line for line in README.splitlines() if "expands `$48`" in line)
    assert "—" not in tip and ";" not in tip
