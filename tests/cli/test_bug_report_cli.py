"""Regression coverage for automated CLI bug reporting."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from click.testing import CliRunner

from smartmemory_app import bug_report
from smartmemory_app.cli import cli


def test_read_log_tail_applies_line_and_byte_limits(tmp_path):
    line_limited = tmp_path / "line-limited.log"
    line_limited.write_text(
        "".join(f"line-{index:04d}\n" for index in range(2_050)),
        encoding="utf-8",
    )

    tail = bug_report.read_log_tail(line_limited)

    assert tail is not None
    assert len(tail.splitlines()) == 2_000
    assert tail.startswith("line-0050\n")
    assert tail.endswith("line-2049\n")

    byte_limited = tmp_path / "byte-limited.log"
    byte_limited.write_bytes(b"x" * (600 * 1024) + b"\nlast-line\n")

    byte_tail = bug_report.read_log_tail(byte_limited)

    assert byte_tail is not None
    assert len(byte_tail.encode("utf-8")) <= 512 * 1024
    assert byte_tail.endswith("last-line\n")


def test_payload_has_required_fields_and_synthesizes_test_id():
    environment = bug_report.EnvironmentInfo(
        core_version="1.2.3",
        wrapper_version="4.5.6",
        python_version="3.13.7",
        os_version="TestOS-1",
    )

    payload = bug_report.build_bug_report_payload(
        test_id=None,
        test_title=None,
        message="Search returned the wrong memory",
        severity="high",
        environment=environment,
        timestamp="20260920T123456Z",
    )

    assert payload == {
        "test_id": "CLI-20260920T123456Z",
        "test_title": "Ad-hoc CLI report",
        "actual_outcome": "Search returned the wrong memory",
        "severity": "high",
        "browser_info": (
            "CLI smartmemory-core=1.2.3 wrapper=4.5.6 TestOS-1 python=3.13.7"
        ),
    }


def test_submit_without_debug_log_skips_upload_and_creates_record(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={"data": {"id": "bug-nested-123"}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = bug_report.submit_bug_report(
            test_id=None,
            test_title=None,
            message="Nothing happened",
            severity="medium",
            log_path=tmp_path / "missing.log",
            client=client,
            now=datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone.utc),
        )

    assert len(requests) == 1
    assert requests[0].url == httpx.URL(bug_report._BUG_REPORT_URL)
    assert requests[0].headers["x-app-id"] == bug_report._APP_ID
    assert requests[0].headers["content-type"] == "application/json"
    assert json.loads(requests[0].content)["test_id"] == "CLI-20260920T123456Z"
    assert result.record_id == "bug-nested-123"
    assert result.debug_log_available is False
    assert result.debug_log_attached is False
    assert result.upload_error is None


def test_submit_uploads_multipart_tail_and_uses_nested_file_url(tmp_path):
    log_path = tmp_path / "cli-debug.log"
    log_path.write_text("debug detail\n", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(bug_report._UPLOAD_URL):
            return httpx.Response(
                200,
                json={"data": {"file_url": "https://files.example/debug.log"}},
                request=request,
            )
        return httpx.Response(
            201,
            json={"id": "bug-top-level-456"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = bug_report.submit_bug_report(
            test_id="TC-LITE-305",
            test_title="Search regression",
            message="Search returned the wrong memory",
            severity="critical",
            log_path=log_path,
            client=client,
            now=datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone.utc),
        )

    assert len(requests) == 2
    upload_request, create_request = requests
    assert upload_request.url == httpx.URL(bug_report._UPLOAD_URL)
    assert upload_request.headers["x-app-id"] == bug_report._APP_ID
    assert upload_request.headers["content-type"].startswith("multipart/form-data;")
    assert b'name="file"' in upload_request.content
    assert b'filename="cli-debug-20260920T123456Z.log"' in upload_request.content
    assert b"debug detail" in upload_request.content

    assert json.loads(create_request.content) == {
        "test_id": "TC-LITE-305",
        "test_title": "Search regression",
        "actual_outcome": "Search returned the wrong memory",
        "severity": "critical",
        "browser_info": json.loads(create_request.content)["browser_info"],
        "debug_log_url": "https://files.example/debug.log",
    }
    assert result.record_id == "bug-top-level-456"
    assert result.debug_log_available is True
    assert result.debug_log_attached is True
    assert result.upload_error is None


def test_upload_failure_still_creates_report_without_debug_url(tmp_path):
    log_path = tmp_path / "cli-debug.log"
    log_path.write_text("debug detail\n", encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(bug_report._UPLOAD_URL):
            return httpx.Response(503, request=request)
        return httpx.Response(201, json={"id": "bug-without-log"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = bug_report.submit_bug_report(
            test_id="TC-LITE-305",
            test_title=None,
            message="Search failed",
            severity="medium",
            log_path=log_path,
            client=client,
        )

    assert len(requests) == 2
    assert "debug_log_url" not in json.loads(requests[1].content)
    assert result.record_id == "bug-without-log"
    assert result.debug_log_attached is False
    assert "503" in result.upload_error


def test_report_command_parses_optional_test_id_and_prints_confirmation(
    monkeypatch, tmp_path
):
    submitted = {}

    def fake_submit_bug_report(**kwargs):
        submitted.update(kwargs)
        return bug_report.BugReportResult(
            record_id="bug-cli-789",
            debug_log_attached=True,
            debug_log_available=True,
        )

    monkeypatch.setattr(bug_report, "submit_bug_report", fake_submit_bug_report)
    result = CliRunner().invoke(
        cli,
        [
            "report",
            "TC-LITE-305",
            "Search returned the wrong memory",
            "--severity",
            "high",
            "--test-title",
            "Search regression",
        ],
        env={
            "SMARTMEMORY_DATA_DIR": str(tmp_path),
            "SMARTMEMORY_UPDATE_CHECK": "0",
        },
    )

    assert result.exit_code == 0, result.output
    assert submitted == {
        "test_id": "TC-LITE-305",
        "test_title": "Search regression",
        "message": "Search returned the wrong memory",
        "severity": "high",
    }
    assert "Bug report submitted (id: bug-cli-789)" in result.output
    assert "debug log was attached automatically" in result.output


def test_report_http_failure_is_clean_and_has_manual_fallback(monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_mock_transport)
    monkeypatch.setattr(bug_report, "read_log_tail", lambda path: None)
    result = CliRunner().invoke(
        cli,
        ["report", "The search command failed"],
        env={
            "SMARTMEMORY_DATA_DIR": str(tmp_path),
            "SMARTMEMORY_UPDATE_CHECK": "0",
        },
    )

    assert result.exit_code != 0
    assert "Could not submit bug report" in result.output
    assert "network unavailable" in result.output
    assert "paste your CLI output into the tracker manually" in result.output
    assert "Traceback" not in result.output
