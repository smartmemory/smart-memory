"""DIST-UPDATE-HINT-1: update-available hint + first-run / upgrade tour nudge.

No network: every test either patches `_fetch_latest` (the single HTTP call) or
asserts a branch that must never reach it. Every test points the data dir at a
tmp_path, so the real `~/.smartmemory` cache is never read or written.
"""

import json

import pytest
from click.testing import CliRunner

from smartmemory_app import update_check


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(update_check.NO_UPDATE_CHECK_ENV, raising=False)
    return tmp_path


def _cache(tmp_path):
    return tmp_path / update_check.UPDATE_CACHE_FILENAME


def _last_seen(tmp_path):
    return tmp_path / update_check.LAST_SEEN_FILENAME


# ── latest_version: caching, back-off, opt-out ──────────────────────────────


def test_cached_check_is_not_repeated_within_24h(tmp_path, monkeypatch):
    _cache(tmp_path).write_text(json.dumps({"checked_at": 1000.0, "latest": "1.4.87"}))
    calls = []
    monkeypatch.setattr(
        update_check, "_fetch_latest", lambda: calls.append(1) or "9.9.9"
    )

    # 23h59m later — still inside the window.
    assert update_check.latest_version(now=1000.0 + 86_399) == "1.4.87"
    assert calls == []


def test_stale_cache_refetches_and_rewrites_the_cache(tmp_path, monkeypatch):
    _cache(tmp_path).write_text(json.dumps({"checked_at": 1000.0, "latest": "1.4.80"}))
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: "1.4.87")

    now = 1000.0 + 86_401
    assert update_check.latest_version(now=now) == "1.4.87"
    written = json.loads(_cache(tmp_path).read_text())
    assert written == {"checked_at": now, "latest": "1.4.87"}


def test_offline_check_is_silent_and_backs_off_for_a_day(tmp_path, monkeypatch):
    """A failed lookup returns None, prints nothing, and stamps the cache.

    Stamping the failure is the point: without it every command would pay the
    2 s timeout while the machine is offline.
    """
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: None)

    assert update_check.latest_version(now=500.0) is None
    assert update_check.update_hint("1.4.86", now=500.0) is None
    assert json.loads(_cache(tmp_path).read_text()) == {
        "checked_at": 500.0,
        "latest": None,
    }


def test_fetch_latest_returns_none_when_the_http_call_raises(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(update_check, "urlopen", boom)
    assert update_check._fetch_latest() is None


def test_opt_out_env_var_skips_the_network_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv(update_check.NO_UPDATE_CHECK_ENV, "1")
    monkeypatch.setattr(
        update_check,
        "_fetch_latest",
        lambda: pytest.fail("network hit while opted out"),
    )

    assert update_check.is_disabled() is True
    assert update_check.latest_version() is None
    assert update_check.trailing_notices("status", argv=["sm", "status"]) == []
    assert not _cache(tmp_path).exists()


# ── update_hint: the exact printed line ─────────────────────────────────────


def test_update_hint_prints_the_available_line_when_newer(monkeypatch):
    monkeypatch.setattr(update_check, "latest_version", lambda **_: "1.4.87")
    assert (
        update_check.update_hint("1.4.86")
        == "smartmemory 1.4.87 available — pip install -U smartmemory"
    )


@pytest.mark.parametrize("current", ["1.4.87", "1.5.0", "dev", ""])
def test_update_hint_is_silent_when_not_behind(monkeypatch, current):
    monkeypatch.setattr(update_check, "latest_version", lambda **_: "1.4.87")
    assert update_check.update_hint(current) is None


def test_version_lt_tuple_fallback_without_packaging(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_packaging(name, *args, **kwargs):
        if name == "packaging.version":
            raise ImportError("packaging missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_packaging)
    assert update_check.version_lt("1.4.86", "1.4.87") is True
    assert update_check.version_lt("1.4.87", "1.4.86") is False
    assert update_check.version_lt("1.4.86rc1", "1.5.0") is True


# ── upgrade_notices: first run, upgrade, tour change ────────────────────────


def test_first_run_records_versions_and_says_nothing(tmp_path):
    assert update_check.upgrade_notices("1.4.86", lambda: 2) == []
    assert json.loads(_last_seen(tmp_path).read_text()) == {
        "wrapper": "1.4.86",
        "tour": 2,
    }


def test_unchanged_version_says_nothing_and_never_imports_the_tour(tmp_path):
    _last_seen(tmp_path).write_text(json.dumps({"wrapper": "1.4.86", "tour": 2}))

    def never():
        pytest.fail("tour imported on an unchanged version")

    assert update_check.upgrade_notices("1.4.86", never) == []


def test_upgrade_without_tour_change_prints_only_the_updated_line(tmp_path):
    _last_seen(tmp_path).write_text(json.dumps({"wrapper": "1.4.86", "tour": 2}))

    assert update_check.upgrade_notices("1.4.87", lambda: 2) == ["Updated to 1.4.87"]
    assert json.loads(_last_seen(tmp_path).read_text()) == {
        "wrapper": "1.4.87",
        "tour": 2,
    }


def test_upgrade_with_tour_change_appends_the_tour_nudge(tmp_path):
    _last_seen(tmp_path).write_text(json.dumps({"wrapper": "1.4.86", "tour": 1}))

    assert update_check.upgrade_notices("1.4.87", lambda: 2) == [
        "Updated to 1.4.87",
        "Run 'sm tour' to see what's new",
    ]
    assert json.loads(_last_seen(tmp_path).read_text()) == {
        "wrapper": "1.4.87",
        "tour": 2,
    }


def test_updated_line_prints_exactly_once(tmp_path):
    _last_seen(tmp_path).write_text(json.dumps({"wrapper": "1.4.86", "tour": 2}))

    first = update_check.upgrade_notices("1.4.87", lambda: 2)
    second = update_check.upgrade_notices("1.4.87", lambda: 2)
    assert first == ["Updated to 1.4.87"]
    assert second == []


def test_corrupt_last_seen_file_is_treated_as_a_first_run(tmp_path):
    _last_seen(tmp_path).write_text("{not json")
    assert update_check.upgrade_notices("1.4.87", lambda: 2) == []
    assert json.loads(_last_seen(tmp_path).read_text()) == {
        "wrapper": "1.4.87",
        "tour": 2,
    }


# ── suppression: TTY, hook commands, --json ─────────────────────────────────


class _Tty:
    def isatty(self) -> bool:
        return True


class _NotTty:
    def isatty(self) -> bool:
        return False


def test_non_tty_stdout_suppresses_everything(monkeypatch):
    monkeypatch.setattr(update_check.sys, "stdout", _NotTty())
    assert update_check._suppressed("status", ["sm", "status"]) is True
    assert update_check.trailing_notices("status", argv=["sm", "status"]) == []


@pytest.mark.parametrize("command", sorted(update_check.SUPPRESSED_COMMANDS))
def test_hook_commands_that_feed_a_prompt_print_nothing(monkeypatch, command):
    monkeypatch.setattr(update_check.sys, "stdout", _Tty())
    monkeypatch.setattr(
        update_check,
        "_fetch_latest",
        lambda: pytest.fail(f"network hit on sm {command}"),
    )
    assert update_check._suppressed(command, ["sm", command]) is True
    assert update_check.trailing_notices(command, argv=["sm", command]) == []


def test_json_output_is_never_decorated(monkeypatch):
    monkeypatch.setattr(update_check.sys, "stdout", _Tty())
    assert update_check._suppressed("search", ["sm", "search", "q", "--json"]) is True


def test_interactive_command_is_not_suppressed(monkeypatch):
    monkeypatch.setattr(update_check.sys, "stdout", _Tty())
    assert update_check._suppressed("status", ["sm", "status"]) is False


def test_trailing_notices_orders_upgrade_before_update(tmp_path, monkeypatch):
    _last_seen(tmp_path).write_text(json.dumps({"wrapper": "1.4.85", "tour": 1}))
    monkeypatch.setattr(update_check.sys, "stdout", _Tty())
    monkeypatch.setattr(update_check, "_suppressed", lambda *_a, **_k: False)
    monkeypatch.setattr(update_check, "_tour_version", lambda: 2)
    monkeypatch.setattr(update_check, "latest_version", lambda **_: "1.4.99")
    monkeypatch.setattr("smartmemory_app.__version__", "1.4.86", raising=False)

    assert update_check.trailing_notices("status", argv=["sm", "status"]) == [
        "Updated to 1.4.86",
        "Run 'sm tour' to see what's new",
        "smartmemory 1.4.99 available — pip install -U smartmemory",
    ]


def test_trailing_notices_never_raises(monkeypatch):
    monkeypatch.setattr(
        update_check,
        "_suppressed",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert update_check.trailing_notices("status") == []


# ── CLI wiring ─────────────────────────────────────────────────────────────


def test_cli_prints_each_notice_after_the_command(monkeypatch):
    from smartmemory_app.cli import cli

    monkeypatch.setattr(
        update_check,
        "trailing_notices",
        lambda command=None, **_: ["Updated to 1.4.87", "hint line"],
    )
    result = CliRunner().invoke(cli, ["config"])

    assert result.exit_code == 0
    assert result.output.rstrip().endswith("Updated to 1.4.87\nhint line")


def test_cli_passes_the_invoked_subcommand_to_the_notice_gate(monkeypatch):
    from smartmemory_app.cli import cli

    seen = []
    monkeypatch.setattr(
        update_check,
        "trailing_notices",
        lambda command=None, **_: seen.append(command) or [],
    )
    CliRunner().invoke(cli, ["config"])

    assert seen == ["config"]


def test_cli_output_is_unchanged_when_the_notice_path_explodes(monkeypatch):
    from smartmemory_app.cli import cli

    def boom(*_args, **_kwargs):
        raise RuntimeError("hint exploded")

    monkeypatch.setattr(update_check, "trailing_notices", boom)
    result = CliRunner().invoke(cli, ["config"])

    assert result.exit_code == 0
    assert "SmartMemory v" in result.output


# ── `sm setup` first-run line ──────────────────────────────────────────────


def _invoke_setup_tui(monkeypatch, config_file):
    """Run `sm setup` down the TUI branch with a stubbed TUI."""
    from smartmemory_app import setup as setup_mod
    from smartmemory_app.setup import SetupResult, setup

    monkeypatch.setattr(setup_mod, "_can_run_tui", lambda: True)
    monkeypatch.setattr(
        "smartmemory_app.setup_tui.run_setup_tui", lambda: SetupResult(mode="local")
    )
    monkeypatch.setattr("smartmemory_app.config.config_path", lambda: config_file)
    return CliRunner().invoke(setup, [])


def test_setup_ends_with_the_first_run_tour_line(tmp_path, monkeypatch):
    result = _invoke_setup_tui(monkeypatch, tmp_path / "missing" / "config.toml")

    assert result.exit_code == 0
    assert "All done. Run 'sm tour' if this is your first time." in result.output


def test_setup_rerun_does_not_repeat_the_first_run_line(tmp_path, monkeypatch):
    existing = tmp_path / "config.toml"
    existing.write_text("[smartmemory]\nmode = 'local'\n")

    result = _invoke_setup_tui(monkeypatch, existing)

    assert result.exit_code == 0
    assert "Run 'sm tour'" not in result.output
