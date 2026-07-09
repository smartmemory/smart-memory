import os
import shutil
from pathlib import Path

import pytest

from smartmemory_app import tour

pytestmark = [
    pytest.mark.integration,
    pytest.mark.needs_live_daemon,
]

if not os.environ.get("SMARTMEMORY_RUN_LIVE_DAEMON_TESTS"):
    pytestmark.append(
        pytest.mark.skip(
            reason=(
                "Set SMARTMEMORY_RUN_LIVE_DAEMON_TESTS=1 in an unsandboxed "
                "environment; this test binds localhost through viewer_server.main()."
            )
        )
    )


def test_live_tour_arc_search_recall_and_default_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data_dir = tmp_path / "real-user-data"
    user_data_dir.mkdir()
    sentinel = user_data_dir / "sentinel.txt"
    sentinel.write_text("do not touch")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(user_data_dir))

    runner = tour.TourSessionRunner(no_viewer=True, pace_seconds=0)
    result = runner.run()

    assert result.store_was_empty is True
    assert result.store_populated is True
    assert result.data_dir.exists() is False
    assert sentinel.read_text() == "do not touch"
    assert "Postgres" in result.search_text
    assert result.recall_text.strip()
    assert result.receipt.full_context_tokens > result.receipt.recall_tokens


def test_live_tour_keep_preserves_isolated_store(tmp_path: Path) -> None:
    runner = tour.TourSessionRunner(keep=True, no_viewer=True, pace_seconds=0)
    result = runner.run()

    try:
        assert result.store_was_empty is True
        assert result.store_populated is True
        assert result.data_dir.exists() is True
        assert any(result.data_dir.iterdir())
    finally:
        shutil.rmtree(result.data_dir, ignore_errors=True)
