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
            resp.json.return_value = {"service": "smartmemory", "status": "ok", "memories": 5}
            return resp

    # Patch is_running to return True so get_status proceeds to the httpx call
    with patch("smartmemory_app.daemon.is_running", return_value=True), \
         patch("httpx.Client", FakeClient):
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

    with patch("httpx.Client", FakeClient), \
         patch("time.sleep"):  # skip the retry sleep
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

    with patch("httpx.Client", FakeClient), \
         patch("time.sleep"):
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
    assert _DAEMON_LOG_HINT in msg, f"Expected log hint '{_DAEMON_LOG_HINT}' in: {msg!r}"


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


def test_start_daemon_port_conflict_message():
    """start_daemon raises RuntimeError with the port-conflict hint when port is open
    but health check fails (another process is using the port)."""
    from smartmemory_app.daemon import start_daemon
    import socket

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

    with patch("smartmemory_app.daemon._launchd_manages_daemon", return_value=False), \
         patch("smartmemory_app.daemon.is_running", return_value=False), \
         patch("subprocess.Popen", return_value=fake_proc), \
         patch("socket.socket", return_value=fake_socket), \
         patch("builtins.open", MagicMock()):
        with pytest.raises(RuntimeError) as exc_info:
            start_daemon()

    msg = str(exc_info.value)
    assert "in use" in msg.lower() or "sm status" in msg, (
        f"Expected port-conflict hint in RuntimeError, got: {msg!r}"
    )
