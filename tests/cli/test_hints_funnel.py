"""CLI-HINTS-FUNNEL-1: hints point one way, toward the account."""

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import smartmemory_app
from smartmemory_app import (
    cli as cli_module,
    config,
    hints,
    remote_backend,
    update_check,
)

DAY = 24 * 60 * 60
T0 = 1_800_000_000.0
LOGIN = "Run: sm login"
LOCAL_HINT = (
    "Memory is on this machine only. `sm login` shares it across agents and machines."
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SMARTMEMORY_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(update_check, "update_hint", lambda *_a, **_k: None)
    monkeypatch.setattr(update_check, "upgrade_notices", lambda *_a, **_k: [])
    monkeypatch.setattr(update_check.sys.stdout, "isatty", lambda: True, raising=False)
    return tmp_path


def _local(now):
    return hints.local_mode_hint("local", now=now)


def _notice_lines(mode):
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            config, "load_config", lambda *a, **k: config.SmartMemoryConfig(mode=mode)
        )
        return update_check.trailing_notices("add", argv=["sm", "add"])
    finally:
        monkey.undo()


def _no_key_error():
    with pytest.MonkeyPatch.context() as m:
        m.setattr(remote_backend, "get_api_key", lambda: "")
        backend = remote_backend.RemoteMemory(api_url="https://x.invalid")
        with pytest.raises(remote_backend.RemoteBackendError) as err:
            backend.request("GET", "/memory/x")
    return str(err.value)


def test_hint_sites(home):
    # site | actual | expected
    rows = [
        (
            "not logged in: no key",
            _no_key_error(),
            f"No SmartMemory API key configured. {LOGIN}",
        ),
        ("local, first run", _local(T0), LOCAL_HINT),
        ("local, 1 day later", _local(T0 + DAY), None),
        ("local, 6 days later", _local(T0 + 6 * DAY), None),
        ("local, 7 days later", _local(T0 + 7 * DAY), LOCAL_HINT),
        ("local, right after", _local(T0 + 7 * DAY + 60), None),
        ("remote, logged in", hints.local_mode_hint("remote", now=T0 + 99 * DAY), None),
        ("unconfigured", hints.local_mode_hint(None, now=T0 + 99 * DAY), None),
    ]
    for site, actual, expected in rows:
        assert actual == expected, site


def test_trailing_notice_by_mode(home):
    rows = [
        ("local mode, first command", "local", [LOCAL_HINT]),
        ("local mode, next command", "local", []),
        ("remote, logged in", "remote", []),
        ("unconfigured", None, []),
    ]
    for site, mode, expected in rows:
        assert _notice_lines(mode) == expected, site


def test_login_command_runs_remote_setup(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "smartmemory_app.setup._setup_click", lambda *a, **k: seen.append(a)
    )
    result = CliRunner().invoke(
        cli_module.login_cmd, ["--api-key", "k", "--api-url", "https://x.invalid"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("remote", "k", "https://x.invalid")]


def test_no_hint_string_suggests_local_mode():
    pattern = re.compile(
        r"setup --local|setup --mode local|--mode local|[Uu]se local mode|switch to local",
    )
    root = Path(smartmemory_app.__file__).parent
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(root.glob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line) and path.name != "hints.py"
    ]
    assert hits == []
