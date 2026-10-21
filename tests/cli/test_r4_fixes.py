"""R4 capture, shell and restart regressions."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from smartmemory_app import (
    crash_reporter as reporter,
    hook_failures,
    cli as cli_module,
)
from smartmemory_app.report_privacy import private_text


@pytest.mark.parametrize("username", ["li", "log", "admin", "test", "user", "id"])
def test_schema_survives_account_names(monkeypatch, tmp_path, username):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("USER", username)
    monkeypatch.setenv("USERNAME", username)
    with (
        patch("getpass.getuser", return_value=username),
        patch.object(reporter, "_post", return_value=True) as post,
    ):
        result = reporter.report_exception(
            RuntimeError("account " + username), source="install", data_dir=tmp_path
        )
    assert result.status == "sent"
    payload = post.call_args.args[0]
    assert set(payload) == {"api_key", "event", "distinct_id", "properties"}
    props = payload["properties"]
    assert {
        "report_id",
        "log_tail_cli",
        "log_tail_daemon",
        "$exception_list",
    } <= props.keys()
    assert props["$exception_list"][0]["value"] == "account <user>"


@pytest.mark.parametrize(
    "error",
    [
        "certificate is not valid for 'customer-private.internal'",
        "HTTPSConnectionPool(host='customer-private.internal', port=443)",
        "proxy connect alice-private.internal",
    ],
)
def test_known_host_captured_anywhere(monkeypatch, tmp_path, error):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_API_URL", "https://customer-private.internal")
    monkeypatch.setenv("HTTPS_PROXY", "http://alice-private.internal:9000")
    with patch.object(reporter, "_post", return_value=True) as post:
        assert (
            reporter.report_exception(
                RuntimeError(error), source="install", data_dir=tmp_path
            ).status
            == "sent"
        )
    body = json.dumps(post.call_args.args[0])
    assert "customer-private.internal" not in body
    assert "alice-private.internal" not in body


@pytest.mark.parametrize(
    "profile", ["/Users/Other Person", "/home/Other Person", r"C:\Users\Other Person"]
)
def test_profile_root_retains_prose(profile):
    assert (
        private_text(profile + ": FAIL: permission denied. Fix: smartmemory setup")
        == "~: FAIL: permission denied. Fix: smartmemory setup"
    )
    sep = "\\" if ":" in profile else "/"
    assert private_text(profile + sep + "cache") == "~" + sep + "cache"


@pytest.mark.parametrize("hook", sorted(hook_failures.HOOKS))
def test_every_hook_missing_cli_toml_spaces(tmp_path, hook):
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".config/smartmemory"
    cfg.mkdir(parents=True)
    custom = home / "SM Data With Spaces"
    (cfg / "config.toml").write_text(
        "[local]\ndata_dir=" + json.dumps(str(custom)) + "\n"
    )
    env = os.environ.copy()
    env.update(HOME=str(home), USERPROFILE=str(home), PATH="/usr/bin:/bin")
    env.pop("SMARTMEMORY_DATA_DIR", None)
    script = Path(hook_failures.__file__).parent / "hooks" / f"{hook}.sh"
    p = subprocess.run(
        ["bash", str(script)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert p.returncode == 0
    marker = home / ".smartmemory/hook-failures.tsv"
    end = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < end:
        time.sleep(0.01)
    assert marker.exists(), hook
    assert marker.read_text().startswith(hook + "\t127\t")


def test_exit_after_enqueue_restart_delivers_once(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook_failures, "marker_directory", lambda: tmp_path / ".smartmemory"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    driver = tmp_path / "exit.py"
    driver.write_text(
        "import os\nfrom pathlib import Path\nfrom smartmemory_app import crash_reporter as r\nr._post=lambda p: os._exit(17)\nr.report_exception(RuntimeError('pending'), source='hook-shell', data_dir=Path(os.environ['HOME']), dedupe_key='hook:recall:127')\n"
    )
    p = subprocess.run([sys.executable, str(driver)], env=os.environ.copy(), timeout=10)
    assert p.returncode == 17
    from smartmemory_app import report_outbox

    assert report_outbox.queued_count() == 1
    with patch.object(reporter, "_post", return_value=True) as post:
        reporter.flush_outbox()
        reporter.flush_outbox()
    assert post.call_count == 1
    assert report_outbox.queued_count() == 0


def test_optout_discards_outbox(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    with patch.object(reporter, "_post", return_value=False):
        reporter.report_exception(
            RuntimeError("pending"), source="install", data_dir=tmp_path
        )
    from smartmemory_app import report_outbox

    assert report_outbox.queued_count() == 1
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    with patch.object(reporter, "_post") as post:
        reporter.flush_outbox()
    assert not post.called
    assert report_outbox.queued_count() == 0


def test_doctor_does_not_consume_flush_reserve_or_send(monkeypatch, tmp_path):
    marker = hook_failures.marker_directory()
    marker.mkdir(parents=True)
    (marker / "hook-failures.tsv").write_text("recall\t127\t0\n")

    def forbidden(*a, **kw):
        pytest.fail("doctor changed reporting state")

    monkeypatch.setattr(hook_failures, "consume_in_background", forbidden)
    monkeypatch.setattr(reporter, "_post", forbidden)
    result = CliRunner().invoke(cli_module.cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "recall exit 127" in result.output
    assert "Queued reports: 0" in result.output
    assert not (marker / ".hook-failure-state.json").exists()


def test_queued_manual_report_saves_zip(monkeypatch, tmp_path):
    import threading
    from smartmemory_app import report_outbox

    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setattr(
        cli_module.doctor_cmd, "callback", lambda *a: print("Doctor: OK")
    )
    release = threading.Event()
    done = threading.Event()

    def slow(payload):
        try:
            release.wait(5)
            return False
        finally:
            done.set()

    monkeypatch.setattr(reporter, "_post", slow)
    target = tmp_path / "queued.zip"
    try:
        result = CliRunner().invoke(
            cli_module.cli, ["report", "--send", "--yes", "--out", str(target)]
        )
        assert result.exit_code == 0, result.output
        assert "queued" in result.output and str(target) in result.output
        assert target.exists() and report_outbox.queued_count() == 1
    finally:
        release.set()
        assert done.wait(2)


def test_first_run_failure_not_reported_until_ack(monkeypatch, tmp_path):
    from smartmemory_app import install_check, install_troubleshooting, report_outbox

    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setattr(
        install_check,
        "run_native_checks",
        lambda: ["Native usearch: FAIL: DLL. Fix: reinstall"],
    )
    monkeypatch.setattr(install_troubleshooting, "doctor_summary", lambda: [])
    with patch.object(reporter, "_post", return_value=False):
        install_check.first_run_check(tmp_path)
    assert not (tmp_path / ".install-check-failure.json").exists()
    assert (tmp_path / ".install-check-pending.json").exists()
    assert report_outbox.queued_count() == 1
    with patch.object(reporter, "_post", return_value=True) as post:
        reporter.flush_outbox()
        install_check.first_run_check(tmp_path)
    assert post.call_count == 1
    assert (tmp_path / ".install-check-failure.json").exists()
    assert report_outbox.queued_count() == 0


def test_hook_consumer_exit_after_enqueue_recovers_marker(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    markers = home / ".smartmemory"
    markers.mkdir(parents=True)
    monkeypatch.setattr(hook_failures, "marker_directory", lambda: markers)
    (markers / "hook-failures.tsv").write_text("recall\t127\t0\n")
    driver = tmp_path / "interrupted-hook.py"
    driver.write_text(
        "import os\nfrom smartmemory_app import crash_reporter as r, hook_failures as h\nr._post=lambda payload: os._exit(17)\nh.consume_failures(h.marker_directory())\n"
    )
    result = subprocess.run(
        [sys.executable, str(driver)], env=os.environ.copy(), timeout=10
    )
    assert result.returncode == 17
    assert not (markers / ".hook-failure-state.json").exists()
    with patch.object(reporter, "_post", return_value=True) as post:
        hook_failures.consume_failures(markers)
        hook_failures.consume_failures(markers)
    assert post.call_count == 1
    assert (
        json.loads((markers / ".hook-failure-state.json").read_text())["offset"]
        == (markers / "hook-failures.tsv").stat().st_size
    )


def test_outbox_cap_age_and_readonly_count(monkeypatch):
    from smartmemory_app import report_outbox

    root = report_outbox.directory()
    root.mkdir(parents=True)
    payload = {"properties": {"report_id": "test_cap"}}
    for i in range(report_outbox.MAX_REPORTS):
        (root / f"{i}.json").write_text(
            json.dumps({"payload": {"properties": {"report_id": f"test_cap_{i}"}}})
        )
    report_outbox.enqueue(payload, "new", 600)
    assert report_outbox.queued_count() == report_outbox.MAX_REPORTS
    expired = root / "new.json"
    old = time.time() - report_outbox.MAX_AGE - 1
    os.utime(expired, (old, old))
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert report_outbox.queued_count() == report_outbox.MAX_REPORTS
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    sent = []
    report_outbox.flush(lambda p: sent.append(p) or True, lambda: True)
    assert not expired.exists()
    assert len(sent) == report_outbox.MAX_REPORTS - 1


def test_bare_proxy_and_configured_model_hosts(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("HTTPS_PROXY", "bare-private.internal:3128")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm-private.internal/v1")
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-private.internal")
    with patch.object(reporter, "_post", return_value=True) as post:
        assert (
            reporter.report_exception(
                RuntimeError(
                    "SSL bare-private.internal llm-private.internal hf-private.internal"
                ),
                source="install",
                data_dir=tmp_path,
            ).status
            == "sent"
        )
    assert "private.internal" not in json.dumps(post.call_args.args[0])


def test_doctor_preserves_queued_files(monkeypatch):
    from smartmemory_app import report_outbox

    root = report_outbox.directory()
    root.mkdir(parents=True)
    path = root / "test_pending.json"
    path.write_text('{"test": "pending"}')
    before = path.read_bytes()

    def forbidden(*a, **kw):
        pytest.fail("doctor changed reporting state")

    monkeypatch.setattr(reporter, "flush_in_background", forbidden)
    monkeypatch.setattr(report_outbox, "enqueue", forbidden)
    monkeypatch.setattr(reporter, "_post", forbidden)
    result = CliRunner().invoke(cli_module.cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Queued reports: 1" in result.output
    assert path.read_bytes() == before
    assert list(root.iterdir()) == [path]


def test_optout_removes_queue_while_sender_locked(monkeypatch):
    from smartmemory_app import report_outbox
    from filelock import FileLock

    root = report_outbox.directory()
    root.mkdir(parents=True)
    path = root / "test_pending.json"
    path.write_text("{}")
    with FileLock(str(root / ".send.lock")):
        report_outbox.flush(lambda p: pytest.fail("optout sent"), lambda: False)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["handled", "model", "unexpected"])
def test_doctor_errors_never_report(monkeypatch, kind):
    import click
    from smartmemory.errors import MissingModelError
    from smartmemory_app import install_troubleshooting

    errors = {
        "handled": click.ClickException("doctor failed"),
        "model": MissingModelError("doctor model"),
        "unexpected": RuntimeError("doctor failure"),
    }

    def fail(*a):
        raise errors[kind]

    calls = []
    monkeypatch.setattr(cli_module.doctor_cmd, "callback", fail)
    monkeypatch.setattr(install_troubleshooting, "is_install_failure", lambda *a: True)
    monkeypatch.setattr(
        install_troubleshooting,
        "handled_install_failure",
        lambda *a: calls.append("handled"),
    )
    monkeypatch.setattr(
        reporter, "crash_notice", lambda *a: calls.append("crash") or ""
    )
    monkeypatch.setattr(
        reporter, "flush_in_background", lambda *a: calls.append("flush")
    )
    monkeypatch.setattr(
        hook_failures, "consume_in_background", lambda *a: calls.append("consume")
    )
    result = CliRunner().invoke(cli_module.cli, ["doctor"])
    assert result.exit_code == 1
    assert not calls, calls
