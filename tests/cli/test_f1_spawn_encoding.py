"""Detached Windows daemon receives explicit UTF-8 unless the user chose an encoding."""

import pytest
from smartmemory_app import daemon


@pytest.mark.parametrize("override", [None, "cp1252:replace"])
def test_daemon_spawn_stdio_encoding(tmp_path, monkeypatch, override):
    monkeypatch.setattr(daemon, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", lambda: None)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda: False)
    monkeypatch.setattr(daemon, "get_status", lambda: None)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: False)
    if override:
        monkeypatch.setenv("PYTHONIOENCODING", override)
    else:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    captured = {}

    def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("test_F1_spawn intercepted")

    monkeypatch.setattr(daemon, "spawn_detached", spawn)
    with pytest.raises(RuntimeError, match="test_F1_spawn intercepted"):
        daemon.start_daemon()
    assert captured["PYTHONIOENCODING"] == (override or "utf-8")
