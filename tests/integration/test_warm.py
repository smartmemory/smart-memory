"""DIST-LITE-WARMSTART-1 — background model warming orchestration.

Tests the *scheduling* contract (opt-out + warm-once), not the model load itself
(the embedder/reranker loads are verified separately and would require a model
download here). Marked integration: exercises real threads.
"""

import threading

import pytest

import smartmemory_app.warm as warm

pytestmark = pytest.mark.integration


def _join_warm_threads(timeout=5):
    for t in threading.enumerate():
        if t.name == "smartmemory-warm":
            t.join(timeout=timeout)


def test_background_warm_opts_out(monkeypatch):
    """SMARTMEMORY_NO_WARM=1 schedules nothing — no thread, no state change."""
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    warm._warm_started = False
    before = threading.active_count()

    warm.warm_models_background()

    assert warm._warm_started is False
    assert threading.active_count() == before


def test_background_warm_runs_once(monkeypatch):
    """Repeated calls schedule the warm work exactly once per process."""
    monkeypatch.delenv("SMARTMEMORY_NO_WARM", raising=False)
    warm._warm_started = False
    calls = []
    # Substitute the downstream loader (verified for real elsewhere) so this test
    # asserts the SCHEDULER, deterministically and without a model download.
    monkeypatch.setattr(warm, "warm_models", lambda **kwargs: calls.append(kwargs))

    warm.warm_models_background()
    warm.warm_models_background()
    warm.warm_models_background()
    _join_warm_threads()

    assert warm._warm_started is True
    assert len(calls) == 1  # only the first call did work


def test_warm_models_never_raises(monkeypatch):
    """warm_models() must swallow loader failures — a warm error can't break callers."""
    import smartmemory.plugins.embedding as emb

    def boom(self):
        raise RuntimeError("simulated model load failure")

    monkeypatch.setattr(emb.EmbeddingService, "warm", boom)
    # reranker=False so we don't trigger a real cross-encoder download here.
    warm.warm_models(reranker=False)  # must not raise


def test_is_warm_false_when_unloaded(monkeypatch):
    """is_warm() reports False when no local model is resident."""
    import smartmemory.plugins.embedding as emb

    monkeypatch.setattr(emb.EmbeddingService, "_st_model", None, raising=False)
    monkeypatch.setattr(emb.EmbeddingService, "_pinned_local_model", None, raising=False)
    assert warm.is_warm() is False


def test_cli_warm_notice_prints_once_when_cold(monkeypatch, capsys):
    """The direct-CLI cold-load notice fires exactly once, only when not warm."""
    from smartmemory_app import cli

    monkeypatch.setattr("smartmemory_app.warm.is_warm", lambda: False)
    cli._warm_notice_shown = False

    cli._warm_notice()
    cli._warm_notice()

    err = capsys.readouterr().err
    assert err.count("First run: loading local models") == 1


def test_add_cmd_direct_path_emits_notice(monkeypatch):
    """The real `add` command, on the direct (no-daemon) path with a cold model,
    emits the warm notice before the blocking ingest."""
    from click.testing import CliRunner

    from smartmemory_app import cli

    monkeypatch.setattr(cli, "_daemon_request", lambda *a, **k: None)  # force direct path
    monkeypatch.setattr("smartmemory_app.storage.ingest", lambda *a, **k: "id-123")
    monkeypatch.setattr("smartmemory_app.warm.is_warm", lambda: False)
    cli._warm_notice_shown = False

    r = CliRunner(mix_stderr=False).invoke(cli.cli, ["add", "hello world"])

    assert r.exit_code == 0, r.output
    assert "id-123" in r.stdout
    assert "First run: loading local models" in r.stderr
