"""CLI-HOME-SHARED-ACROSS-ENVS-1: warn when the store was last used by another sm version.

The record is the install-check pass file ``first_run_check`` already keeps in the data
dir (the store's only per-store version record). The first command of a run reads its
``version`` before the check rewrites it, so a version switch warns once and then goes quiet.
"""

import json
from unittest.mock import patch

from click.testing import CliRunner

from smartmemory_app import __version__
from smartmemory_app import bug_report
from smartmemory_app import cli as cli_mod
from smartmemory_app import install_check

WARNING = "was last used by smartmemory"

# (name, recorded version or None for no record, commands run in order, warnings expected per command)
ROWS = [
    ("older writer warns once", "0.0.1", ["status", "status"], [True, False]),
    ("same version is silent", __version__, ["status"], [False]),
    ("no record is silent", None, ["status"], [False]),
    ("first command of a run warns", "0.0.1", ["search", "status"], [True, False]),
]


def _run(data_dir, command):
    args = ["search", "anything"] if command == "search" else [command]
    with (
        patch.object(
            install_check, "run_native_checks", return_value=["Native usearch: OK"]
        ),
        patch.object(cli_mod, "_memory_request", return_value=None),
        patch("smartmemory_app.storage.search", return_value=[]),
        patch.object(cli_mod, "_prepare_direct_access", return_value=None),
    ):
        return CliRunner().invoke(cli_mod.cli, ["--quiet", *args])


def test_store_writer_version_rows(monkeypatch, tmp_path):
    for name, recorded, commands, expected in ROWS:
        data_dir = tmp_path / name.replace(" ", "-")
        data_dir.mkdir()
        monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
        # conftest pins the debug log (the CLI's data-dir handle) to tmp_path; point it at this row.
        monkeypatch.setattr(
            bug_report, "debug_log_path", lambda d=data_dir: d / "cli-debug.log"
        )
        if recorded is not None:
            (data_dir / ".install-check-pass.json").write_text(
                json.dumps({"version": recorded, "python": [3, 11, 0], "probe": 1}),
                encoding="utf-8",
            )
        seen = []
        for command in commands:
            result = _run(data_dir, command)
            seen.append(WARNING in result.output)
        assert seen == expected, (name, seen, result.output)
        record = json.loads(
            (data_dir / ".install-check-pass.json").read_text(encoding="utf-8")
        )
        assert record["version"] == __version__, name
