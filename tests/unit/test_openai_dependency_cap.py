"""openai 3.x builds on httpx2, which core's destination_pin refuses: the openai requirement stays below 3."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def test_every_openai_requirement_is_capped_below_3():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    specs = list(data["project"].get("dependencies", []))
    for extra in data["project"].get("optional-dependencies", {}).values():
        specs.extend(extra)
    reqs = [Requirement(s) for s in specs if Requirement(s).name.lower() == "openai"]
    assert reqs, "pyproject.toml declares no openai requirement"
    for req in reqs:
        assert not req.specifier.contains("3.0.0"), f"{req} admits openai 3.x"
        assert not req.specifier.contains("3.46.0"), f"{req} admits openai 3.x"
