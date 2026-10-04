"""Tests for the idempotent setup installer."""

import json
import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from unittest.mock import Mock, PropertyMock


def test_register_hooks_idempotent(tmp_path):
    """Running _register_hooks() twice does not duplicate hook entries."""
    settings_file = tmp_path / "settings.json"
    hooks_dest = tmp_path / "hooks"

    with (
        patch("smartmemory_app.setup.SETTINGS", settings_file),
        patch("smartmemory_app.setup.HOOKS_DEST", hooks_dest),
    ):
        from smartmemory_app.setup import _get_hook_registrations, _register_hooks

        _register_hooks()
        _register_hooks()  # second run

        # Build expected entries inside patch context so they use hooks_dest paths.
        expected = _get_hook_registrations()

    cfg = json.loads(settings_file.read_text())
    for event, entry in expected.items():
        event_hooks = cfg["hooks"].get(event, [])
        count = sum(1 for h in event_hooks if h == entry)
        assert count == 1, (
            f"Hook entry for {event} must appear exactly once, got {count}"
        )


def test_copy_skills_no_overwrite(tmp_path):
    """_copy_skills() does not overwrite existing skill files."""
    skills_dest = tmp_path / "skills"
    skills_dest.mkdir()
    skills_src = tmp_path / "skills_src"
    skills_src.mkdir()

    # Write an existing custom skill
    existing_skill = skills_dest / "remember.md"
    existing_skill.write_text("# Custom content that must not be overwritten")

    # Write a new skill in source (different content)
    (skills_src / "remember.md").write_text("# New content from plugin")
    (skills_src / "search.md").write_text("# Search skill")

    with (
        patch("smartmemory_app.setup.SKILLS_SRC", skills_src),
        patch("smartmemory_app.setup.CLAUDE_DIR", tmp_path),
    ):
        from smartmemory_app.setup import _copy_skills

        _copy_skills()

    # Custom content must be preserved
    assert existing_skill.read_text() == "# Custom content that must not be overwritten"
    # New skill must be copied
    assert (skills_dest / "search.md").exists()


def test_seed_data_dir_idempotent(tmp_path, monkeypatch):
    """_seed_data_dir() can be called twice without error or data corruption."""
    monkeypatch.delenv("SMARTMEMORY_DATA_DIR", raising=False)
    with patch("smartmemory_app.setup.DATA_DIR", tmp_path):
        from smartmemory_app.setup import _seed_data_dir

        _seed_data_dir()
        first_content = (tmp_path / "entity_patterns.jsonl").read_text()
        _seed_data_dir()
        second_content = (tmp_path / "entity_patterns.jsonl").read_text()

    assert first_content == second_content, "seed_data_dir must be idempotent"
    assert len(first_content) > 0, "seed file must not be empty"


def test_deregister_hooks_removes_entries(tmp_path):
    """_deregister_hooks() removes registered entries from settings.json."""
    settings_file = tmp_path / "settings.json"
    hooks_dest = tmp_path / "hooks"

    # First register
    with (
        patch("smartmemory_app.setup.SETTINGS", settings_file),
        patch("smartmemory_app.setup.HOOKS_DEST", hooks_dest),
    ):
        from smartmemory_app.setup import _deregister_hooks, _register_hooks

        _register_hooks()
        _deregister_hooks()

    cfg = json.loads(settings_file.read_text())
    for event in ["SessionStart", "Stop", "PostToolUseFailure"]:
        assert cfg["hooks"].get(event, []) == [], (
            f"All SmartMemory entries for {event} must be removed after deregister"
        )


def test_deregister_hooks_preserves_other_hooks(tmp_path):
    """_deregister_hooks() leaves non-SmartMemory hooks in settings.json untouched."""
    settings_file = tmp_path / "settings.json"
    hooks_dest = tmp_path / "hooks"
    other_hook = {"command": "bash", "args": ["/other/hook.sh"]}
    settings_file.write_text(json.dumps({"hooks": {"SessionStart": [other_hook]}}))

    with (
        patch("smartmemory_app.setup.SETTINGS", settings_file),
        patch("smartmemory_app.setup.HOOKS_DEST", hooks_dest),
    ):
        from smartmemory_app.setup import _deregister_hooks, _register_hooks

        _register_hooks()
        _deregister_hooks()

    cfg = json.loads(settings_file.read_text())
    assert other_hook in cfg["hooks"]["SessionStart"], (
        "Non-SmartMemory hooks must survive deregistration"
    )


def test_remove_data_dir_honours_env_var(tmp_path, monkeypatch):
    """_remove_data_dir() removes the SMARTMEMORY_DATA_DIR path, not the default."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    (custom_dir / "memory.db").touch()
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(custom_dir))

    with patch("smartmemory_app.setup.DATA_DIR", tmp_path / "default"):
        from smartmemory_app.setup import _remove_data_dir

        _remove_data_dir()

    assert not custom_dir.exists(), "SMARTMEMORY_DATA_DIR must be removed"


def test_copy_hooks_namespaced_no_clobber(tmp_path):
    """_copy_hooks() writes smartmemory-prefixed files and never touches generic names."""
    hooks_dest = tmp_path / "hooks"
    hooks_dest.mkdir()
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()

    # Existing generic hook owned by another app
    other_app_hook = hooks_dest / "orient.sh"
    other_app_hook.write_text("#!/bin/bash\n# coder-config hook — DO NOT CLOBBER")

    # SmartMemory source hooks
    (hooks_src / "orient.sh").write_text("#!/bin/bash\n# smartmemory orient")
    (hooks_src / "recall.sh").write_text("#!/bin/bash\n# smartmemory recall")

    with (
        patch("smartmemory_app.setup.HOOKS_SRC", hooks_src),
        patch("smartmemory_app.setup.CLAUDE_DIR", tmp_path),
    ):
        from smartmemory_app.setup import _copy_hooks

        _copy_hooks()

    # Generic file must be untouched
    assert (
        other_app_hook.read_text()
        == "#!/bin/bash\n# coder-config hook — DO NOT CLOBBER"
    )
    # Namespaced files must exist
    assert (hooks_dest / "smartmemory-orient.sh").exists()
    assert (hooks_dest / "smartmemory-recall.sh").exists()


def test_copy_hooks_updates_on_upgrade(tmp_path):
    """_copy_hooks() overwrites its own namespaced files (safe for upgrades)."""
    hooks_dest = tmp_path / "hooks"
    hooks_dest.mkdir()
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()

    # Existing SmartMemory hook from v1
    old = hooks_dest / "smartmemory-orient.sh"
    old.write_text("#!/bin/bash\n# v1 — old logic")

    # New version in source
    (hooks_src / "orient.sh").write_text("#!/bin/bash\n# v2 — new logic")

    with (
        patch("smartmemory_app.setup.HOOKS_SRC", hooks_src),
        patch("smartmemory_app.setup.CLAUDE_DIR", tmp_path),
    ):
        from smartmemory_app.setup import _copy_hooks

        _copy_hooks()

    assert old.read_text() == "#!/bin/bash\n# v2 — new logic"


def test_seed_data_dir_honours_env_var(tmp_path, monkeypatch):
    """_seed_data_dir() uses SMARTMEMORY_DATA_DIR when set, not the default DATA_DIR."""
    custom_dir = tmp_path / "custom"
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(custom_dir))

    with patch("smartmemory_app.setup.DATA_DIR", tmp_path / "default"):
        from smartmemory_app.setup import _seed_data_dir

        _seed_data_dir()

    assert (custom_dir / "entity_patterns.jsonl").exists(), (
        "_seed_data_dir must seed into SMARTMEMORY_DATA_DIR, not the default path"
    )
    assert not (tmp_path / "default" / "entity_patterns.jsonl").exists(), (
        "default DATA_DIR must not be seeded when SMARTMEMORY_DATA_DIR is set"
    )


@pytest.mark.parametrize("path", ["tui", "click"])
@pytest.mark.parametrize("provider", ["local", "openai", "ollama"])
def test_setup_downloads_only_local_embedding(
    tmp_path, monkeypatch, capsys, path, provider
):
    from smartmemory_app import setup

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "openai")
    steps = []

    def require(*, allow_download):
        assert allow_download is True
        assert os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] == "local"

    with (
        patch("smartmemory_app.setup._ensure_spacy"),
        patch("smartmemory_app.setup._ensure_lazy_models") as lazy_models,
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.config.save_config"),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch(
            "smartmemory.tools.factory._require_embedding_model", side_effect=require
        ) as download,
        patch("click.confirm", return_value=False),
        patch("click.prompt", side_effect=["none", provider, "sm", str(tmp_path)]),
    ):
        if path == "tui":
            setup._apply_setup_result(
                setup.SetupResult(embedding_provider=provider), on_step=steps.append
            )
        else:
            setup._setup_local()
    lazy_models.assert_called_once_with()
    if provider == "local":
        download.assert_called_once_with(allow_download=True)
        assert "Preparing local embedding model" in capsys.readouterr().out
        if path == "tui":
            assert "Embedding model ready" in steps
    else:
        download.assert_not_called()
        assert "Preparing local embedding model" not in capsys.readouterr().out
    assert os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] == "openai"


def test_embedding_setup_surfaces_core_error_unchanged(monkeypatch):
    import click
    from smartmemory.errors import MissingModelError
    from smartmemory_app.setup import _ensure_embedding_model

    failure = MissingModelError(
        "Embedding model unavailable. Run sm setup after checking network access."
    )
    monkeypatch.delenv("SMARTMEMORY_EMBEDDING_PROVIDER", raising=False)
    with patch(
        "smartmemory.tools.factory._require_embedding_model", side_effect=failure
    ):
        with pytest.raises(click.ClickException) as caught:
            _ensure_embedding_model("local")
    assert str(caught.value) == str(failure)
    assert "SMARTMEMORY_EMBEDDING_PROVIDER" not in os.environ


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    """Exercise setup dispatch/startup without models or real service managers."""
    from smartmemory_app import daemon, setup

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(setup, "_can_run_tui", lambda: False)
    monkeypatch.setattr(setup, "_setup_local", Mock(return_value=True))
    monkeypatch.setattr(setup, "_install_launchd_plist", Mock(return_value=True))
    monkeypatch.setattr(daemon, "_upgrade_worker_agent", Mock())
    monkeypatch.setattr(daemon, "_retire_legacy_workers", Mock(return_value=False))
    start = Mock(return_value={"status": "ok"})
    start.real_start = daemon.start_daemon
    monkeypatch.setattr(daemon, "start_daemon", start)
    metrics = Mock()
    monkeypatch.setattr("smartmemory_app.launch_metrics.emit", metrics)
    return setup, start, metrics, tmp_path / "data" / "daemon.log"


@pytest.mark.parametrize("flags", [True, False], ids=["flags", "prompts"])
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("startup timed out"),
        None,
        {"status": "error"},
        {"status": "degraded", "degraded_reason": "missing model"},
    ],
    ids=["exception", "no-response", "error", "degraded"],
)
def test_setup_exits_nonzero_when_daemon_is_not_ready(setup_runtime, flags, failure):
    setup, start, metrics, log_path = setup_runtime
    if isinstance(failure, Exception):
        start.side_effect = failure
    else:
        start.return_value = failure

    result = CliRunner().invoke(
        setup.setup, ["--mode", "local"] if flags else [], input="1\n"
    )

    assert result.exit_code == 1, result.output
    assert "daemon" in result.output.lower()
    assert str(log_path) in result.output
    assert "sm doctor" in result.output
    assert "sm start --wait" in result.output
    if isinstance(failure, Exception):
        assert str(failure) in result.output
    if failure and isinstance(failure, dict) and failure.get("degraded_reason"):
        assert failure["degraded_reason"] in result.output
    assert "All done" not in result.output
    assert "SmartMemory is ready" not in result.output
    metrics.assert_not_called()
    start.assert_called_once()


@pytest.mark.parametrize("first_run", [True, False])
def test_setup_success_still_exits_zero(setup_runtime, first_run):
    setup, start, metrics, _ = setup_runtime
    setup._setup_local.return_value = first_run
    result = CliRunner().invoke(setup.setup, ["--mode", "local"])
    assert result.exit_code == 0, result.output
    assert "SmartMemory is running" in result.output
    metrics.assert_called_once_with("setup.complete", {"mode": "local"})
    start.assert_called_once()


def test_setup_remote_and_cancel_do_not_start_daemon(setup_runtime, monkeypatch):
    setup, start, metrics, _ = setup_runtime
    remote = Mock()
    monkeypatch.setattr(setup, "_setup_remote", remote)
    result = CliRunner().invoke(setup.setup, ["--mode", "remote"])
    assert result.exit_code == 0, result.output
    remote.assert_called_once_with(None, None)
    metrics.assert_called_once_with("setup.complete", {"mode": "remote"})

    metrics.reset_mock()
    monkeypatch.setattr(setup, "_can_run_tui", lambda: True)
    monkeypatch.setattr("smartmemory_app.setup_tui.run_setup_tui", lambda: None)
    result = CliRunner().invoke(setup.setup, [])
    assert result.exit_code == 0, result.output
    assert "Setup cancelled" in result.output
    start.assert_not_called()
    metrics.assert_not_called()


@pytest.mark.parametrize("outcome", ["failed", "ready", "warming"])
def test_setup_tui_propagates_startup_result(setup_runtime, monkeypatch, outcome):
    from smartmemory_app.setup_tui import ProgressScreen

    setup, start, metrics, log_path = setup_runtime
    if outcome == "failed":
        start.side_effect = RuntimeError("startup timed out")
    elif outcome == "warming":
        start.return_value = {"status": "warming"}
    screen = ProgressScreen()
    widget = Mock()

    class AppStub:
        _result = setup.SetupResult()
        _setup_error = None

        def call_from_thread(self, callback, *args):
            callback(*args)

        def exit(self, result):
            self.result = result

        def run(self):
            ProgressScreen._run_setup.__wrapped__(screen)
            screen.on_key()
            return self.result

    app = AppStub()
    fallback = Mock()
    monkeypatch.setattr(setup, "_can_run_tui", lambda: True)
    monkeypatch.setattr(setup, "_setup_click", fallback)
    monkeypatch.setattr(setup, "_apply_setup_result", Mock())
    monkeypatch.setattr("smartmemory_app.setup_tui.SetupApp", lambda: app)
    with (
        patch.object(
            ProgressScreen, "app", new_callable=PropertyMock, return_value=app
        ),
        patch.object(screen, "query_one", return_value=widget),
        patch.object(screen, "_begin_daemon_wait"),
        patch.object(screen, "_finish_daemon_wait"),
        patch.object(screen, "_show_daemon_log"),
    ):
        result = CliRunner().invoke(setup.setup, [])

    assert result.exit_code == (1 if outcome == "failed" else 0), result.output
    fallback.assert_not_called()
    start.assert_called_once()
    if outcome == "failed":
        assert str(log_path) in result.output
        assert "sm doctor" in result.output
        assert "sm start --wait" in result.output
        assert "All done" not in result.output
        metrics.assert_not_called()
    elif outcome == "warming":
        assert "still warming" in result.output
        assert "once warmup finishes" in result.output
        assert "sm status" in result.output
        assert "sm start --wait" in result.output
        assert "All done" not in result.output
        assert "SmartMemory is ready" not in result.output
        assert any(
            "once warmup finishes" in call.args[0]
            for call in widget.update.call_args_list
        )
        metrics.assert_called_once_with(
            "setup.complete", {"mode": "local", "warming": True}
        )
    else:
        metrics.assert_called_once_with("setup.complete", {"mode": "local"})


@pytest.mark.parametrize("flags", [True, False], ids=["flags", "prompts"])
def test_setup_warns_when_daemon_is_still_warming(setup_runtime, flags):
    setup, start, metrics, _ = setup_runtime
    start.return_value = {"status": "warming"}
    result = CliRunner().invoke(
        setup.setup, ["--mode", "local"] if flags else [], input="1\n"
    )
    assert result.exit_code == 0, result.output
    assert "still warming" in result.output
    assert "once warmup finishes" in result.output
    assert "sm status" in result.output
    assert "sm start --wait" in result.output
    assert "SmartMemory is ready" not in result.output
    assert "All done" not in result.output
    metrics.assert_called_once_with(
        "setup.complete", {"mode": "local", "warming": True}
    )


@pytest.mark.parametrize("path", ["existing", "launchd", "subprocess"])
@pytest.mark.parametrize(
    "outcome",
    ["warming", "ready", "no-response", "lost-response", "degraded", "error", "crash"],
)
def test_setup_real_readiness_loop(setup_runtime, monkeypatch, tmp_path, path, outcome):
    """Run actual readiness polling with a virtual clock and fake OS boundaries."""
    from smartmemory_app import daemon

    setup, start, metrics, _ = setup_runtime
    monkeypatch.setattr(daemon, "start_daemon", start.real_start)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: path == "launchd")
    monkeypatch.setattr(daemon, "_launchd_plist_path", lambda label: tmp_path / label)
    monkeypatch.setattr(daemon, "_launchd_job_summary", lambda label: [])
    workers = Mock()
    monkeypatch.setattr(daemon, "_start_workers", workers)
    elapsed = [0.0]
    health_calls = [0]

    def sleep(seconds):
        elapsed[0] += seconds

    def health():
        health_calls[0] += 1
        if path == "existing" and health_calls[0] == 1 and outcome == "no-response":
            return {"status": "warming"}
        if path != "existing" and health_calls[0] == 1:
            return None
        if outcome == "no-response" or (
            outcome == "lost-response" and elapsed[0] >= 59
        ):
            return None
        if outcome == "crash" and elapsed[0] >= 1:
            raise RuntimeError("health connection failed")
        if outcome == "degraded":
            return {"status": "degraded", "degraded_reason": "missing model"}
        if outcome == "error":
            return {"status": "error"}
        return {
            "status": "ok"
            if outcome == "ready" and elapsed[0] >= 2 or elapsed[0] >= 75
            else "warming"
        }

    monkeypatch.setattr(daemon, "get_status", health)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    proc = Mock(returncode=1)
    proc.poll.side_effect = (
        lambda: 1 if outcome == "crash" and elapsed[0] >= 1 else None
    )

    def popen(*args, **kwargs):
        kwargs["stdout"].close()
        return proc

    socket = Mock()
    socket.connect_ex.return_value = 0
    # No real daemon processes, ports, or service manager calls in these tests.
    with (
        patch(
            "subprocess.Popen",
            side_effect=popen
            if path == "subprocess"
            else AssertionError("unexpected launch"),
        ),
        patch(
            "subprocess.run",
            side_effect=AssertionError("unexpected service manager call"),
        ),
        patch("socket.socket", return_value=socket),
    ):
        result = CliRunner().invoke(setup.setup, ["--mode", "local"])

    if outcome == "warming":
        assert result.exit_code == 0, result.output
        assert elapsed[0] == 60
        workers.assert_not_called()
        assert health()["status"] == "warming"
        proc.terminate.assert_not_called()
        assert "still warming" in result.output
        assert "once warmup finishes" in result.output
        assert "sm status" in result.output
        assert "sm start --wait" in result.output
        assert "SmartMemory is ready" not in result.output
        assert "All done" not in result.output
        metrics.assert_called_once_with(
            "setup.complete", {"mode": "local", "warming": True}
        )
        elapsed[0] = 75
        assert health()["status"] == "ok"
    elif outcome == "ready":
        assert result.exit_code == 0, result.output
        assert "SmartMemory is running" in result.output
        metrics.assert_called_once_with("setup.complete", {"mode": "local"})
        proc.terminate.assert_not_called()
    else:
        assert result.exit_code == 1, result.output
        assert "sm doctor" in result.output
        metrics.assert_not_called()


def test_start_wait_strict_timeout_restores_head_termination(
    setup_runtime, monkeypatch
):
    """The setup-only warming policy never changes strict sm start --wait."""
    from smartmemory_app import daemon
    from smartmemory_app.cli import cli

    _, start, _, _ = setup_runtime
    monkeypatch.setattr(daemon, "start_daemon", start.real_start)
    checks = [0]

    def health():
        checks[0] += 1
        return None if checks[0] <= 2 else {"status": "warming"}

    monkeypatch.setattr(daemon, "get_status", health)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: False)
    sleep = Mock()
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    workers = Mock()
    monkeypatch.setattr(daemon, "_start_workers", workers)
    proc = Mock()
    proc.poll.return_value = None

    def popen(*args, **kwargs):
        kwargs["stdout"].close()
        return proc

    socket = Mock()
    socket.connect_ex.return_value = 0
    with (
        patch("smartmemory_app.cli._configure_cli_logging"),
        patch("subprocess.Popen", side_effect=popen) as spawn,
        patch("socket.socket", return_value=socket),
    ):
        result = CliRunner().invoke(cli, ["start", "--wait"])

    assert result.exit_code == 1, result.output
    assert "SmartMemory is ready" not in result.output
    spawn.assert_called_once()
    assert sleep.call_count == 120
    proc.terminate.assert_called_once()
    workers.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [["start", "--wait"], ["setup", "--mode", "local"]],
    ids=["start", "setup"],
)
def test_real_warmup_crash_does_not_start_an_orphan_worker(
    setup_runtime, monkeypatch, command
):
    from smartmemory_app import daemon
    from smartmemory_app.cli import cli

    _, start, _, _ = setup_runtime
    monkeypatch.setattr(daemon, "start_daemon", start.real_start)
    monkeypatch.setattr(daemon, "_launchd_manages_daemon", lambda: False)
    elapsed = [0.0]
    started = [False]
    commands = []

    def health():
        return {"status": "warming"} if started[0] else None

    def sleep(seconds):
        elapsed[0] += seconds

    def spawn(args, **kwargs):
        kwargs["stdout"].close()
        commands.append(args)
        if "smartmemory_app.worker_entry" not in args:
            started[0] = True
        proc = Mock(returncode=42)
        proc.poll.side_effect = lambda: 42 if elapsed[0] >= 1 else None
        return proc

    monkeypatch.setattr(daemon, "get_status", health)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    socket = Mock()
    socket.connect_ex.return_value = 0
    with (
        patch("smartmemory_app.cli._configure_cli_logging"),
        patch("subprocess.Popen", side_effect=spawn),
        patch("socket.socket", return_value=socket),
    ):
        result = CliRunner().invoke(cli, command)
    assert result.exit_code == 1, result.output
    assert elapsed[0] == 1
    assert len(commands) == 1
    assert "smartmemory_app.worker_entry" not in commands[0]


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_existing_daemon_returns_never_spawn_workers_before_a_child_lock(
    setup_runtime, monkeypatch, mode
):
    """Keep HEAD fast paths; no artificial synchronous child lock masks a race."""
    from smartmemory_app import daemon
    from smartmemory_app.cli import cli
    from smartmemory_app.config import SmartMemoryConfig, save_config

    _, start, _, log_path = setup_runtime
    save_config(SmartMemoryConfig(mode=mode))
    log_path.parent.mkdir(parents=True)
    (log_path.parent / ".worker.lock").touch()
    (log_path.parent / ".worker.pid").write_text("123456")
    monkeypatch.setattr(daemon, "start_daemon", start.real_start)
    monkeypatch.setattr(daemon, "get_status", lambda: {"status": "ok", "mode": mode})
    with (
        patch("smartmemory_app.cli._configure_cli_logging"),
        patch("subprocess.Popen") as spawn,
    ):
        # No child acquires a worker lock during these repeated calls.
        assert daemon.start_daemon()["status"] == "ok"
        assert daemon.start_daemon()["status"] == "ok"
        for args in (["start"], ["start", "--wait"]):
            result = CliRunner().invoke(cli, args)
            assert result.exit_code == 0, result.output
            assert "already running" in result.output
    spawn.assert_not_called()


def test_remote_setup_and_start_real_existing_readiness_spawn_no_local_workers(
    setup_runtime, monkeypatch
):
    from smartmemory_app import daemon
    from smartmemory_app.cli import cli
    from smartmemory_app.config import SmartMemoryConfig, save_config

    setup, start, _, _ = setup_runtime
    save_config(SmartMemoryConfig(mode="remote"))
    monkeypatch.setattr(daemon, "start_daemon", start.real_start)
    monkeypatch.setattr(setup, "_setup_remote", Mock())
    elapsed = [0.0]

    def health():
        return {"status": "warming" if elapsed[0] < 2 else "ok", "mode": "remote"}

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(daemon, "get_status", health)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with (
        patch("smartmemory_app.cli._configure_cli_logging"),
        patch("subprocess.Popen") as spawn,
        patch("subprocess.run", side_effect=AssertionError("no service manager calls")),
    ):
        runner = CliRunner()
        configured = runner.invoke(cli, ["setup", "--mode", "remote"])
        assert configured.exit_code == 0, configured.output
        warming = runner.invoke(cli, ["start"])
        assert warming.exit_code == 0, warming.output
        ready = runner.invoke(cli, ["start", "--wait"])
        assert ready.exit_code == 0, ready.output
        assert elapsed[0] == 2
    spawn.assert_not_called()
