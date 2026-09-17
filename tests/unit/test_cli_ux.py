"""FIX-C unit tests: wrapper CLI UX improvements.

Covers:
- L4: trust_env=False on httpx.Client for all daemon calls
- L1: Friendly ClickException messages (no raw tracebacks) for:
    connection refused/timeout and HTTP 5xx responses
- L2: --version flag on both entry points
- Port-conflict message in start_daemon (daemon.py Phase 2)
"""

import pytest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


# ── L2: --version ────────────────────────────────────────────────────────────


def test_version_flag(runner):
    """--version prints the package version and exits 0."""
    from smartmemory_app.cli import cli

    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    # Output should contain a version string (digits and dots)
    import re

    assert re.search(r"\d+\.\d+\.\d+", result.output), (
        f"Expected version string in output, got: {result.output!r}"
    )


# ── L4: trust_env=False ───────────────────────────────────────────────────────


def test_daemon_request_uses_trust_env_false():
    """_daemon_request constructs httpx.Client with trust_env=False."""
    import httpx
    from smartmemory_app.cli import _daemon_request

    captured_kwargs = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {"item_id": "test-id"}
            resp.raise_for_status = lambda: None
            return resp

    with patch("httpx.Client", FakeClient):
        result = _daemon_request("GET", "/memory/test")

    assert captured_kwargs.get("trust_env") is False, (
        f"Expected trust_env=False, got kwargs={captured_kwargs}"
    )
    assert result == {"item_id": "test-id"}


def test_daemon_is_running_uses_trust_env_false():
    """is_running() constructs httpx.Client with trust_env=False."""
    import httpx
    from smartmemory_app.daemon import is_running

    captured_kwargs = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.json.return_value = {"service": "smartmemory", "status": "ok"}
            return resp

    with patch("httpx.Client", FakeClient):
        result = is_running()

    assert captured_kwargs.get("trust_env") is False, (
        f"is_running: expected trust_env=False, got {captured_kwargs}"
    )
    assert result is True


def test_get_status_uses_trust_env_false():
    """get_status() constructs httpx.Client with trust_env=False."""
    import httpx
    from smartmemory_app.daemon import get_status

    captured_kwargs = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.json.return_value = {
                "service": "smartmemory",
                "status": "ok",
                "memories": 5,
            }
            return resp

    # Patch is_running to return True so get_status proceeds to the httpx call
    with (
        patch("smartmemory_app.daemon.is_running", return_value=True),
        patch("httpx.Client", FakeClient),
    ):
        result = get_status()

    assert captured_kwargs.get("trust_env") is False, (
        f"get_status: expected trust_env=False, got {captured_kwargs}"
    )
    assert result is not None


# ── L1: Friendly error messages ───────────────────────────────────────────────


def test_daemon_request_connect_error_returns_none_with_notice(capsys):
    """ConnectError after retry returns None (callers fall back to direct local
    storage — add/search/get all branch on None) and prints a stderr notice, never
    a raw traceback. Codex review 2026-07-03: raising here killed the fallback."""
    import httpx
    from smartmemory_app.cli import _daemon_request

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            raise httpx.ConnectError("Connection refused")

    with patch("httpx.Client", FakeClient), patch("time.sleep"):  # skip the retry sleep
        result = _daemon_request("GET", "/memory/test")

    assert result is None
    err = capsys.readouterr().err
    assert "not running" in err.lower() or "sm start" in err, (
        f"Expected friendly 'not running' stderr notice, got: {err!r}"
    )
    assert "traceback" not in err.lower()


def test_daemon_request_connect_timeout_returns_none_with_notice(capsys):
    """ConnectTimeout after retry returns None (local fallback) with stderr notice."""
    import httpx
    from smartmemory_app.cli import _daemon_request

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            raise httpx.ConnectTimeout("Timed out connecting")

    with patch("httpx.Client", FakeClient), patch("time.sleep"):
        result = _daemon_request("GET", "/memory/test")

    assert result is None
    err = capsys.readouterr().err
    assert "not running" in err.lower() or "sm start" in err, (
        f"Expected friendly 'not running' stderr notice, got: {err!r}"
    )


def test_daemon_request_read_timeout_raises_friendly_message():
    """ReadTimeout raises ClickException with a log-hint (no raw traceback)."""
    import httpx
    import click
    from smartmemory_app.cli import _daemon_request, _DAEMON_LOG_HINT

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("Read timed out")

    with patch("httpx.Client", FakeClient):
        with pytest.raises(click.ClickException) as exc_info:
            _daemon_request("GET", "/memory/test")

    msg = exc_info.value.format_message()
    assert "daemon" in msg.lower(), f"Expected daemon mention, got: {msg!r}"
    assert _DAEMON_LOG_HINT in msg, (
        f"Expected log hint '{_DAEMON_LOG_HINT}' in: {msg!r}"
    )


def test_daemon_request_http_5xx_adds_log_hint():
    """HTTP 5xx response raises ClickException with daemon.log pointer."""
    import httpx
    import click
    from smartmemory_app.cli import _daemon_request, _DAEMON_LOG_HINT

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 500
            resp.json.return_value = {"detail": "Internal Server Error"}
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=MagicMock(),
                response=resp,
            )
            return resp

    with patch("httpx.Client", FakeClient):
        with pytest.raises(click.ClickException) as exc_info:
            _daemon_request("GET", "/memory/test")

    msg = exc_info.value.format_message()
    assert _DAEMON_LOG_HINT in msg, (
        f"Expected log hint '{_DAEMON_LOG_HINT}' in 5xx error message, got: {msg!r}"
    )
    assert "Internal Server Error" in msg, f"Expected detail in message, got: {msg!r}"


def test_daemon_request_http_4xx_no_log_hint():
    """HTTP 4xx response raises ClickException WITHOUT daemon.log pointer."""
    import httpx
    import click
    from smartmemory_app.cli import _daemon_request, _DAEMON_LOG_HINT

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 422
            resp.json.return_value = {"detail": "Unprocessable Entity"}
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "422 Unprocessable Entity",
                request=MagicMock(),
                response=resp,
            )
            return resp

    with patch("httpx.Client", FakeClient):
        with pytest.raises(click.ClickException) as exc_info:
            _daemon_request("GET", "/memory/test")

    msg = exc_info.value.format_message()
    assert "Unprocessable Entity" in msg
    assert _DAEMON_LOG_HINT not in msg, (
        f"4xx errors should NOT include log hint, but got: {msg!r}"
    )


# ── Port-conflict message ──────────────────────────────────────────────────────


def test_start_daemon_port_conflict_message_includes_log_tail(tmp_path):
    """start_daemon raises RuntimeError with the port-conflict hint when port is open
    but health check fails (another process is using the port)."""
    from smartmemory_app.daemon import start_daemon

    log_path = tmp_path / "daemon.log"
    log_path.write_text("Loading backend...\nfatal startup detail\n")

    # Simulate: launchd doesn't manage it, subprocess starts but port opens immediately,
    # but is_running returns False (another process, not SmartMemory).
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # process appears alive

    def fake_connect_ex(addr):
        return 0  # port appears open immediately

    fake_socket = MagicMock()
    fake_socket.__enter__ = lambda s: s
    fake_socket.__exit__ = MagicMock(return_value=False)
    fake_socket.connect_ex = fake_connect_ex

    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].close()
        return fake_proc

    with (
        patch("smartmemory_app.daemon._data_dir", return_value=tmp_path),
        patch("smartmemory_app.daemon._launchd_manages_daemon", return_value=False),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("socket.socket", return_value=fake_socket),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            start_daemon()

    msg = str(exc_info.value)
    assert "in use" in msg.lower() or "sm status" in msg, (
        f"Expected port-conflict hint in RuntimeError, got: {msg!r}"
    )
    assert "fatal startup detail" in msg
    assert str(log_path) in msg


def test_start_daemon_timeout_survives_missing_log(tmp_path):
    """A missing daemon log never masks the original startup timeout."""
    from smartmemory_app.daemon import start_daemon

    log_path = tmp_path / "daemon.log"
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_socket = MagicMock()
    fake_socket.connect_ex.return_value = 1

    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].close()
        log_path.unlink()
        return fake_proc

    with (
        patch("smartmemory_app.daemon._data_dir", return_value=tmp_path),
        patch("smartmemory_app.daemon._launchd_manages_daemon", return_value=False),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("socket.socket", return_value=fake_socket),
        patch("time.sleep"),
    ):
        with pytest.raises(TimeoutError) as exc_info:
            start_daemon()

    msg = str(exc_info.value)
    assert "within 60s" in msg
    assert "No daemon log was written" in msg
    assert str(log_path) in msg


def test_launchd_start_waits_through_keepalive_throttle_gap(tmp_path):
    """Ten seconds without a process in a crash-loop is not an early verdict."""
    from smartmemory_app import daemon

    plist = tmp_path / "ai.smartmemory.daemon.plist"
    plist.write_text("plist")
    health = {"service": "smartmemory", "status": "ok"}
    # Initial idempotence probe plus 20 half-second launch probes, then recovery.
    health_checks = [None] * 21 + [health]

    with (
        patch.object(daemon, "get_status", side_effect=health_checks),
        patch.object(daemon, "_data_dir", return_value=tmp_path),
        patch.object(daemon, "_launchd_manages_daemon", return_value=True),
        patch.object(daemon, "_launchd_plist_path", return_value=plist),
        patch.object(daemon, "_launchd_loaded", return_value=True),
        patch("time.sleep"),
    ):
        assert daemon.start_daemon() == health


def test_launchd_start_can_return_observed_warming(tmp_path):
    from smartmemory_app import daemon

    plist = tmp_path / "ai.smartmemory.daemon.plist"
    plist.write_text("plist")
    warming = {"service": "smartmemory", "status": "warming"}

    with (
        patch.object(daemon, "get_status", side_effect=[None, warming]),
        patch.object(daemon, "_data_dir", return_value=tmp_path),
        patch.object(daemon, "_launchd_manages_daemon", return_value=True),
        patch.object(daemon, "_launchd_plist_path", return_value=plist),
        patch.object(daemon, "_launchd_loaded", return_value=True),
        patch("time.sleep"),
    ):
        assert daemon.start_daemon(wait_until_ready=False) == warming


def test_subprocess_start_can_return_observed_warming(tmp_path):
    from smartmemory_app import daemon

    warming = {"service": "smartmemory", "status": "warming"}
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_socket = MagicMock()
    fake_socket.connect_ex.return_value = 0

    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].close()
        return fake_proc

    with (
        patch.object(daemon, "get_status", side_effect=[None, warming]),
        patch.object(daemon, "_data_dir", return_value=tmp_path),
        patch.object(daemon, "_launchd_manages_daemon", return_value=False),
        patch.object(daemon, "_port", return_value=19015),
        patch.object(daemon, "_start_workers") as start_workers,
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("socket.socket", return_value=fake_socket),
    ):
        assert daemon.start_daemon(wait_until_ready=False) == warming

    start_workers.assert_called_once_with(1)


def test_start_daemon_default_waits_past_existing_warming(tmp_path):
    from smartmemory_app import daemon

    warming = {"service": "smartmemory", "status": "warming"}
    health = {"service": "smartmemory", "status": "ok"}

    with (
        patch.object(daemon, "get_status", side_effect=[warming, warming, health]),
        patch.object(daemon, "_data_dir", return_value=tmp_path),
        patch("time.sleep"),
    ):
        assert daemon.start_daemon() == health


# ── Startup progress and truthful outcomes ───────────────────────────────────


@pytest.mark.parametrize("command", ["start", "restart"])
def test_start_commands_report_verified_healthy_result(runner, command):
    """A success line is printed only for an explicit healthy payload."""
    from smartmemory_app.cli import cli

    health = {"service": "smartmemory", "status": "ok"}
    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("smartmemory_app.daemon.stop_daemon"),
        patch("smartmemory_app.daemon.start_daemon", return_value=health),
    ):
        result = runner.invoke(cli, [command])

    assert result.exit_code == 0, result.output
    assert "SmartMemory is ready." in result.output


def test_start_returns_on_observed_warming_by_default(runner):
    """Ordinary start is fast but reports only the state observed from /health."""
    from smartmemory_app.cli import cli

    warming = {"service": "smartmemory", "status": "warming"}
    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch(
            "smartmemory_app.daemon.start_daemon", return_value=warming
        ) as start_daemon,
    ):
        result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory is warming up." in result.output
    assert "SmartMemory is ready." not in result.output
    assert start_daemon.call_args.kwargs["wait_until_ready"] is False


def test_start_wait_blocks_until_verified_ready(runner):
    from smartmemory_app.cli import cli

    health = {"service": "smartmemory", "status": "ok"}
    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch(
            "smartmemory_app.daemon.start_daemon", return_value=health
        ) as start_daemon,
    ):
        result = runner.invoke(cli, ["start", "--wait"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory is ready." in result.output
    assert start_daemon.call_args.kwargs["wait_until_ready"] is True


def test_start_waits_when_existing_daemon_is_warming(runner):
    from smartmemory_app.cli import cli

    warming = {"service": "smartmemory", "status": "warming"}
    health = {"service": "smartmemory", "status": "ok"}
    with (
        patch("smartmemory_app.daemon.get_status", return_value=warming),
        patch(
            "smartmemory_app.daemon.start_daemon", return_value=health
        ) as start_daemon,
    ):
        result = runner.invoke(cli, ["start", "--wait"])

    assert result.exit_code == 0, result.output
    assert "SmartMemory is ready." in result.output
    assert start_daemon.call_args.kwargs["wait_until_ready"] is True


@pytest.mark.parametrize("command", ["start", "restart"])
def test_start_commands_report_degraded_reason_without_success(runner, command):
    """A degraded daemon is explained and never labelled ready."""
    from smartmemory_app.cli import cli

    health = {
        "service": "smartmemory",
        "status": "degraded",
        "degraded_reason": "The local model could not be loaded.",
    }
    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("smartmemory_app.daemon.stop_daemon"),
        patch("smartmemory_app.daemon.start_daemon", return_value=health),
    ):
        result = runner.invoke(cli, [command])

    assert result.exit_code == 0, result.output
    assert "SmartMemory started, but it needs attention." in result.output
    assert "The local model could not be loaded." in result.output
    assert "Run: sm doctor" in result.output
    assert "SmartMemory is ready." not in result.output
    assert "Daemon ready." not in result.output


@pytest.mark.parametrize("command", ["start", "restart"])
def test_start_commands_fail_when_nothing_answers(runner, command):
    """No health response is a failed command, never a success."""
    from smartmemory_app.cli import cli

    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("smartmemory_app.daemon.stop_daemon"),
        patch("smartmemory_app.daemon.start_daemon", return_value=None),
    ):
        result = runner.invoke(cli, [command])

    assert result.exit_code == 1
    assert "SmartMemory did not respond after startup." in result.output
    assert "SmartMemory is ready." not in result.output
    assert "Daemon ready." not in result.output


def test_status_says_should_be_running_when_pid_file_exists(runner, tmp_path):
    """A stale pid/plist points to restart instead of claiming it is uninstalled."""
    from smartmemory_app.cli import cli

    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("1234")
    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch("smartmemory_app.daemon._pid_file", return_value=pid_file),
        patch("smartmemory_app.daemon._launchd_manages_daemon", return_value=False),
    ):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "should be running, but it is not responding" in result.output
    assert "Run: sm restart" in result.output
    assert "Start with: smartmemory start" not in result.output


def test_status_keeps_not_running_message_without_runtime_markers(runner, tmp_path):
    """A genuinely uninstalled/stopped daemon keeps the established guidance."""
    from smartmemory_app.cli import cli

    with (
        patch("smartmemory_app.daemon.get_status", return_value=None),
        patch(
            "smartmemory_app.daemon._pid_file", return_value=tmp_path / "missing.pid"
        ),
        patch("smartmemory_app.daemon._launchd_manages_daemon", return_value=False),
    ):
        result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "SmartMemory daemon is not running." in result.output
    assert "Start with: smartmemory start" in result.output


def test_non_tty_startup_progress_has_no_spinner_control_output():
    """Pipes and service logs receive plain lines, not animated terminal frames."""
    from io import StringIO

    from smartmemory_app.progress import startup_progress

    class Pipe(StringIO):
        def isatty(self):
            return False

    stream = Pipe()
    emitted = []
    with startup_progress(
        "Starting SmartMemory", stream=stream, emit=emitted.append
    ) as on_log:
        on_log("Loading language tools...")

    assert emitted == ["  Loading language tools..."]
    assert stream.getvalue() == ""


def test_download_progress_is_throttled_newline_text_without_carriage_returns():
    """Model transfer updates are discrete log lines, never tqdm redraw fragments."""
    from smartmemory_app.hf_progress import DownloadProgressReporter

    emitted = []
    now = iter([0.0, 0.1, 0.2, 2.2, 2.3])
    reporter = DownloadProgressReporter(
        emitted.append, clock=lambda: next(now), interval=2.0
    )
    bar = reporter.new_bar(total=50 * 1024 * 1024, initial=0)
    bar.update(1 * 1024 * 1024)
    bar.update(5 * 1024 * 1024)
    bar.update(10 * 1024 * 1024)

    assert len(emitted) == 2
    assert emitted[0].startswith("Downloading the local AI model:")
    assert "32%" in emitted[-1]
    assert all("\r" not in line and "\n" not in line for line in emitted)


def test_backend_startup_lines_are_emitted_in_order(monkeypatch, capsys):
    """The daemon names each long startup step before doing the work."""
    from smartmemory_app import viewer_server

    class FakeService:
        provider = "local"

        def embed(self, text):
            assert text == "warmup"

    def fake_get_memory(on_progress=None):
        assert on_progress is not None
        for line in (
            "Loading language tools (spaCy)...",
            "Language tools ready (0.1s)",
            "Checking the local AI model...",
            "Local AI model ready (0.2s)",
            "Loading the spaCy language model and opening saved memories...",
            "Language model and saved memories ready (0.1s)",
        ):
            on_progress(line)
        return object()

    monkeypatch.setattr("smartmemory_app.storage.get_memory", fake_get_memory)
    monkeypatch.setattr("smartmemory.plugins.embedding.EmbeddingService", FakeService)

    assert viewer_server._warm_backend() is True
    output = capsys.readouterr().out
    expected = [
        "Loading SmartMemory...",
        "Loading language tools (spaCy)...",
        "Language tools ready",
        "Checking the local AI model...",
        "Local AI model ready",
        "Loading the spaCy language model and opening saved memories...",
        "Language model and saved memories ready",
        "Warming the search model...",
        "Search model ready",
        "SmartMemory startup complete",
    ]
    positions = [output.index(text) for text in expected]
    assert positions == sorted(positions)
