"""Stop never boots out a launchd job this HOME did not install (DAEMON-9014-CRASH-DIAG-1).

launchd labels are per user, not per HOME. A test (or a second install) running with
another HOME asked real launchd about ``ai.smartmemory.daemon``, saw the owner's job
loaded and booted it out: the owner's daemon died twice on 2026-10-22 during wrapper
suite runs. A loaded job is ours only when launchd loaded it from this HOME's plist.

One row per destructive call site and job origin. Every row fails on the base, where
any loaded job with the label counted as ours.
"""

import logging
import subprocess
import sys

import pytest

from smartmemory_app import daemon

# Rows name a job by role. The label is read from the daemon module when a test runs:
# the autouse host_isolation_guard patches it to a scratch label per test, so a label
# captured at import time would never match the one the code under test asks about.
ROLE_ATTR = {"daemon": "_LAUNCHD_DAEMON_LABEL", "worker": "_LAUNCHD_WORKER_LABEL"}
FOREIGN = "/Users/someone-else/Library/LaunchAgents/{label}.plist"


def label_of(role):
    return getattr(daemon, ROLE_ATTR[role])


class FakeLaunchd:
    """Answers `launchctl print` with the job's plist path; records every command."""

    def __init__(self, origins):
        self.origins = dict(origins)  # label -> "ours" | "foreign" | "no-path"
        self.commands = []

    def printed(self, label):
        origin = self.origins[label]
        path = {
            "ours": str(daemon._launchd_plist_path(label)),
            "foreign": FOREIGN.format(label=label),
        }.get(origin)
        lines = [f"gui/501/{label} = {{", "\tstate = running", "\tpid = 4242"]
        if path:
            lines.insert(1, f"\tpath = {path}")
            lines.append(f"\tstderr path = {path}.log")
        return "\n".join(lines + ["}"])

    def run(self, command, **kwargs):
        assert command[0] == "launchctl", command
        self.commands.append(tuple(command[:2]))
        label = command[-1].rsplit("/", 1)[-1].removesuffix(".plist")
        if command[1] == "print":
            if label in self.origins:
                return subprocess.CompletedProcess(command, 0, self.printed(label), "")
            return subprocess.CompletedProcess(
                command, 113, "", "Could not find service"
            )
        if command[1] in ("bootout", "unload"):
            self.origins.pop(label, None)
            return subprocess.CompletedProcess(command, 0, "", "")
        pytest.fail(f"Unexpected launchctl action: {command}")

    @property
    def bootouts(self):
        return [c for c in self.commands if c[1] in ("bootout", "unload")]


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    agents = daemon._launchd_plist_path(label_of("daemon")).parent
    agents.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(daemon, "_retire_legacy_workers", lambda **kw: False)
    monkeypatch.setattr(daemon, "_stop_core_worker", lambda: None)
    monkeypatch.setattr(daemon, "is_running", lambda require_healthy=True: False)

    def install(origins):
        fake = FakeLaunchd({label_of(role): o for role, o in origins.items()})
        monkeypatch.setattr(subprocess, "run", fake.run)
        return fake

    return install


# origin of the loaded job -> what _launchd_loaded must answer
LOADED_ROWS = [
    ("ours", True),
    ("foreign", False),  # the 2026-10-22 incident: owner's job seen from a test HOME
    ("no-path", False),  # submitted job, no plist: never ours
]


@pytest.mark.parametrize("origin,expected", LOADED_ROWS)
def test_loaded_means_loaded_from_this_home(launchd, origin, expected):
    launchd({"daemon": origin})
    assert daemon._launchd_loaded(label_of("daemon")) is expected


def test_symlinked_home_still_matches(launchd, monkeypatch, tmp_path):
    real = tmp_path / "real-home"
    (real / "Library" / "LaunchAgents").mkdir(parents=True)
    link = tmp_path / "link-home"
    link.symlink_to(real)
    monkeypatch.setenv("HOME", str(link))
    fake = launchd({"daemon": "ours"})
    monkeypatch.setattr(
        fake,
        "printed",
        lambda label: f"\tpath = {real}/Library/LaunchAgents/{label}.plist\n",
    )
    assert daemon._launchd_loaded(label_of("daemon")) is True


# call site, job roles loaded with their origin, roles that must be booted out
SITE_ROWS = [
    ("stop_daemon", {"daemon": "ours", "worker": "ours"}, {"daemon", "worker"}),
    ("stop_daemon", {"daemon": "foreign", "worker": "foreign"}, set()),
    ("stop_daemon", {"daemon": "foreign"}, set()),
    ("stop_daemon", {"daemon": "no-path", "worker": "no-path"}, set()),
    ("stop_workers", {"worker": "ours"}, {"worker"}),
    ("stop_workers", {"worker": "foreign"}, set()),
    ("retire_legacy_agent", {"worker": "ours"}, {"worker"}),
    ("retire_legacy_agent", {"worker": "foreign"}, set()),
]


@pytest.mark.parametrize("site,origins,booted", SITE_ROWS)
def test_destructive_sites_only_boot_out_our_jobs(
    launchd, monkeypatch, caplog, site, origins, booted
):
    fake = launchd(origins)
    if site == "retire_legacy_agent":
        daemon._launchd_plist_path(label_of("worker")).write_text("enrichment_worker.main()")
        monkeypatch.setattr(daemon, "_wait_legacy_exit", lambda pid: None)
    with caplog.at_level(logging.WARNING, logger=daemon.__name__):
        {
            "stop_daemon": daemon.stop_daemon,
            "stop_workers": daemon._stop_workers,
            "retire_legacy_agent": daemon._retire_legacy_agent,
        }[site]()
    gone = {role for role in origins if label_of(role) not in fake.origins}
    assert gone == booted
    for role, origin in origins.items():
        label = label_of(role)
        if origin != "ours":
            assert label in fake.origins, f"{label} ({origin}) was booted out"
            assert any(
                label in r.getMessage() and "not ours" in r.getMessage()
                for r in caplog.records
            ), f"no WARNING naming {label}"
