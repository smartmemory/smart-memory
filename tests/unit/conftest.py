"""Unit-test fixtures for smartmemory_app."""

import pytest


@pytest.fixture(autouse=True)
def _first_run_models_assumed_present(monkeypatch):
    """Unit CLI tests mock storage, so the first-run model fetch must not probe this machine.

    LITE-FIRSTRUN-SPACY-1: tests/cli/test_first_run_models.py resets this flag and
    drives the real check.
    """
    import smartmemory_app.cli as cli_module

    monkeypatch.setattr(cli_module, "_first_run_models_ready", True)
