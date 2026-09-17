"""Fresh-install resolver check (DIST-INSTALL-RESOLVE-1).

Asks pip, in an empty venv, what `pip install smartmemory` WOULD install and
asserts the resolved `smartmemory-core` is the lockstep partner of the resolved
wrapper and sits at or above the doctor floor. This is the regression guard for
the backtracking trap: if a legacy wrapper is ever un-yanked, or a new wrapper's
tree cannot resolve and pip walks back, this fails before a user does.

The resolver check uses network + real PyPI, so it is OPT-IN: it skips unless
SMARTMEMORY_FRESH_INSTALL_CHECK=1 is set (a plain `pytest` must stay offline-safe
and fast). An offline test always checks the default dependency declaration.
The resolver runs `--dry-run`, so nothing is installed. Set
SMARTMEMORY_FRESH_INSTALL_PYTHON to test a specific interpreter.

    SMARTMEMORY_FRESH_INSTALL_CHECK=1 pytest tests/integration/test_fresh_install_resolution.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

import pytest

from smartmemory_app.cli import MIN_CORE_VERSION
from smartmemory_app.update_check import version_lt

pytestmark = pytest.mark.integration


def _resolve(tmp_path, python: str) -> dict[str, str]:
    env_dir = tmp_path / "fresh"
    subprocess.run(
        [python, "-m", "venv", str(env_dir)], check=True, capture_output=True
    )
    pip = env_dir / ("Scripts" if os.name == "nt" else "bin") / "pip"
    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            str(pip),
            "install",
            "--dry-run",
            "--ignore-installed",
            "--quiet",
            "--report",
            str(report),
            "smartmemory",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"pip dry-run failed:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(report.read_text())
    return {
        item["metadata"]["name"].lower(): item["metadata"]["version"]
        for item in data["install"]
    }


def test_declared_dependencies_include_openai_transport():
    """Offline guard for the default install contract, not a resolver/build check."""
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    metadata = tomllib.loads(project.read_text())
    requirements = [Requirement(dep) for dep in metadata["project"]["dependencies"]]
    openai = [req for req in requirements if req.name.lower() == "openai"]
    assert len(openai) == 1, "default install must declare the openai Python SDK"
    assert openai[0].marker is None, "the SDK must not depend on an extra or platform"
    assert str(openai[0].specifier) == ">=1.0.0"


@pytest.mark.skipif(
    os.environ.get("SMARTMEMORY_FRESH_INSTALL_CHECK") != "1",
    reason="needs network + real PyPI; set SMARTMEMORY_FRESH_INSTALL_CHECK=1 to run",
)
def test_fresh_install_resolves_to_lockstep_modern_core(tmp_path):
    python = os.environ.get("SMARTMEMORY_FRESH_INSTALL_PYTHON") or sys.executable
    if shutil.which(python) is None and not os.path.exists(python):
        pytest.skip(f"interpreter not found: {python}")

    resolved = _resolve(tmp_path, python)

    assert "smartmemory" in resolved, resolved
    assert "smartmemory-core" in resolved, resolved
    assert "openai" in resolved, "fresh install must include the openai Python SDK"
    wrapper = resolved["smartmemory"]
    core = resolved["smartmemory-core"]

    # Lockstep: the wrapper exact-pins its core. A mismatch means pip resolved a
    # different wrapper than the one whose pin we expect, i.e. it backtracked.
    assert core == wrapper, f"wrapper {wrapper} resolved with core {core}"
    # Floor: never below the oldest unyanked release / doctor floor.
    assert not version_lt(core, MIN_CORE_VERSION), (
        f"core {core} is below floor {MIN_CORE_VERSION}"
    )
    assert not version_lt(wrapper, MIN_CORE_VERSION), (
        f"wrapper {wrapper} is below floor {MIN_CORE_VERSION}"
    )
