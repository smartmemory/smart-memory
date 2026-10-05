"""Doctor ownership classification, without changing destructive reset rules."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from smartmemory_app import daemon, store_diagnostics as diagnostics
from smartmemory_app.store_reset import preflight_store_reset


def test_healthy_daemon_is_ok_but_reset_still_refuses(tmp_path, monkeypatch):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "# smartmemory_app.viewer_server\nimport time; time.sleep(30)",
        ]
    )
    try:
        (tmp_path / "daemon.pid").write_text(str(child.pid))
        monkeypatch.setattr(daemon, "_data_dir", lambda: tmp_path)
        health = dict(
            service="smartmemory",
            status="ok",
            pid=child.pid,
            mode="lite",
            data_dir=str(tmp_path),
        )
        monkeypatch.setattr(
            daemon, "_health_response", lambda _: SimpleNamespace(json=lambda: health)
        )
        row = diagnostics.lock_row(tmp_path)
        assert "Write lock: OK" in row and str(child.pid) in row
        assert "Fix:" not in row and "stop" not in row
        with pytest.raises(RuntimeError, match="live owners"):
            preflight_store_reset(tmp_path)
        health["status"] = "degraded"
        assert "warning" in diagnostics.lock_row(tmp_path)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_foreign_and_dead_marker_warn(tmp_path):
    marker = tmp_path / "daemon.pid"
    marker.write_text(str(os.getpid()))
    assert "live store owner" in diagnostics.lock_row(tmp_path)
    marker.write_text("99999999")
    assert "stale" in diagnostics.lock_row(tmp_path)


def test_ok_dependencies_have_no_fix_hint(tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: SimpleNamespace(priority=1))
    rows = list(diagnostics.dependency_rows(tmp_path))
    assert all("Fix:" not in row for row in rows if ": OK" in row)
