"""The retag dry-run option appears in the rendered CLI help."""

from click.testing import CliRunner

from smartmemory_app.cli import retag_cmd


def test_help_lists_dry_run_and_not_its_negation():
    result = CliRunner().invoke(retag_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--no-dry-run" not in result.output
