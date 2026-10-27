"""CLI-CRASH-REPORTER-TEST-GUARD-1: test runs never reach a live report sender."""

import httpx
import pytest

from smartmemory_app import crash_reporter, launch_metrics

# (sender, guard env name that lifts the pytest no-op)
SENDERS = {
    "report_exception": lambda: crash_reporter.report_exception(
        ValueError("boom"), source="cli"
    ).status,
    "report_event": lambda: crash_reporter.report_event(
        "hook_deadline_exceeded", {"phase": "x"}, dedupe_key="k", wait=2
    ).status,
    "send_support_report": lambda: crash_reporter.send_support_report(
        {
            k: ""
            for k in (
                "doctor.txt",
                "environment.txt",
                "config.txt",
                "cli-debug.log",
                "daemon.log",
            )
        }
    ).status,
    "launch_metrics.emit": lambda: launch_metrics.emit("setup.complete"),
}

# sender | guard lifted | expected network calls
ROWS = [
    ("report_exception", False, 0),
    ("report_event", False, 0),
    ("send_support_report", False, 0),
    ("launch_metrics.emit", False, 0),
    ("report_exception", True, 1),
    ("report_event", True, 1),
    ("send_support_report", True, 1),
    ("launch_metrics.emit", True, 1),
]


@pytest.mark.parametrize(
    "name,lifted,calls",
    ROWS,
    ids=[f"{r[0]}-{'lifted' if r[1] else 'guarded'}" for r in ROWS],
)
def test_senders_are_noop_under_pytest_until_lifted(
    name, lifted, calls, monkeypatch, tmp_path
):
    for key in ("SMARTMEMORY_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_HOST", "https://capture.test")
    monkeypatch.delenv("SMARTMEMORY_DISABLE_LAUNCH_METRICS", raising=False)
    monkeypatch.delenv("SMARTMEMORY_CRASH_REPORTS_IN_TESTS", raising=False)
    monkeypatch.delenv("SMARTMEMORY_LAUNCH_METRICS_IN_TESTS", raising=False)
    if lifted:
        monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS_IN_TESTS", "1")
        monkeypatch.setenv("SMARTMEMORY_LAUNCH_METRICS_IN_TESTS", "1")
    sent = []

    def stub_post(self, url, *args, **kwargs):
        sent.append(str(url))
        return httpx.Response(200, request=httpx.Request("POST", url))

    # Network boundary: any real send would raise, only the stub may be reached.
    def no_real_network(*args, **kwargs):
        raise AssertionError("real network send attempted")

    monkeypatch.setattr(httpx._client.Client, "post", stub_post)
    monkeypatch.setattr(httpx._client.Client, "send", no_real_network)
    from smartmemory_app.config import SmartMemoryConfig, save_config

    save_config(SmartMemoryConfig(mode="local", crash_reports=True))
    SENDERS[name]()
    crash_reporter.flush_outbox(tmp_path / "data")
    assert len(sent) == calls
