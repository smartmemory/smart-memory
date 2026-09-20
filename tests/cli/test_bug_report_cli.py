"""Regression coverage for local CLI bug report formatting."""

from __future__ import annotations

from datetime import datetime, timezone

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


def test_format_bug_report_includes_tracker_fields_and_log_preview(tmp_path):
    environment = bug_report.EnvironmentInfo(
        core_version="1.2.3",
        wrapper_version="4.5.6",
        python_version="3.13.7",
        os_version="TestOS-1",
    )
    log_path = tmp_path / "cli-debug.log"
    log_path.write_text("first detail\nlast detail\n", encoding="utf-8")

    report = bug_report.format_bug_report(
        test_id=None,
        test_title=None,
        message="Search returned the wrong memory",
        severity="high",
        environment=environment,
        log_path=log_path,
        timestamp=datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone.utc),
    )

    assert "Test ID: CLI-20260920T123456Z" in report
    assert "Test title: Ad-hoc CLI report" in report
    assert "Severity: high" in report
    assert "Search returned the wrong memory" in report
    assert (
        "Environment: CLI smartmemory-core=1.2.3 wrapper=4.5.6 TestOS-1 python=3.13.7"
    ) in report
    assert f"Debug log path: {log_path}" in report
    assert "first detail\nlast detail" in report


def test_format_bug_report_handles_missing_and_empty_logs(tmp_path):
    environment = bug_report.EnvironmentInfo("1", "2", "3", "TestOS")
    missing_path = tmp_path / "missing.log"

    missing_report = bug_report.format_bug_report(
        test_id="TC-LITE-305",
        test_title="Search regression",
        message="Nothing happened",
        severity="medium",
        environment=environment,
        log_path=missing_path,
    )

    assert "Debug log preview: unavailable (file not found)" in missing_report

    empty_path = tmp_path / "empty.log"
    empty_path.touch()
    empty_report = bug_report.format_bug_report(
        test_id="TC-LITE-305",
        test_title="Search regression",
        message="Nothing happened",
        severity="medium",
        environment=environment,
        log_path=empty_path,
    )

    assert "Debug log preview: unavailable (file is empty)" in empty_report


def test_format_bug_report_bounds_log_preview(tmp_path):
    log_path = tmp_path / "long.log"
    log_path.write_text(
        "".join(f"preview-{index:03d}\n" for index in range(100)),
        encoding="utf-8",
    )

    report = bug_report.format_bug_report(
        test_id="TC-LITE-305",
        test_title=None,
        message="Search failed",
        severity="medium",
        environment=bug_report.EnvironmentInfo("1", "2", "3", "TestOS"),
        log_path=log_path,
    )

    preview = report.split("Debug log preview:\n", maxsplit=1)[1]
    assert len(preview.splitlines()) == 40
    assert preview.startswith("preview-060\n")
    assert preview.endswith("preview-099")


def test_report_command_prints_local_report_and_manual_paste_guidance(
    monkeypatch, tmp_path
):
    log_path = tmp_path / "cli-debug.log"
    log_path.write_text("debug detail\n", encoding="utf-8")
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: log_path)
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: bug_report.EnvironmentInfo("1.2.3", "4.5.6", "3.13.7", "TestOS"),
    )
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
    assert "Test ID: TC-LITE-305" in result.output
    assert "Test title: Search regression" in result.output
    assert "Severity: high" in result.output
    assert "Search returned the wrong memory" in result.output
    assert "Environment: CLI smartmemory-core=1.2.3 wrapper=4.5.6" in result.output
    assert f"Debug log path: {log_path}" in result.output
    assert "debug detail" in result.output
    assert "Paste this report into the tracker's Bug Report dialog." in result.output
    assert "one-click copy button" in result.output
    assert "attach the log file manually there" in result.output
    assert "submitted" not in result.output.lower()


def test_report_command_accepts_message_without_test_id(monkeypatch, tmp_path):
    missing_log_path = tmp_path / "missing.log"
    monkeypatch.setattr(bug_report, "debug_log_path", lambda: missing_log_path)
    monkeypatch.setattr(
        bug_report,
        "gather_environment",
        lambda: bug_report.EnvironmentInfo("1", "2", "3", "TestOS"),
    )

    result = CliRunner().invoke(
        cli,
        ["report", "The search command failed"],
        env={"SMARTMEMORY_UPDATE_CHECK": "0"},
    )

    assert result.exit_code == 0, result.output
    assert "Test ID: CLI-" in result.output
    assert "Test title: Ad-hoc CLI report" in result.output
    assert "The search command failed" in result.output
    assert f"Debug log path: {missing_log_path}" in result.output
    assert "Debug log preview:" in result.output
