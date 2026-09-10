"""Regression coverage for the superadmin diagnostics bundle downloader."""

from __future__ import annotations

import io
import tarfile

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app.cli import cli


def _bundle_bytes() -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name in (
            "manifest.json",
            "health.json",
            "errors.json",
            "env.json",
            "ready.json",
        ):
            data = b"{}"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return archive.getvalue()


def _install_mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_mock_transport)


def test_doctor_bundle_streams_to_out_without_leaking_api_key(monkeypatch, tmp_path):
    """The archive is copied byte-for-byte and the bearer token is never echoed."""
    token = "super-secret-token-that-must-not-appear"
    served = _bundle_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=served, request=request)

    _install_mock_client(monkeypatch, handler)
    output_path = tmp_path / "support-bundle.tar.gz"

    result = CliRunner().invoke(
        cli,
        [
            "doctor",
            "--bundle",
            "--url",
            "https://console.example/",
            "--out",
            str(output_path),
        ],
        env={"SMARTMEMORY_API_KEY": token, "SMARTMEMORY_UPDATE_CHECK": "0"},
    )

    assert result.exit_code == 0, result.output
    assert output_path.read_bytes() == served
    assert requests[0].url == "https://console.example/superadmin/diagnostics/bundle"
    assert requests[0].headers["authorization"] == f"Bearer {token}"
    assert token not in result.output
    assert "manifest.json" in result.output


@pytest.mark.parametrize(
    ("status_code", "expected_message"),
    [(401, "HTTP 401"), (500, "HTTP 500")],
)
def test_doctor_bundle_rejects_unsuccessful_responses_before_creating_output(
    monkeypatch, tmp_path, status_code, expected_message
):
    output_path = tmp_path / "support-bundle.tar.gz"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    _install_mock_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "doctor",
            "--bundle",
            "--url",
            "https://console.example",
            "--out",
            str(output_path),
        ],
        env={"SMARTMEMORY_API_KEY": "secret", "SMARTMEMORY_UPDATE_CHECK": "0"},
    )

    assert result.exit_code != 0
    assert expected_message in result.output
    assert not output_path.exists()
    assert not list(tmp_path.glob(f".{output_path.name}.*.tmp"))


def test_doctor_bundle_removes_temporary_output_when_streaming_times_out(
    monkeypatch, tmp_path
):
    output_path = tmp_path / "support-bundle.tar.gz"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _install_mock_client(monkeypatch, handler)
    result = CliRunner().invoke(
        cli,
        [
            "doctor",
            "--bundle",
            "--url",
            "https://console.example",
            "--out",
            str(output_path),
        ],
        env={"SMARTMEMORY_API_KEY": "secret", "SMARTMEMORY_UPDATE_CHECK": "0"},
    )

    assert result.exit_code != 0
    assert "timed out" in result.output
    assert not output_path.exists()
    assert not list(tmp_path.glob(f".{output_path.name}.*.tmp"))
