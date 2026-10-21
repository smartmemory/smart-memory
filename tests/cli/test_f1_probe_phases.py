"""Bounded offline phases preserve results and reuse only proven daemon warmup."""

import json
import subprocess
from types import SimpleNamespace

from smartmemory_app import daemon, diagnostic_process
from smartmemory_app.diagnostic_process import offline_checks as checks


def test_phases_have_separate_budgets_and_cached_embedding(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostic_process, "offline_checks", checks)
    cached = (
        "Embedding runtime: OK (onnxruntime/cpu, cached startup warmup, 384 dimensions)"
    )
    health = dict(
        service="smartmemory",
        status="ok",
        mode="lite",
        embedding_check=cached,
        data_dir=str(tmp_path),
        embedding_provider="local",
    )
    monkeypatch.setattr(
        daemon, "_health_response", lambda _: SimpleNamespace(json=lambda: health)
    )
    calls = []

    def run(args, **kwargs):
        calls.append((args[-1], kwargs["timeout"]))
        return SimpleNamespace(
            stdout=json.dumps(args[-1] + ": OK").encode(), returncode=0
        )

    monkeypatch.setattr(subprocess, "run", run)
    rows = checks("doctor")
    assert calls == [
        ("owners", 40),
        ("sqlite", 6),
        ("dependencies", 6),
        ("native", 10),
        ("vectors", 10),
    ]
    assert cached in rows
    assert len([row for row in rows if row.startswith("Diagnostic phase")]) == 6
    health["status"] = "degraded"
    checks("doctor")
    assert calls[-1] == ("embedding", 20)


def test_embedding_timeout_is_incomplete_and_completed_rows_survive(
    monkeypatch, caplog
):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            args, kwargs["timeout"], output=b'"Completed check: OK"\n'
        )

    monkeypatch.setattr(subprocess, "run", run)
    rows = checks("embedding", budget=20)
    assert rows[0] == "Completed check: OK"
    assert (
        rows[-1]
        == "Embedding runtime: not checked (probe exceeded 20 s; slow cold start)"
    )
    assert "warning:" not in rows[-1] and "Fix:" not in rows[-1]
    assert "not checked" in caplog.text
