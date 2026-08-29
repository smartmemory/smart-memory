"""The retag dry-run hint must name an option that actually exists.

Regression: the hint said "pass --no-dry-run", but `retag` declares a plain
`--dry-run` boolean flag, so the suggested command failed with
`Error: No such option: --no-dry-run`. The dry run printed its plan and the
follow-up it told you to run did nothing — a silent no-op that looked like a
completed retag.
"""

from __future__ import annotations

import inspect
import re

from click.testing import CliRunner

from smartmemory_app.cli import retag_cmd


def _declared_options() -> set[str]:
    names: set[str] = {"--help"}
    for param in retag_cmd.params:
        names.update(getattr(param, "opts", []))
        names.update(getattr(param, "secondary_opts", []))
    return names


def test_messages_only_reference_declared_options():
    body = inspect.getsource(retag_cmd.callback)
    hinted = set(re.findall(r"--[a-z][a-z0-9-]+", body))
    unknown = hinted - _declared_options()
    assert not unknown, f"retag messages reference undeclared options: {unknown}"


def test_help_lists_dry_run_and_not_its_negation():
    result = CliRunner().invoke(retag_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--no-dry-run" not in result.output
