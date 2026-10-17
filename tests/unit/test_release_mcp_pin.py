"""Release error harness for published MCP pins and the resulting wheel metadata."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from scripts.sync_mcp_pin import sync_mcp_pin

ROOT = Path(__file__).parents[2]


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    repo = tmp_path / "test_mcp_release"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "smartmemory"\nversion = "1.5.18"\n'
        'dependencies = ["smartmemory-core[lite,wikipedia]==1.5.18",\n'
        '    "smartmemory-mcp==1.4.100", # preserve this comment\n]\n',
        encoding="utf-8",
    )
    data = {
        "info": {"version": "1.5.11"},
        "releases": {"1.4.100": [{}], "1.5.11": [{}], "1.6.0rc1": [{}]},
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: io.BytesIO(json.dumps(data).encode())
    )
    return repo, data


def test_bumps_exact_pin_idempotently_without_dropping_comments(release_repo, capsys):
    repo, _ = release_repo
    sync_mcp_pin(repo)
    first = (repo / "pyproject.toml").read_bytes()
    modified = (repo / "pyproject.toml").stat().st_mtime_ns
    sync_mcp_pin(repo)
    assert (repo / "pyproject.toml").read_bytes() == first
    assert (repo / "pyproject.toml").stat().st_mtime_ns == modified
    assert b'"smartmemory-mcp==1.5.11", # preserve this comment' in first
    assert "bumped exact pin 1.4.100 -> 1.5.11" in capsys.readouterr().out


def test_unreachable_pypi_leaves_pin_unchecked(release_repo, monkeypatch, capsys):
    repo, _ = release_repo
    first = (repo / "pyproject.toml").read_bytes()

    def unreachable(*a, **kw):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)
    sync_mcp_pin(repo)
    assert (repo / "pyproject.toml").read_bytes() == first
    assert "UNCHECKED" in capsys.readouterr().out


@pytest.mark.parametrize("unpublished", [None, []])
def test_unpublished_pin_remains_fatal(release_repo, unpublished):
    repo, data = release_repo
    data["releases"]["1.4.100"] = unpublished
    first = (repo / "pyproject.toml").read_bytes()
    with pytest.raises(SystemExit, match="FATAL: pinned smartmemory-mcp==1.4.100"):
        sync_mcp_pin(repo)
    assert (repo / "pyproject.toml").read_bytes() == first


@pytest.mark.parametrize("tracked", [True, False])
def test_only_tracked_lock_is_refreshed(release_repo, monkeypatch, tracked):
    repo, _ = release_repo
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "uv.lock").write_text("old lock\n")
    if tracked:
        subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    # Instrument the external lock command while exercising real tracked-file
    # detection and cwd. Dependency resolution belongs to uv's own test suite.
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\n[ "$1" = lock ] || exit 9\nprintf "refreshed lock\\n" > uv.lock\n'
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    sync_mcp_pin(repo)
    assert (repo / "uv.lock").read_text() == (
        "refreshed lock\n" if tracked else "old lock\n"
    )


def _wheel(repo, pin):
    dist = repo / "dist"
    dist.mkdir(exist_ok=True)
    wheel = dist / "smartmemory-1.5.18-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "smartmemory-1.5.18.dist-info/METADATA",
            f"Requires-Dist: smartmemory-core[lite,wikipedia]==1.5.18\nRequires-Dist: {pin}\n",
        )
    return wheel


@pytest.mark.parametrize("check_only", [False, True])
def test_release_checks_the_bumped_pin_in_actual_wheel(release_repo, check_only):
    repo, data = release_repo
    scripts = repo / "scripts"
    scripts.mkdir()
    for name in ("release.sh", "sync_mcp_pin.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    # All Python subprocesses use an offline PyPI fixture. The real release
    # script, TOML rewrite, wheel metadata inspection and failure gate execute.
    fixture_dir = repo / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "pypi.json").write_text(json.dumps(data))
    (fixture_dir / "sitecustomize.py").write_text(
        "import io, urllib.request\nfrom pathlib import Path\n"
        "urllib.request.urlopen = lambda *a, **kw: io.BytesIO("
        "(Path(__file__).parent / 'pypi.json').read_bytes())\n"
    )
    bin_dir = repo / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    python3.symlink_to(sys.executable)
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport tomllib, zipfile\nfrom pathlib import Path\n"
        "assert __import__('sys').argv[1:] == ['build', '--wheel', '--out-dir', 'dist']\n"
        "data=tomllib.loads(Path('pyproject.toml').read_text())\n"
        "Path('dist').mkdir()\n"
        "with zipfile.ZipFile('dist/smartmemory-1.5.18-py3-none-any.whl','w') as z:\n"
        "    z.writestr('smartmemory-1.5.18.dist-info/METADATA', ''.join("
        "'Requires-Dist: '+d+'\\n' for d in data['project']['dependencies']))\n"
    )
    uv.chmod(0o755)
    if check_only:
        _wheel(repo, "smartmemory-mcp==1.4.100")
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(fixture_dir),
    }
    result = subprocess.run(
        ["bash", "scripts/release.sh", "--check-only" if check_only else "--dry-run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if check_only else 0), result.stdout + result.stderr
    deps = tomllib.loads((repo / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert "smartmemory-mcp==1.5.11" in deps
    if check_only:
        assert "does not contain the current exact MCP pin" in result.stderr
    else:
        wheel = next((repo / "dist").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            assert b"Requires-Dist: smartmemory-mcp==1.5.11" in archive.read(
                "smartmemory-1.5.18.dist-info/METADATA"
            )
