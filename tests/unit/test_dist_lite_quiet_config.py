"""DIST-LITE-QUIET-1 — config.get_api_key() must not warn on the local tier.

The "No API key found" UserWarning is correct guidance only when a key is
*expected* (remote mode). On the FREE/local default it is the single worst
first-impression noise line, since local mode needs no key by design.

See smart-memory-docs/docs/features/DIST-LITE-QUIET-1/ (decision D1).
"""

from __future__ import annotations

import warnings

import pytest

from smartmemory_app import config


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch):
    """Force the no-key path deterministically regardless of host keyring/env."""
    monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
    # keyring is imported inside get_api_key(); stub it to return no key.
    try:
        import keyring

        monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    except Exception:
        pass


def _make_cfg(mode):
    cfg = config.SmartMemoryConfig()
    cfg.mode = mode
    return cfg


@pytest.mark.parametrize("mode", ["local", None])
def test_get_api_key_silent_on_local_tier(monkeypatch, mode):
    monkeypatch.setattr(config, "load_config", lambda: _make_cfg(mode))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        key = config.get_api_key()
    assert key == ""
    offenders = [w for w in caught if "No API key found" in str(w.message)]
    assert not offenders, (
        f"local/unconfigured tier must not surface the 'No API key' warning; "
        f"got: {[str(w.message) for w in offenders]}"
    )


def test_get_api_key_warns_in_remote_mode(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: _make_cfg("remote"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        key = config.get_api_key()
    assert key == ""
    offenders = [w for w in caught if "No API key found" in str(w.message)]
    assert offenders, "remote mode without a key MUST warn (actionable setup guidance)"
