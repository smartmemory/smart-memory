"""openai 3.x builds on httpx2, which core's destination_pin.py refuses (fail closed): cap openai below 3."""

from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[2]
_NAME = re.compile(r"^\s*openai\s*(?![\w.-])(?P<spec>[^;#]*)", re.IGNORECASE)


def _requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    found = [("dependencies", r) for r in project.get("dependencies", [])]
    for extra, reqs in project.get("optional-dependencies", {}).items():
        found += [(extra, r) for r in reqs]
    return [(src, m.group("spec").strip()) for src, r in found if (m := _NAME.match(r))]


def test_openai_requirement_exists():
    assert _requirements()


def test_every_openai_requirement_has_upper_bound_below_3():
    uncapped = [(s, spec) for s, spec in _requirements() if not re.search(r"<\s*3(\.0)*\s*(,|$)", spec)]
    assert not uncapped, f"openai needs an upper bound <3: {uncapped}"
