"""Pytest configuration for smartmemory tests."""

import pytest


def pytest_collection_modifyitems(items):
    """Auto-mark tests in tests/integration/ with the integration marker."""
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _first_run_models_assumed_present(monkeypatch):
    """CLI tests mock storage, so the first-run model fetch must not probe this machine.

    LITE-FIRSTRUN-SPACY-1: tests/cli/test_first_run_models.py resets this flag and
    drives the real check.
    """
    import smartmemory_app.cli as cli_module

    monkeypatch.setattr(cli_module, "_first_run_models_ready", True)
