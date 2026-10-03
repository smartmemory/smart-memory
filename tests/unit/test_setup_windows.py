"""Windows setup stays usable without enabling terminal mouse tracking."""

import io
import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import config, setup, setup_tui, tour

DISABLE_REPORTING = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?1004l"


@pytest.mark.parametrize(
    "platform,force,expected",
    [
        ("win32", None, False),
        ("win32", "0", False),
        ("win32", "1", True),
        ("darwin", None, True),
        ("linux", None, True),
    ],
)
def test_setup_tui_platform_gate(monkeypatch, caplog, platform, force, expected):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: True))
    monkeypatch.setattr("sys.stdout", Mock(isatty=lambda: True))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("SMARTMEMORY_FORCE_TUI", raising=False)
    if force is not None:
        monkeypatch.setenv("SMARTMEMORY_FORCE_TUI", force)
    with caplog.at_level(logging.DEBUG, logger=setup.__name__):
        assert setup._can_run_tui() is expected
    if not expected:
        assert "Skipping setup TUI on Windows" in caplog.text


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("tty", [False, True])
def test_setup_tui_restores_terminal(monkeypatch, raises, tty):
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: tty)
    flush = Mock(wraps=output.flush)
    monkeypatch.setattr(output, "flush", flush)
    monkeypatch.setattr("sys.stdout", output)
    result = setup.SetupResult()
    app = Mock(_setup_error=None)
    app.run.return_value = result
    if raises:
        app.run.side_effect = RuntimeError("terminal failed")
    monkeypatch.setattr(setup_tui, "SetupApp", lambda: app)
    if raises:
        with pytest.raises(RuntimeError, match="terminal failed"):
            setup_tui.run_setup_tui()
    else:
        assert setup_tui.run_setup_tui() is result
    assert output.getvalue() == (DISABLE_REPORTING if tty else "")
    assert flush.call_count == int(tty)


@pytest.mark.parametrize("failure", ["isatty", "write", "flush"])
def test_setup_tui_cleanup_never_masks_error(monkeypatch, failure):
    output = Mock()
    output.isatty.return_value = True
    getattr(output, failure).side_effect = OSError("closed terminal")
    monkeypatch.setattr("sys.stdout", output)
    app = Mock()
    app.run.side_effect = RuntimeError("original failure")
    monkeypatch.setattr(setup_tui, "SetupApp", lambda: app)
    with pytest.raises(RuntimeError, match="original failure"):
        setup_tui.run_setup_tui()


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_windows_setup_click_flow_persists_config(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SMARTMEMORY_MODE", raising=False)
    monkeypatch.delenv("SMARTMEMORY_FORCE_TUI", raising=False)
    monkeypatch.setattr(
        setup, "check_installation", lambda: Mock(ok=True, socks_support_ok=True)
    )
    # Simulate setup's platform without asking macOS Click to import msvcrt.
    monkeypatch.setattr(
        setup,
        "sys",
        SimpleNamespace(platform="win32", stdin=sys.stdin, stdout=sys.stdout),
    )
    tui = Mock(side_effect=AssertionError("Windows must not launch Textual"))
    monkeypatch.setattr(setup_tui, "run_setup_tui", tui)
    for name in (
        "_ensure_spacy",
        "_ensure_embedding_model",
        "_ensure_lazy_models",
        "_copy_hooks",
        "_copy_skills",
        "_register_hooks",
        "_seed_data_dir",
    ):
        monkeypatch.setattr(setup, name, Mock())
    monkeypatch.setattr("smartmemory_app.daemon.is_running", lambda **kwargs: False)
    start = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(setup, "_start_daemon_local", start)
    metrics = Mock()
    monkeypatch.setattr("smartmemory_app.launch_metrics.emit", metrics)
    key_store = Mock()
    monkeypatch.setattr(config, "set_api_key", key_store)
    auth = Mock(
        return_value=httpx.Response(
            200,
            json={"email": "test@example.test", "default_team_id": "test_windows_team"},
            request=httpx.Request("GET", "https://api.smartmemory.ai/auth/me"),
        )
    )
    monkeypatch.setattr(httpx, "get", auth)
    inputs = "1\nn\nnone\nlocal\nsm\n\n" if mode == "local" else "2\ntest_windows_key\n"
    result = CliRunner().invoke(setup.setup, [], input=inputs)
    assert result.exit_code == 0, result.output
    assert config.load_config().mode == mode
    assert ("All done" if mode == "local" else "Setup complete") in result.output
    tui.assert_not_called()
    metrics.assert_called_once_with("setup.complete", {"mode": mode})
    if mode == "local":
        start.assert_called_once()
        assert config.load_config().llm_provider == "none"
    else:
        start.assert_not_called()
        key_store.assert_called_once_with("test_windows_key")
        assert config.load_config().team_id == "test_windows_team"


@pytest.mark.parametrize("force", [False, True])
def test_first_run_tour_windows_gate(monkeypatch, capsys, force):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("SMARTMEMORY_FORCE_TUI", raising=False)
    if force:
        monkeypatch.setenv("SMARTMEMORY_FORCE_TUI", "1")
    runner = Mock()
    runner.start_daemon_for_app.return_value = None
    factory = Mock(return_value=runner)
    monkeypatch.setattr(tour, "TourSessionRunner", factory)
    app = Mock()
    monkeypatch.setattr(tour, "TourApp", Mock(return_value=app))
    tour.run_tour()
    if force:
        factory.assert_called_once()
        app.run.assert_called_once()
    else:
        factory.assert_not_called()
        assert "disabled on Windows" in capsys.readouterr().out


@pytest.mark.parametrize("force", [False, True])
def test_setup_first_run_explorer_windows_gate(monkeypatch, capsys, caplog, force):
    from smartmemory_app import cli
    from smartmemory_app.tui import app, client

    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.delenv("SMARTMEMORY_FORCE_TUI", raising=False)
    if force:
        monkeypatch.setenv("SMARTMEMORY_FORCE_TUI", "1")
    connection = Mock()
    client_factory = Mock(return_value=connection)
    monkeypatch.setattr(client, "ExploreClient", client_factory)
    explorer = Mock()
    app_factory = Mock(return_value=explorer)
    monkeypatch.setattr(app, "ExploreApp", app_factory)
    with caplog.at_level(logging.DEBUG, logger=cli.__name__):
        cli.explore_cmd.callback(None)
    if force:
        client_factory.assert_called_once()
        app_factory.assert_called_once_with(connection, None)
        explorer.run.assert_called_once()
        connection.close.assert_called_once()
    else:
        client_factory.assert_not_called()
        app_factory.assert_not_called()
        assert "graph explorer TUI is disabled on Windows" in capsys.readouterr().out
        assert "Skipping graph explorer TUI on Windows" in caplog.text
