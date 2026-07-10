import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from smartmemory_app import tour


class FakeHttpClient:
    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.closed = False

    def ingest(self, content: str, memory_type: str = "semantic") -> dict:
        self.ingested.append(content)
        return {"item_id": f"item-{len(self.ingested)}"}

    def search(self, query: str, top_k: int = 5) -> dict:
        assert query == "which database did we pick"
        postgres = next(item for item in self.ingested if "Postgres" in item)
        return {
            "items": [
                {
                    "item_id": "pg-1",
                    "memory_type": "semantic",
                    "content": postgres,
                }
            ]
        }

    def recall(
        self, cwd: str | None = None, top_k: int = 8, query: str | None = None
    ) -> dict:
        return {"context": "\n".join(self.ingested[:top_k])}

    def close(self) -> None:
        self.closed = True


class EmptyResultHttpClient(FakeHttpClient):
    def search(self, query: str, top_k: int = 5) -> dict:
        return {"items": []}

    def recall(
        self, cwd: str | None = None, top_k: int = 8, query: str | None = None
    ) -> dict:
        return {"context": ""}


class FakeDaemon:
    def __init__(self, store_dir: Path, port: int = 19191, keep: bool = False) -> None:
        self.store_dir = store_dir
        self.port = port
        self.keep = keep
        self.started = False
        self.stopped = False
        self.store_was_empty = False
        self.store_populated = False
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> "FakeDaemon":
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.store_was_empty = not any(self.store_dir.iterdir())
        self.started = True
        return self

    def mark_populated(self) -> None:
        (self.store_dir / "memory.sqlite").write_text("seeded")
        self.store_populated = True

    def stop(self) -> None:
        self.stopped = True
        if self.store_dir.exists():
            self.store_populated = self.store_populated or any(self.store_dir.iterdir())
        if not self.keep and self.store_dir.exists():
            for path in self.store_dir.iterdir():
                path.unlink()
            self.store_dir.rmdir()


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def httpx_client_for(responses: list[FakeResponse], calls: list[str]) -> type:
    class SequencedHttpxClient:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def get(self, url: str):
            calls.append(url)
            return responses.pop(0)

    return SequencedHttpxClient


def test_token_receipt_uses_real_tiktoken_counts() -> None:
    facts = [
        "Project Atlas chose Postgres for reporting queries.",
        "Deploys go through GitHub Actions, never manual laptop deploys.",
    ]
    recall = "Project Atlas chose Postgres for reporting queries."

    receipt = tour.compute_token_receipt(facts, recall)
    expanded = tour.compute_token_receipt(
        facts + ["The staging cache key prefix is atlas:v2:receipt-check."],
        recall,
    )

    assert receipt.full_context_tokens == tour.count_tokens(
        tour.build_claude_md_equivalent(facts)
    )
    assert receipt.recall_tokens == tour.count_tokens(recall)
    assert expanded.full_context_tokens > receipt.full_context_tokens


def test_arc_driver_seeds_searches_recalls_and_receipts(tmp_path: Path) -> None:
    client = FakeHttpClient()
    driver = tour.TourArcDriver(
        client=client,
        facts=tour.DEFAULT_TOUR_FACTS,
        cwd=tmp_path,
        pace_seconds=0,
    )

    result = driver.run_default_arc(include_claude_import=False)

    assert client.ingested == list(tour.DEFAULT_TOUR_FACTS)
    assert "Postgres" in result.search_text
    assert result.recall_text.strip()
    assert result.receipt.full_context_tokens == tour.count_tokens(
        tour.build_claude_md_equivalent(tour.DEFAULT_TOUR_FACTS)
    )
    assert result.receipt.recall_tokens == tour.count_tokens(result.recall_text)


def test_arc_driver_zero_step_dwell_does_not_sleep_between_receipt_and_recall(
    tmp_path: Path,
) -> None:
    client = FakeHttpClient()
    events: list[tour.TourEvent] = []
    driver = tour.TourArcDriver(
        client=client,
        facts=tour.DEFAULT_TOUR_FACTS,
        cwd=tmp_path,
        pace_seconds=0,
        step_dwell_seconds=0,
    )

    with patch("smartmemory_app.tour.time.sleep") as mock_sleep:
        driver.run_default_arc(include_claude_import=False, emit=events.append)

    mock_sleep.assert_not_called()
    titles = [event.title for event in events]
    receipt_index = titles.index("Token receipt")
    recall_index = titles.index("Cross-session recall")
    assert receipt_index + 1 == recall_index


def test_arc_driver_step_dwell_runs_before_receipt_and_recall_emits(
    tmp_path: Path,
) -> None:
    client = FakeHttpClient()
    timeline: list[str] = []
    driver = tour.TourArcDriver(
        client=client,
        facts=tour.DEFAULT_TOUR_FACTS,
        cwd=tmp_path,
        pace_seconds=0,
        step_dwell_seconds=1.25,
    )

    def record_sleep(seconds: float) -> None:
        timeline.append(f"sleep:{seconds}")

    def record_emit(event: tour.TourEvent) -> None:
        timeline.append(f"emit:{event.title}")

    with patch(
        "smartmemory_app.tour.time.sleep", side_effect=record_sleep
    ) as mock_sleep:
        driver.run_default_arc(include_claude_import=False, emit=record_emit)

    assert mock_sleep.call_count == 5
    assert all(call.args == (1.25,) for call in mock_sleep.call_args_list)
    assert timeline[timeline.index("emit:Token receipt") - 1] == "sleep:1.25"
    assert timeline[timeline.index("emit:Cross-session recall") - 1] == "sleep:1.25"


def test_arc_driver_degrades_when_search_or_recall_do_not_return_seeded_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = EmptyResultHttpClient()
    events: list[tour.TourEvent] = []
    driver = tour.TourArcDriver(
        client=client,
        facts=tour.DEFAULT_TOUR_FACTS,
        cwd=tmp_path,
        pace_seconds=0,
    )

    with caplog.at_level("WARNING", logger="smartmemory_app.tour"):
        result = driver.run_default_arc(
            include_claude_import=False,
            emit=events.append,
        )

    assert result.success is False
    assert "search did not return the seeded Postgres fact" in result.degraded_reason
    assert "recall did not return non-empty seeded content" in result.degraded_reason
    assert any("DEGRADED" in event.body for event in events)
    assert "SmartMemory tour degraded" in caplog.text


def test_no_viewer_runner_skips_webbrowser_and_runs_arc(tmp_path: Path) -> None:
    fake_daemon = FakeDaemon(tmp_path / "tour-store")
    fake_client = FakeHttpClient()

    def daemon_factory(port: int | None, keep: bool):
        fake_daemon.keep = keep
        return fake_daemon

    with patch("smartmemory_app.tour.webbrowser.open") as mock_open:
        runner = tour.TourSessionRunner(
            keep=False,
            code=False,
            port=19191,
            no_viewer=True,
            daemon_factory=daemon_factory,
            client_factory=lambda base_url: fake_client,
            pace_seconds=0,
            step_dwell_seconds=0,
        )
        result = runner.run()

    mock_open.assert_not_called()
    assert fake_daemon.started is True
    assert fake_daemon.stopped is True
    assert fake_daemon.store_was_empty is True
    assert fake_daemon.store_populated is True
    assert fake_daemon.store_dir.exists() is False
    assert len(fake_client.ingested) == len(tour.DEFAULT_TOUR_FACTS)
    assert "Postgres" in result.search_text


def test_tour_session_runner_uses_step_dwell_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SMARTMEMORY_TOUR_STEP_DWELL", "1.5")
    fake_daemon = FakeDaemon(tmp_path / "tour-store")
    fake_client = FakeHttpClient()

    runner = tour.TourSessionRunner(
        no_viewer=True,
        daemon_factory=lambda port, keep: fake_daemon,
        client_factory=lambda base_url: fake_client,
        pace_seconds=0,
    )
    with patch("smartmemory_app.tour.time.sleep") as mock_sleep:
        runner.run()

    assert mock_sleep.call_count == 5
    assert all(call.args == (1.5,) for call in mock_sleep.call_args_list)


def test_tour_session_runner_warns_and_uses_default_for_bad_step_dwell_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SMARTMEMORY_TOUR_STEP_DWELL", "abc")

    with caplog.at_level("WARNING", logger="smartmemory_app.tour"):
        runner = tour.TourSessionRunner(no_viewer=True, pace_seconds=0)

    assert runner.step_dwell_seconds == 4.0
    assert "SMARTMEMORY_TOUR_STEP_DWELL" in caplog.text
    assert "abc" in caplog.text


def test_tour_daemon_forces_isolated_local_env_for_popen(tmp_path: Path) -> None:
    responses = [
        FakeResponse(200, {"ok": True}),
        FakeResponse(200, {"nodes": [], "edges": [], "node_count": 0}),
    ]
    http_calls: list[str] = []
    process = FakeProcess()
    popen_calls: list[dict] = []

    def fake_popen(args, **kwargs):
        popen_calls.append({"args": args, "kwargs": kwargs})
        return process

    with (
        patch("smartmemory_app.tour._is_port_available", return_value=True),
        patch("smartmemory_app.tour.subprocess.Popen", side_effect=fake_popen),
        patch(
            "smartmemory_app.tour.httpx.Client",
            httpx_client_for(responses, http_calls),
        ),
    ):
        daemon = tour.TourDaemon(port=19191, data_dir=tmp_path / "tour-store").start()
        try:
            env = popen_calls[0]["kwargs"]["env"]
            assert env["SMARTMEMORY_MODE"] == "local"
            assert env["SMARTMEMORY_DATA_DIR"] == str(tmp_path / "tour-store")
            assert env["SMARTMEMORY_DAEMON_PORT"] == str(daemon.port)
            assert env["SMARTMEMORY_EMBEDDING_PROVIDER"] == "local"
        finally:
            daemon.stop()

    assert process.terminated is True
    assert (tmp_path / "tour-store").exists() is False
    assert http_calls[0].endswith("/health")
    assert http_calls[1].endswith("/memory/graph/full")


def test_tour_daemon_rejects_occupied_explicit_port(tmp_path: Path) -> None:
    port = 19000

    with (
        patch("smartmemory_app.tour._is_port_available", return_value=False),
        patch("smartmemory_app.tour.subprocess.Popen") as mock_popen,
    ):
        with pytest.raises(RuntimeError, match=f"port {port} is in use"):
            tour.TourDaemon(port=port, data_dir=tmp_path / "tour-store").start()

    mock_popen.assert_not_called()
    assert (tmp_path / "tour-store").exists() is False


def test_empty_graph_identity_check_aborts_before_ingest(tmp_path: Path) -> None:
    responses = [
        FakeResponse(200, {"ok": True}),
        FakeResponse(
            200,
            {
                "nodes": [{"item_id": "real-memory", "content": "do not touch"}],
                "edges": [],
                "node_count": 1,
            },
        ),
    ]
    http_calls: list[str] = []
    process = FakeProcess()
    client_created = False

    def fail_if_client_created(base_url: str) -> FakeHttpClient:
        nonlocal client_created
        client_created = True
        return FakeHttpClient()

    with (
        patch("smartmemory_app.tour._is_port_available", return_value=True),
        patch("smartmemory_app.tour.subprocess.Popen", return_value=process),
        patch(
            "smartmemory_app.tour.httpx.Client",
            httpx_client_for(responses, http_calls),
        ),
    ):
        runner = tour.TourSessionRunner(
            no_viewer=True,
            port=19192,
            daemon_factory=lambda port, keep: tour.TourDaemon(
                port=port,
                keep=keep,
                data_dir=tmp_path / "tour-store",
            ),
            client_factory=fail_if_client_created,
            pace_seconds=0,
        )
        with pytest.raises(RuntimeError, match="non-empty memory graph"):
            runner.run()

    assert client_created is False
    assert process.terminated is True
    assert (tmp_path / "tour-store").exists() is False
    assert http_calls[1].endswith("/memory/graph/full")


def test_teardown_runs_on_early_quit_and_exception_mid_arc(tmp_path: Path) -> None:
    early_daemon = FakeDaemon(tmp_path / "early-store")

    def fake_tour_daemon(*, port: int | None = None, keep: bool = False):
        early_daemon.port = port or early_daemon.port
        early_daemon.keep = keep
        return early_daemon

    with (
        patch("smartmemory_app.tour.TourDaemon", side_effect=fake_tour_daemon),
        patch.object(tour.TourApp, "run", return_value=None),
    ):
        assert tour.run_tour(no_viewer=True) is None

    assert early_daemon.started is True
    assert early_daemon.stopped is True
    assert early_daemon.store_dir.exists() is False

    failing_daemon = FakeDaemon(tmp_path / "failing-store")
    failing_client = FakeHttpClient()

    def fail_search(query: str, top_k: int = 5) -> dict:
        raise RuntimeError("mid-arc failure")

    failing_client.search = fail_search  # type: ignore[method-assign]
    runner = tour.TourSessionRunner(
        no_viewer=True,
        daemon_factory=lambda port, keep: failing_daemon,
        client_factory=lambda base_url: failing_client,
        pace_seconds=0,
    )

    with pytest.raises(RuntimeError, match="mid-arc failure"):
        runner.run()

    assert failing_client.closed is True
    assert failing_daemon.started is True
    assert failing_daemon.stopped is True
    assert failing_daemon.store_dir.exists() is False


def test_tour_app_mounts_headless_and_advances_steps() -> None:
    class StubRunner:
        def run(self, emit=None):
            if emit:
                emit(tour.TourEvent(step=0, title="Arrange viewer", body="Ready"))
                emit(
                    tour.TourEvent(
                        step=1, title="Seed facts", body="Postgres fact added"
                    )
                )
            return tour.TourRunResult(
                data_dir=Path("/tmp/smartmemory-tour-test"),
                port=19191,
                viewer_url="http://127.0.0.1:19191",
                seeded_count=1,
                search_text="Postgres fact",
                recall_text="Postgres fact",
                receipt=tour.compute_token_receipt(["Postgres fact"], "Postgres fact"),
                imported_claude=False,
                store_was_empty=True,
                store_populated=True,
            )

    async def exercise() -> None:
        app = tour.TourApp(runner=StubRunner())
        async with app.run_test(headless=True) as pilot:
            await pilot.pause()
            assert app.current_step == 0
            await pilot.press("enter")
            await pilot.pause()
            assert app.current_step >= 1
            assert any("Postgres" in event.body for event in app.event_history)

    asyncio.run(exercise())


def test_cli_tour_invokes_run_tour() -> None:
    from smartmemory_app.cli import cli

    runner = CliRunner()
    with patch("smartmemory_app.tour.run_tour") as mock_run:
        result = runner.invoke(
            cli,
            ["tour", "--keep", "--code", "--port", "19000", "--no-viewer"],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once_with(
        keep=True,
        code=True,
        port=19000,
        no_viewer=True,
    )
