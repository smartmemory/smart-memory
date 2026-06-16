"""Service-mode recovery fixes.

Fix 3 (remote_backend): RemoteMemory.ingest/search must RAISE RemoteBackendError on
a hosted-API failure (timeout/unreachable/HTTP error) instead of masquerading — a
fake "Error: ..." id from ingest, or an empty list from search that reads as "no
results". recall() (prompt-hook background path) must instead degrade to empty.

Fix 1 (daemon launchd): `sm stop` must bootout the launchd job (KeepAlive=true) so a
plain kill is not respawned. Helpers must be no-ops off macOS so the subprocess path
is unaffected in dev/CI/Linux.
"""
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_app.remote_backend import RemoteMemory, RemoteBackendError


def _make_remote() -> RemoteMemory:
    # No token → constructor skips the /auth/me bootstrap, so no network in tests.
    with patch("smartmemory_app.remote_backend.get_api_key", return_value=""):
        return RemoteMemory(api_url="https://api.test")


class TestRemoteSurfacesErrors:
    def test_ingest_raises_on_api_error(self):
        m = _make_remote()
        with patch.object(m, "_request", return_value={"error": "API unreachable at ..."}):
            with pytest.raises(RemoteBackendError, match="unreachable"):
                m.ingest("hello")

    def test_ingest_returns_id_on_success(self):
        m = _make_remote()
        with patch.object(m, "_request", return_value={"item_id": "itm_1"}):
            assert m.ingest("hello") == "itm_1"

    def test_search_raises_on_api_error(self):
        m = _make_remote()
        with patch.object(m, "_request", return_value={"error": "Request failed: timeout"}):
            with pytest.raises(RemoteBackendError, match="timeout"):
                m.search("q")

    def test_search_returns_list_on_success(self):
        m = _make_remote()
        with patch.object(m, "_request", return_value=[{"item_id": "a"}]):
            assert m.search("q") == [{"item_id": "a"}]

    def test_recall_degrades_to_empty_on_remote_error(self):
        # recall runs on every prompt hook — a remote failure degrades, never raises.
        m = _make_remote()
        with patch.object(m, "_request", return_value={"error": "service down"}):
            out = m.recall(query="anything", top_k=5)
        assert isinstance(out, str)  # produced a (degraded) recall string, no exception


class TestLaunchdRecovery:
    def test_launchd_helpers_are_noop_off_macos(self):
        """Off macOS the launchd helpers must do nothing — subprocess path unaffected."""
        from smartmemory_app import daemon
        with patch("sys.platform", "linux"):
            assert daemon._launchd_loaded("ai.smartmemory.daemon") is False
            assert daemon._launchd_bootout("ai.smartmemory.daemon") is False
            assert daemon._launchd_bootstrap("ai.smartmemory.daemon") is False
            assert daemon._launchd_manages_daemon() is False

    def test_stop_daemon_boots_out_launchd_when_loaded(self):
        """On macOS with the job loaded, stop must bootout BOTH labels (so KeepAlive can't revive)."""
        from smartmemory_app import daemon
        booted = []
        with patch("sys.platform", "darwin"), \
                patch.object(daemon, "_stop_workers"), \
                patch.object(daemon, "_launchd_loaded", return_value=True), \
                patch.object(daemon, "_launchd_bootout", side_effect=lambda label: booted.append(label) or True), \
                patch.object(daemon, "is_running", return_value=False), \
                patch.object(daemon, "_pid_file", return_value=MagicMock()):
            daemon.stop_daemon()
        assert booted == ["ai.smartmemory.worker", "ai.smartmemory.daemon"]
