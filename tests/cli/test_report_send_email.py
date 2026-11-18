"""CLI-REPORT-SEND-EMAIL-1: `sm report --send` carries an optional contact email, nothing else does."""

import json

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import cli as cli_module, config, crash_reporter

EMAIL = "reporter@example.com"


@pytest.fixture
def posted(monkeypatch, tmp_path):
    """Lift the pytest no-op and route every post to a MockTransport: no live host is reachable."""
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "1")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS_IN_TESTS", "1")
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_UPDATE_CHECK", "0")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_HOST", "https://capture.test")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORT_KEY", "phc_test_public")
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setattr(cli_module.doctor_cmd, "callback", lambda *a: print("Doctor"))
    rows = []
    original = httpx.Client

    def handler(request):
        assert str(request.url) == "https://capture.test/i/v0/e/"
        rows.append(json.loads(request.content))
        return httpx.Response(200, request=request)

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return rows


def run(*args, tty=False, input=None, monkeypatch=None):
    return CliRunner().invoke(cli_module.cli, ["report", "--send", *args], input=input)


def test_email_flag_sent_as_property_and_person_set(posted):
    result = run("--email", EMAIL, "--yes")
    assert result.exit_code == 0, result.output
    props = posted[0]["properties"]
    assert props["contact_email"] == EMAIL
    assert props["$set"] == {"email": EMAIL}
    assert props["$process_person_profile"] is True
    assert posted[0]["distinct_id"] != EMAIL


def test_email_flag_named_before_confirm(posted):
    result = run("--email", EMAIL, input="y\n")
    assert f"Includes your contact email: {EMAIL}" in result.output
    assert posted[0]["properties"]["contact_email"] == EMAIL


def test_tty_prompt_once_enter_skips(posted, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = run(input="\ny\n")
    assert result.output.count("Email for support to reply") == 1
    props = posted[0]["properties"]
    assert "contact_email" not in props and "$set" not in props
    assert props["$process_person_profile"] is False


def test_tty_prompt_typed_email_sent_and_not_remembered_on_no(posted, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = run(input=f"{EMAIL}\ny\nn\n")
    assert result.exit_code == 0, result.output
    assert posted[0]["properties"]["$set"] == {"email": EMAIL}
    assert config.load_config(apply_env=False).contact_email == ""
    assert not (config.config_path()).exists() or "contact_email" not in (
        config.config_path().read_text()
    )


def test_remember_yes_writes_and_reloads(posted, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = run(input=f"{EMAIL}\ny\ny\n")
    assert result.exit_code == 0, result.output
    assert config.load_config(apply_env=False).contact_email == EMAIL
    cfg = config.load_config(apply_env=False)
    config.save_config(cfg)
    assert config.load_config(apply_env=False).contact_email == EMAIL
    # A later send uses it without prompting.
    posted.clear()
    later = run(input="y\n")
    assert "Email for support" not in later.output
    assert posted[0]["properties"]["contact_email"] == EMAIL


def test_invalid_typed_email_is_skipped(posted, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = run(input="not an email\ny\n")
    assert "does not look like an email" in result.output
    assert "contact_email" not in posted[0]["properties"]


@pytest.mark.parametrize("args", [["--yes"], ["--no-email"], []])
def test_no_prompt_no_email(posted, monkeypatch, args):
    # Non-tty (CliRunner), --yes, and --no-email on a tty all skip silently.
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: args == ["--no-email"])
    result = run(*args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "Email for support" not in result.output
    props = posted[0]["properties"]
    assert "contact_email" not in props and "$set" not in props


def test_bad_email_flag_is_a_usage_error(posted):
    result = run("--email", "no at sign", "--yes")
    assert result.exit_code == 2 and not posted


def test_automatic_paths_never_carry_an_email(posted):
    cfg = config.load_config(apply_env=False)
    cfg.contact_email = EMAIL
    config.save_config(cfg)
    assert crash_reporter.report_exception(ValueError("boom"), source="cli").status
    crash_reporter.report_event(
        "hook_deadline_exceeded", {"phase": "x"}, dedupe_key="k", wait=2
    )
    assert posted
    for row in posted:
        assert "contact_email" not in row["properties"]
        assert "$set" not in row["properties"]
        assert EMAIL not in json.dumps(row)
