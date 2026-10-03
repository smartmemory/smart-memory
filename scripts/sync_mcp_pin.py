"""Synchronize the wrapper's exact MCP pin before building its release wheel."""

import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import urllib.request


def sync_mcp_pin(repo: Path) -> None:
    """Keep unpublished pins fatal and unreachable PyPI advisory."""
    project = repo / "pyproject.toml"
    source = project.read_text(encoding="utf-8")
    deps = tomllib.loads(source)["project"]["dependencies"]
    pin = next(
        (d for d in deps if d.replace(" ", "").startswith("smartmemory-mcp==")),
        None,
    )
    if pin is None:
        print(">> smartmemory-mcp: no exact pin found, skipping freshness check.")
        return
    pinned = pin.split("==", 1)[1].strip()

    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/smartmemory-mcp/json", timeout=15
        ) as response:
            data = json.load(response)
    except Exception as exc:
        print(f">> smartmemory-mcp: PyPI unreachable ({exc}), freshness UNCHECKED.")
        return

    released = data["releases"]
    if not released.get(pinned):
        sys.exit(
            f"FATAL: pinned smartmemory-mcp=={pinned} is not published on PyPI. "
            "The wheel would be uninstallable."
        )

    # PyPI's advertised latest version avoids selecting a newer prerelease
    # merely because it has more digits than the current stable release.
    latest = data["info"]["version"]
    if not released.get(latest):
        sys.exit(f"FATAL: latest smartmemory-mcp=={latest} has no published files.")

    def version_key(version: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", version))

    if version_key(latest) <= version_key(pinned):
        print(f">> smartmemory-mcp pin {pinned} is current.")
        return

    # Replace only the exact dependency string, retaining surrounding comments
    # and all other TOML. Never turn the dependency into a floating constraint.
    dependency = re.compile(
        r'(["\']smartmemory-mcp\s*==\s*)' + re.escape(pinned) + r'(["\'])'
    )
    updated, count = dependency.subn(lambda m: m[1] + latest + m[2], source)
    if count != 1:
        sys.exit("FATAL: expected one exact smartmemory-mcp pin to rewrite.")
    project.write_text(updated, encoding="utf-8")
    print(f">> smartmemory-mcp: bumped exact pin {pinned} -> {latest}.", flush=True)

    tracked_lock = (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", "uv.lock"],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if tracked_lock:
        print(">> refreshing tracked uv.lock for the MCP pin.", flush=True)
        subprocess.run(["uv", "lock"], cwd=repo, check=True)


if __name__ == "__main__":
    sync_mcp_pin(Path(__file__).resolve().parent.parent)
