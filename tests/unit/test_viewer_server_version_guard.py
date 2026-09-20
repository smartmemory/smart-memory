"""Regression tests for daemon restart after an installed-package upgrade."""

from unittest.mock import call, patch

from fastapi.testclient import TestClient


def test_version_guard_exits_on_first_request_after_version_change():
    from smartmemory_app.viewer_server import _build_app

    installed_versions = {
        "smartmemory": "1.4.104",
        "smartmemory-core": "1.4.104",
    }

    with patch(
        "importlib.metadata.version",
        side_effect=lambda package: installed_versions[package],
    ):
        app = _build_app()
        installed_versions["smartmemory-core"] = "1.4.105"

        with patch("smartmemory_app.viewer_server.os._exit") as exit_process:
            response = TestClient(app).get("/")

    assert response.status_code == 200
    exit_process.assert_called_once_with(0)


def test_version_guard_checks_first_request_without_exiting_when_versions_match():
    from smartmemory_app.viewer_server import _build_app

    installed_versions = {
        "smartmemory": "1.4.104",
        "smartmemory-core": "1.4.104",
    }

    with patch(
        "importlib.metadata.version",
        side_effect=lambda package: installed_versions[package],
    ) as package_version:
        app = _build_app()
        package_version.reset_mock()

        with patch("smartmemory_app.viewer_server.os._exit") as exit_process:
            response = TestClient(app).get("/")

    assert response.status_code == 200
    assert package_version.call_args_list == [
        call("smartmemory"),
        call("smartmemory-core"),
    ]
    exit_process.assert_not_called()
