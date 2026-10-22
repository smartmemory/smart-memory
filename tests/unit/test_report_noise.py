"""CLI-REPORT-NOISE-1: expected exceptions are not crashes, and the daily cap holds.

No test here reaches PostHog: every PostHog key variable is blanked and
``httpx.Client`` is replaced by a function that fails the test if it is built.
"""

import itertools
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect

from smartmemory_app import bug_report, crash_reporter, hook_failures, report_outbox
from smartmemory_app.storage import StoreBusyError

# Captured at import, before the autouse isolation fixture patches them.
_REAL_DEBUG_LOG_PATH = bug_report.debug_log_path
_REAL_MARKER_DIRECTORY = hook_failures.marker_directory

POSTHOG_KEY_VARS = (
    "POSTHOG_API_KEY",
    "SMEM_POSTHOG_API_KEY",
    "REG_POSTHOG_API_KEY",
    "POSTHOG_KEY",
)


@pytest.fixture(autouse=True)
def no_posthog(monkeypatch):
    for name in POSTHOG_KEY_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("SMARTMEMORY_CRASH_REPORTS", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("a report test built an httpx client (network call)")

    monkeypatch.setattr(crash_reporter.httpx, "Client", forbidden)


@pytest.fixture
def captured(monkeypatch):
    """Replace the transport boundary: record what would have been enqueued."""
    events = []
    monkeypatch.setattr(
        crash_reporter,
        "_capture",
        lambda event, *args, **kwargs: events.append(event)
        or crash_reporter.ReportResult("queued", "x"),
    )
    return events


# --- one list of expected exceptions, consulted by every report_* entry point ---

EXPECTED = {
    "ClientDisconnect": lambda: ClientDisconnect(),
    "StoreBusyError": lambda: StoreBusyError("Store busy."),
    "StoreBusyError wrapped by raise-from": lambda: _wrapped(StoreBusyError("busy")),
}


def _wrapped(cause):
    try:
        try:
            raise cause
        except Exception as inner:
            raise RuntimeError("route failed") from inner
    except RuntimeError as outer:
        return outer


def _asgi_boom(exc):
    import asyncio

    from smartmemory_app import viewer_server

    handler = viewer_server._build_app().exception_handlers[Exception]
    return asyncio.run(handler(None, exc))


def _install_failure(exc):
    from smartmemory_app import install_troubleshooting

    return install_troubleshooting.handled_install_failure(exc, ["setup"], rows=[])


ENTRY_POINTS = {
    "report_exception": lambda exc: crash_reporter.report_exception(exc, source="t"),
    "report_in_background": lambda exc: crash_reporter.report_in_background(
        exc, source="t"
    ),
    "crash_notice": lambda exc: crash_reporter.crash_notice(exc, ["status"]),
    "handled_install_failure": _install_failure,
    "asgi_handler": _asgi_boom,
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("kind", EXPECTED)
def test_expected_exception_is_never_captured(monkeypatch, captured, entry, kind):
    # Table: every expected type x every entry point (the product is the point:
    # each entry point must consult the one list).
    monkeypatch.setattr(
        "smartmemory_app.install_troubleshooting.doctor_summary", lambda **kw: []
    )
    ENTRY_POINTS[entry](EXPECTED[kind]())
    assert captured == [], f"{entry} reported expected {kind}"


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_real_crash_is_still_captured(monkeypatch, captured, entry):
    monkeypatch.setattr(
        "smartmemory_app.install_troubleshooting.doctor_summary", lambda **kw: []
    )
    ENTRY_POINTS[entry](ValueError("genuine failure"))
    assert captured, f"{entry} dropped a genuine crash"


def test_implicit_context_does_not_hide_a_crash(captured):
    try:
        try:
            raise StoreBusyError("busy")
        except StoreBusyError:
            raise ValueError("a different bug")  # implicit __context__ only
    except ValueError as exc:
        crash_reporter.report_exception(exc, source="t")
    assert captured


# --- dedupe and cap keyed to the install id's location ---


def _real_resolution(monkeypatch, shared: Path):
    """Undo the isolation patches so HOME and SMARTMEMORY_DATA_DIR decide paths."""
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(shared))
    monkeypatch.setattr(bug_report, "debug_log_path", _REAL_DEBUG_LOG_PATH)
    monkeypatch.setattr(crash_reporter, "debug_log_path", _REAL_DEBUG_LOG_PATH)
    monkeypatch.setattr(hook_failures, "marker_directory", _REAL_MARKER_DIRECTORY)


def _two_homes(monkeypatch, tmp_path):
    shared = tmp_path / "shared-data"
    homes = []
    for name in ("home-a", "home-b"):
        home = tmp_path / name
        home.mkdir()
        homes.append(home)
    _real_resolution(monkeypatch, shared)
    return shared, homes


def _use_home(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_daily_cap_holds_across_homes_sharing_one_data_dir(monkeypatch, tmp_path):
    shared, homes = _two_homes(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(crash_reporter, "_post", lambda p: posts.append(p) or True)
    for i, home in zip(range(24), itertools.cycle(homes)):  # 12 per HOME, 24 total
        _use_home(monkeypatch, home)
        crash_reporter.report_exception(
            RuntimeError("boom"), source="t", dedupe_key=f"k{i}"
        )
    ids = {Path(p["distinct_id"]) for p in posts}
    assert len(ids) == 1, "one data dir is one install id"
    assert len(posts) <= 20, f"{len(posts)} reports sent for one install in one day"


def test_hook_shell_reports_share_the_install_cap_with_other_sources(
    monkeypatch, tmp_path
):
    shared, homes = _two_homes(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(crash_reporter, "_post", lambda p: posts.append(p) or True)
    for home in homes:  # same markers under two HOMEs, one data dir
        _use_home(monkeypatch, home)
        marker_dir = hook_failures.marker_directory()
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "hook-failures.tsv").write_text(
            "".join(f"observe\t{code}\t1\n" for code in range(1, 13))
        )
        hook_failures.consume_failures(marker_dir)
    assert len({p["distinct_id"] for p in posts}) == 1
    assert len(posts) <= 20


def test_outbox_lives_beside_the_install_id(monkeypatch, tmp_path):
    shared, homes = _two_homes(monkeypatch, tmp_path)
    _use_home(monkeypatch, homes[0])
    monkeypatch.setattr(crash_reporter, "_post", lambda p: True)
    crash_reporter.report_exception(RuntimeError("boom"), source="t")
    assert (shared / ".install-id").exists()
    assert (shared / "report-outbox" / ".reservations").exists()
    assert not (homes[0] / ".smartmemory" / "report-outbox" / ".reservations").exists()


# --- manual `report --send` cap ---


def _texts():
    return {
        "doctor.txt": "doctor",
        "environment.txt": "env",
        "config.txt": "cfg",
        "message.txt": "help",
        "cli-debug.log": "",
        "daemon.log": "",
    }


def test_manual_send_is_capped_per_install_per_day(monkeypatch, tmp_path):
    shared, homes = _two_homes(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(crash_reporter, "_post", lambda p: posts.append(p) or True)
    statuses = []
    for home in itertools.islice(itertools.cycle(homes), 30):
        _use_home(monkeypatch, home)
        statuses.append(crash_reporter.send_support_report(_texts()).status)
    cap = report_outbox.MANUAL_DAILY_CAP
    assert cap == 5
    assert len(posts) == cap
    assert statuses.count("capped") == 30 - cap


def test_manual_cap_does_not_block_crash_reports(monkeypatch, tmp_path):
    shared, homes = _two_homes(monkeypatch, tmp_path)
    monkeypatch.setattr(crash_reporter, "_post", lambda p: True)
    for _ in range(report_outbox.MANUAL_DAILY_CAP + 2):
        crash_reporter.send_support_report(_texts())
    result = crash_reporter.report_exception(RuntimeError("real"), source="t")
    assert result.status in {"sent", "queued"}


def test_cli_tells_the_user_when_the_manual_cap_is_reached(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from smartmemory_app import cli

    monkeypatch.setattr(
        crash_reporter,
        "send_support_report",
        lambda texts: crash_reporter.ReportResult("capped"),
    )
    monkeypatch.setattr(
        "smartmemory_app.support_diagnostics.support_texts",
        lambda out, msg="": _texts(),
    )
    zip_path = tmp_path / "bundle.zip"
    monkeypatch.setattr(
        "smartmemory_app.support_diagnostics.write_support_bundle",
        lambda path, output, texts=None: zip_path,
    )
    monkeypatch.setattr(cli.doctor_cmd, "callback", lambda *a: None)
    result = CliRunner().invoke(cli.cli, ["report", "--send", "--yes", "help"])
    assert "daily" in result.output.lower() and str(zip_path) in result.output
