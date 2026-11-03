"""CLI-TEST-ENV-LEAK-1: the provider-env guard fails the test that leaks."""

import os

import pytest

from tests.conftest import _provider_env_snapshot, assert_provider_env_unchanged


def _direct_assignment():
    os.environ["LLM_PROVIDER"] = "synthetic-leak"


def _monkeypatched():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LLM_PROVIDER", "synthetic-ok")
        mp.setenv("GROQ_API_KEY", "synthetic-ok")


def _called_function():
    # capture_worker._memory pins the provider by writing os.environ itself.
    from unittest.mock import Mock

    from smartmemory_app import capture_worker

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GROQ_API_KEY", "synthetic-ok")
        mp.delenv("SMARTMEMORY_CAPTURE_OFFLINE", raising=False)
        mp.setattr("smartmemory.tools.factory.create_lite_memory", Mock())
        capture_worker._memory()


def _write_then_delete():
    os.environ["DEEPSEEK_BASE_URL"] = "synthetic-base"
    del os.environ["DEEPSEEK_BASE_URL"]


def _deleted_existing():
    del os.environ["DEEPSEEK_API_KEY"]


# one row per leak route: direct write, monkeypatch (clean), production call,
# a write-then-restore that nets to zero, and removal of a var that was present.
ROWS = [
    ("os.environ assignment", _direct_assignment, ["LLM_PROVIDER"]),
    ("monkeypatch.setenv", _monkeypatched, []),
    (
        "called function",
        _called_function,
        ["LLM_API_KEY", "LLM_PROVIDER", "OPENAI_BASE_URL", "SMARTMEMORY_LLM_MODEL"],
    ),
    ("write then delete", _write_then_delete, []),
    ("deleted pre-existing var", _deleted_existing, ["DEEPSEEK_API_KEY"]),
]


@pytest.mark.parametrize(
    "leak, expected", [(r[1], r[2]) for r in ROWS], ids=[r[0] for r in ROWS]
)
def test_guard_table(leak, expected, monkeypatch):
    # A pre-existing var for the deletion row; monkeypatch restores it afterwards.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-present")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    before = _provider_env_snapshot()
    leak()
    if not expected:
        assert_provider_env_unchanged(before)
        return
    with pytest.raises(AssertionError) as caught:
        assert_provider_env_unchanged(before)
    message = str(caught.value)
    for name in expected:
        assert name in message
    assert "synthetic" not in message  # names only, never values
    assert _provider_env_snapshot() == before  # leak was undone
