"""Offline reporter flows with a real HTTP client and mocked capture transport."""

import json
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app import (
    cli as cli_module,
    config,
    crash_reporter as reporter,
    viewer_server,
)
from smartmemory_app.report_privacy import private_text, safe_log_text
from smartmemory_app.runtime_diagnostics import install_daemon_diagnostics

KEY = "gsk_" + "PlantedReporterKey" * 3
MEMORY = "My private memory sentence should never leave this machine."


@pytest.fixture
def capture(monkeypatch, tmp_path):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_UPDATE_CHECK", "0")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_HOST", "https://capture.test")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_KEY", "phc_test_public")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    rows = []
    original = httpx.Client
    done = threading.Event()
    state = {"rows": rows, "status": 200, "done": done, "options": []}

    def handler(request):
        rows.append(json.loads(request.content))
        assert str(request.url) == "https://capture.test/i/v0/e/"
        done.set()
        return httpx.Response(state["status"], request=request)

    def client(*args, **kwargs):
        state["options"].append(kwargs.copy())
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return state


def chained_error():
    # Compile a real traceback in the wrapper namespace and a home-directory path.
    namespace = {"__name__": "smartmemory_app.test_reporter", "key": KEY}
    code = compile(
        "def fail():\n"
        "    try:\n"
        "        raise ValueError('root failure ' + key)\n"
        "    except ValueError as cause:\n"
        "        raise RuntimeError('outer failure') from cause\n"
        "fail()\n",
        str(Path.home() / "smartmemory_app" / "probe.py"),
        "exec",
    )
    try:
        exec(code, namespace)
    except RuntimeError as exc:
        return exc


def test_exception_payload_chain_privacy_and_persisted_identity(capture, tmp_path):
    for name in ("cli-debug.log", "daemon.log"):
        (tmp_path / name).write_text(
            "INFO 你好 " * 10_000
            + "\n"
            + f"INFO safe diagnostic {KEY} {Path.home()}/models\n"
            f"DEBUG daemon request: kwargs={{'content': '{MEMORY}'}}\n"
            f"{MEMORY}\n"
            f"INFO safe again\nDEBUG daemon response: body={MEMORY}\n"
            f"INFO content={MEMORY}\n"
        )
    result = reporter.report_exception(
        chained_error(), source="cli", args=["add", MEMORY, "--api-key", KEY]
    )
    assert result.status == "sent"
    payload = capture["rows"][0]
    assert payload["event"] == "$exception"
    props = payload["properties"]
    assert props["report_id"] == result.report_id
    assert props["$process_person_profile"] is False
    assert props["command"] == "add --api-key"
    assert props["source"] == "cli"
    assert {
        "smartmemory_version",
        "smartmemory_core_version",
        "python_version",
        "os",
    } <= props.keys()
    exceptions = props["$exception_list"]
    assert [item["type"] for item in exceptions] == ["RuntimeError", "ValueError"]
    assert exceptions[0]["mechanism"] == {"handled": False, "synthetic": False}
    frame = exceptions[0]["stacktrace"]["frames"][-1]
    assert frame == {
        "filename": "probe.py",
        "abs_path": "~/smartmemory_app/probe.py",
        "function": "fail",
        "lineno": 5,
        "in_app": True,
        "platform": "python",
    }
    assert exceptions[0]["stacktrace"]["type"] == "raw"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert KEY not in rendered and MEMORY not in rendered
    assert str(Path.home()) not in rendered and "<redacted>" in rendered
    assert len(props["log_tail_cli"].encode()) <= 30_000
    assert len(props["log_tail_daemon"].encode()) <= 30_000
    assert (
        capture["options"][0]["timeout"] == 5
        and capture["options"][0]["trust_env"] is True
    )
    persisted = (tmp_path / ".install-id").read_text()
    assert str(uuid.UUID(persisted)) == payload["distinct_id"]
    second = reporter.report_exception(TypeError("different failure"), source="daemon")
    assert second.status == "sent"
    assert capture["rows"][1]["distinct_id"] == persisted


def test_body_filter_and_home_paths():
    value = f"INFO safe\nDEBUG content:\n{MEMORY}\n  {MEMORY}\nINFO recovered {KEY}"
    text = safe_log_text(value)
    assert MEMORY not in text and "content" not in text
    assert "recovered" in text and KEY not in text
    assert private_text(str(Path.home() / "models")) == "~/models"


def test_dedupe_persistence_expiry_and_daily_cap(capture, tmp_path, monkeypatch):
    now = [time.time()]
    from smartmemory_app import report_outbox

    monkeypatch.setattr(report_outbox.time, "time", lambda: now[0])
    error = chained_error()
    assert reporter.report_exception(error, source="cli").status == "sent"
    assert reporter.report_exception(error, source="daemon").status == "suppressed"
    state = json.loads((report_outbox.directory() / ".reservations").read_text())
    assert state["count"] == 1 and len(state["recent"]) == 1
    now[0] += 601
    assert reporter.report_exception(error, source="cli").status == "sent"
    for index in range(18):
        unique = type(f"UniqueFailure{index}", (Exception,), {})("test")
        assert reporter.report_exception(unique, source="daemon").status == "sent"
    assert (
        reporter.report_exception(LookupError("cap"), source="daemon").status
        == "suppressed"
    )
    assert len(capture["rows"]) == 20
    now[0] += 86_400
    assert (
        reporter.report_exception(LookupError("new day"), source="daemon").status
        == "sent"
    )
    assert (
        json.loads((report_outbox.directory() / ".reservations").read_text())["count"]
        == 1
    )


@pytest.mark.parametrize("kind", ["env", "config"])
def test_opt_out_never_sends(capture, tmp_path, monkeypatch, kind):
    if kind == "env":
        monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    else:
        config.save_config(config.SmartMemoryConfig(mode="local", crash_reports=False))
        assert config.load_config().crash_reports is False
    assert (
        reporter.report_exception(RuntimeError("failure"), source="cli").status
        == "disabled"
    )
    assert not capture["rows"]
    assert not (tmp_path / ".install-id").exists()


def test_cli_crash_prints_id(capture, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("crash " + KEY)

    monkeypatch.setattr(cli_module.cli.commands["start"], "callback", fail)
    result = CliRunner().invoke(
        cli_module.cli, ["start"], env={"SMARTMEMORY_DEBUG": "0"}
    )
    assert result.exit_code == 1
    assert (
        f"Crash report sent (ID {capture['rows'][0]['properties']['report_id']})."
        in result.output
    )
    assert "Disable: SMARTMEMORY_CRASH_REPORTS=0" in result.output
    assert KEY not in result.output


def test_failed_cli_send_discloses_recovery(capture, monkeypatch):
    capture["status"] = 503

    def fail(**kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(cli_module.cli.commands["start"], "callback", fail)
    result = CliRunner().invoke(cli_module.cli, ["start"])
    assert result.exit_code == 1
    assert (
        "Crash report queued for the next CLI or daemon start. Run smartmemory report --zip"
        in result.output
    )


def test_asgi_failure_reports_and_preserves_500(capture):
    app = viewer_server._build_app()

    @app.get("/test-report-failure")
    def failure():
        from smartmemory.errors import VectorWriteError

        raise VectorWriteError("test_report_item", "test vector write failure")

    # The viewer mounts a catch-all static route, so put this test route before it.
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-report-failure")
        assert response.status_code == 500 and response.text == "Internal Server Error"
        assert capture["done"].wait(3)
    props = capture["rows"][0]["properties"]
    assert props["source"] == "asgi"
    assert props["$exception_list"][0]["type"] == "VectorWriteError"
    assert props["$exception_list"][0]["mechanism"]["handled"] is False


def test_daemon_process_thread_hooks(capture, tmp_path):
    install_daemon_diagnostics(tmp_path)
    error = ValueError("process failure")
    sys.excepthook(type(error), error, None)
    assert capture["done"].wait(3)
    capture["done"].clear()
    error = TypeError("thread failure")
    threading.excepthook(
        SimpleNamespace(exc_type=type(error), exc_value=error, exc_traceback=None)
    )
    assert capture["done"].wait(3)
    assert [row["properties"]["source"] for row in capture["rows"]] == [
        "daemon",
        "daemon_thread",
    ]


def test_warmup_failure_is_reported(capture, monkeypatch):
    for name in ("_startup_status", "_startup_reason", "_last_warmup_failure"):
        monkeypatch.setattr(viewer_server, name, None)

    def fail():
        raise RuntimeError("warmup failed")

    monkeypatch.setattr(viewer_server, "_warm_backend", fail)
    thread = viewer_server._start_background_warmup()
    thread.join(3)
    assert capture["done"].wait(3)
    assert viewer_server._get_startup_state()[0] == "degraded"
    props = capture["rows"][0]["properties"]
    assert (
        props["source"] == "daemon"
        and props["$exception_list"][0]["mechanism"]["handled"] is True
    )


@pytest.mark.parametrize("status", [200, 503])
def test_manual_send_and_failed_send_same_bundle(
    capture, monkeypatch, tmp_path, status
):
    capture["status"] = status
    monkeypatch.setattr(
        cli_module.doctor_cmd, "callback", lambda *args: print("Doctor summary " + KEY)
    )
    (tmp_path / "daemon.log").write_text(f"INFO content={MEMORY}\nINFO safe {KEY}\n")
    target = tmp_path / "fallback.zip"
    result = CliRunner().invoke(
        cli_module.cli,
        ["report", "Support message " + KEY, "--send", "--yes", "--out", str(target)],
    )
    assert result.exit_code == 0, result.output
    payload = capture["rows"][0]
    props = payload["properties"]
    assert (
        payload["event"] == "smartmemory_support_report" and props["source"] == "manual"
    )
    assert "Doctor summary" in props["doctor"] and "Support message" in props["message"]
    assert KEY not in json.dumps(payload) and MEMORY not in json.dumps(payload)
    assert "api_key" not in props["config"]
    if status == 200:
        assert (
            f"Sent. Report ID: {props['report_id']}. Quote this ID to support@smartmemory.ai."
            in result.output
        )
        assert not target.exists()
    else:
        assert str(target) in result.output and "Email this file" in result.output
        with zipfile.ZipFile(target) as archive:
            assert archive.read("doctor.txt").decode() == props["doctor"]
            assert archive.read("message.txt").decode() == props["message"]
            assert archive.read("daemon.log").decode() == props["log_tail_daemon"]


def test_manual_send_declined_has_no_capture(capture, monkeypatch):
    monkeypatch.setattr(
        cli_module.doctor_cmd, "callback", lambda *args: print("Doctor")
    )
    result = CliRunner().invoke(cli_module.cli, ["report", "--send"], input="n\n")
    assert result.exit_code == 0
    assert "Send this to SmartMemory support? [y/N]" in result.output
    assert "Cancelled. Nothing sent." in result.output
    assert not capture["rows"]


def test_transport_failure_never_raises(capture, monkeypatch, caplog):
    def failing_post(self, *args, **kwargs):
        raise httpx.ConnectError("failure " + KEY)

    # Replace only the real client's send method, retaining the mocked transport boundary.
    monkeypatch.setattr(httpx._client.Client, "post", failing_post)
    result = reporter.report_exception(RuntimeError("failure"), source="daemon")
    assert result.status == "queued" and "could not be sent" in caplog.text
    assert KEY not in caplog.text


def test_wall_clock_send_budget(capture, monkeypatch):
    from smartmemory_app.report_outbox import queued_count

    release = threading.Event()
    finished = threading.Event()

    def slow_post(self, *args, **kwargs):
        try:
            release.wait(2)
            raise httpx.ConnectError("test released")
        finally:
            finished.set()

    monkeypatch.setattr(httpx._client.Client, "post", slow_post)
    try:
        start = time.monotonic()
        result = reporter.report_exception(RuntimeError("slow failure"), source="cli")
        assert result.status == "queued" and time.monotonic() - start < 0.5
        assert queued_count() == 1
    finally:
        release.set()
        assert finished.wait(2)


def test_first_run_discloses_automatic_reporting(capture, monkeypatch):
    from smartmemory_app import setup

    monkeypatch.setattr(setup, "_setup_click", lambda *args: None)
    monkeypatch.setattr(
        setup,
        "check_installation",
        lambda: SimpleNamespace(ok=True, socks_support_ok=True),
    )
    first = CliRunner().invoke(setup.setup, ["--mode", "local"])
    assert first.exit_code == 0
    assert (
        "Automatic anonymous crash reports are on. Disable: SMARTMEMORY_CRASH_REPORTS=0"
        in first.output
    )
    config.save_config(config.SmartMemoryConfig(mode="local"))
    again = CliRunner().invoke(setup.setup, ["--mode", "local"])
    assert (
        again.exit_code == 0 and "Automatic anonymous crash reports" not in again.output
    )
    assert not capture["rows"]


def test_manual_send_respects_optout_and_saves_bundle(capture, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    monkeypatch.setattr(
        cli_module.doctor_cmd, "callback", lambda *args: print("Doctor")
    )
    result = CliRunner().invoke(cli_module.cli, ["report", "--send", "--yes"])
    assert result.exit_code == 0 and "Support zip:" in result.output
    assert not capture["rows"]


def test_personal_key_is_rejected_without_capture(capture, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_KEY", "phx_personal_key")
    assert (
        reporter.report_exception(RuntimeError("failure"), source="cli").status
        == "failed"
    )
    assert not capture["rows"]


def test_cli_input_reflected_in_errors_and_logs_is_omitted(capture, tmp_path):
    (tmp_path / "cli-debug.log").write_text(f"INFO failed to write {MEMORY}\n")
    result = reporter.report_exception(
        RuntimeError(f"failed to write {MEMORY}"), source="cli", args=["add", MEMORY]
    )
    assert result.status == "sent"
    assert MEMORY not in json.dumps(capture["rows"][0])
    assert (
        "<omitted>" in capture["rows"][0]["properties"]["$exception_list"][0]["value"]
    )


def test_byte_truncated_memory_body_is_not_uploaded(capture, tmp_path):
    for name in ("cli-debug.log", "daemon.log"):
        (tmp_path / name).write_text(
            "DEBUG daemon request: kwargs=" + MEMORY * 2000 + "\nINFO recovered\n"
        )
    assert (
        reporter.report_exception(RuntimeError("failure"), source="daemon").status
        == "sent"
    )
    props = capture["rows"][0]["properties"]
    assert props["log_tail_cli"] == "INFO recovered"
    assert props["log_tail_daemon"] == "INFO recovered"
    assert MEMORY not in json.dumps(capture["rows"][0])


def test_report_event_is_minimal_bounded_and_once_per_day(capture, tmp_path):
    """HOOK-DEADLINE: non-exception events carry no log tails and dedupe per install."""
    (tmp_path / "cli-debug.log").write_text(MEMORY, encoding="utf-8")
    props = {
        "command": "lifecycle recall",
        "phase": "engine",
        "elapsed_s": 8.01,
        "deadline_s": 8.0,
        "phase_timings": {"payload": 0.01, "engine": 8.0},
    }
    first = reporter.report_event(
        "hook_deadline_exceeded", props, dedupe_key="hook_deadline_exceeded", wait=2
    )
    assert first.status == "sent"
    second = reporter.report_event(
        "hook_deadline_exceeded", props, dedupe_key="hook_deadline_exceeded", wait=2
    )
    assert second.status == "suppressed"
    assert len(capture["rows"]) == 1
    row = capture["rows"][0]
    assert row["event"] == "hook_deadline_exceeded"
    sent = row["properties"]
    assert sent["command"] == "lifecycle recall"
    assert sent["phase_timings"] == {"payload": 0.01, "engine": 8.0}
    assert sent["$process_person_profile"] is False
    assert {"os", "smartmemory_version", "python_version"} <= set(sent)
    assert not {"log_tail_cli", "log_tail_daemon", "$exception_list"} & set(sent)
    assert MEMORY not in json.dumps(row)


def test_report_event_respects_opt_out(capture, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    result = reporter.report_event("hook_deadline_exceeded", {}, dedupe_key="x")
    assert result.status == "disabled"
    assert capture["rows"] == []
