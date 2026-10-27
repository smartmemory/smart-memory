"""`sm recall` says so when nothing matches (CLI-RECALL-EMPTY-STATE-1).

The tester's report: `sm recall --query "..."` with zero matches printed only the
scope line. The empty-state line goes to stderr: recall runs as a hook whose
stdout is fed to a model prompt, so stdout must stay exactly what the producer
returned (empty when nothing matched).
"""

from __future__ import annotations

import inspect

import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module
from smartmemory_app import storage

EMPTY_LINE = 'No results. Try different words, or `sm search "<query>"`.'

# id | daemon reply (None = daemon unreachable) | in-process recall text | stdout | stderr has line
ROWS = [
    ("daemon-empty-context", {"context": ""}, None, "", True),
    (
        "daemon-hit",
        {"context": "## SmartMemory Context\n- a memory"},
        None,
        "## SmartMemory Context\n- a memory\n",
        False,
    ),
    ("fallback-empty-context", None, "", "", True),
    (
        "fallback-hit",
        None,
        "## SmartMemory Context\n- a memory",
        "## SmartMemory Context\n- a memory\n",
        False,
    ),
]


def _runner() -> CliRunner:
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


@pytest.mark.parametrize(
    "daemon,direct,stdout,empty_line",
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_recall_empty_state(monkeypatch, daemon, direct, stdout, empty_line):
    monkeypatch.setattr(cli_module, "_memory_request", lambda *a, **k: daemon)
    monkeypatch.setattr(cli_module, "_prepare_direct_access", lambda *a, **k: None)
    monkeypatch.setattr(storage, "recall", lambda *a, **k: direct)

    result = _runner().invoke(
        cli_module.cli,
        ["--quiet", "recall", "--query", "anything"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert getattr(result, "stdout", result.output) == stdout
    stderr = result.stderr if hasattr(result, "stderr") else result.output
    assert (EMPTY_LINE in stderr) is empty_line
