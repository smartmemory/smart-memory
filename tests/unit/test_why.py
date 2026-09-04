"""Unit tests for the read-only ``sm why`` command."""

import json
from types import SimpleNamespace

from click.testing import CliRunner

from smartmemory_app import cli as cli_module
from smartmemory_app import config


PROVENANCE = {
    "decision": {
        "decision_id": "decision-current",
        "content": "Use the hosted service for team decision provenance.",
        "status": "active",
        "created_at": "2026-07-10T09:00:00Z",
        "rationale": "The service retains supersession and evidence links.",
    },
    "reasoning_trace": [],
    "evidence": [
        {
            # Real provenance payloads (verified live, DEMO-WALKTHROUGH-4 spike 0.3)
            # carry NO top-level created_at on resolved memories — the timestamp
            # lives in metadata.created_at.
            "memory": {
                "memory_type": "episodic",
                "content": "The local daemon only supports generic memory lineage.",
                "metadata": {"created_at": "2026-07-09T09:00:00Z"},
            }
        }
    ],
    "superseded": [
        {
            "decision_id": "decision-old",
            "content": "Use a local decision store.",
            "created_at": "2026-07-01T09:00:00Z",
        }
    ],
}


def test_why_remote_renders_provenance(monkeypatch) -> None:
    """Remote mode renders decision, supersession, evidence, and extra matches."""
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: SimpleNamespace(mode="remote", api_url="https://test", team_id="team"),
    )

    def request(_cfg, _method, path, **kwargs):
        if path.endswith("/search"):
            assert kwargs["params"] == {"topic": "Why hosted?", "limit": 3}
            return {
                "decisions": [
                    {
                        "decision_id": "decision-current",
                        "content": "Use hosted provenance.",
                    },
                    {
                        "decision_id": "decision-other",
                        "content": "Keep decisions query-only.",
                    },
                ]
            }
        assert path == "/memory/decisions/decision-current/provenance"
        return PROVENANCE

    monkeypatch.setattr(cli_module, "_why_remote_request", request)

    result = CliRunner().invoke(cli_module.cli, ["why", "Why hosted?"])

    assert result.exit_code == 0, result.output
    assert "Use the hosted service" in result.output
    assert "supersedes: Use a local decision store." in result.output
    assert "[episodic] The local daemon" in result.output
    # Evidence timestamp must render from metadata.created_at (never "unknown date")
    assert "(2026-07-09T09:00:00Z)" in result.output
    assert "unknown date" not in result.output
    assert "Also matched: Keep decisions query-only." in result.output


def test_why_date_falls_back_through_metadata_and_transaction_time() -> None:
    """_why_date: created_at > updated_at > metadata.created_at > transaction_time."""
    assert cli_module._why_date({"created_at": "A", "transaction_time": "D"}) == "A"
    assert cli_module._why_date({"updated_at": "B"}) == "B"
    assert cli_module._why_date({"metadata": {"created_at": "C"}}) == "C"
    assert cli_module._why_date({"metadata": None, "transaction_time": "D"}) == "D"
    assert cli_module._why_date({}) == "unknown date"


def test_why_remote_json_prints_raw_provenance(monkeypatch) -> None:
    """Remote --json returns the unmodified provenance payload."""
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: SimpleNamespace(mode="remote", api_url="https://test", team_id="team"),
    )
    responses = iter([{"decisions": [{"decision_id": "decision-current"}]}, PROVENANCE])
    monkeypatch.setattr(
        cli_module, "_why_remote_request", lambda *args, **kwargs: next(responses)
    )

    result = CliRunner().invoke(cli_module.cli, ["why", "Why hosted?", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == PROVENANCE


def test_why_local_shows_honesty_note_and_lineage(monkeypatch) -> None:
    """Local mode visibly degrades to closest-match derivation lineage."""
    monkeypatch.setattr(config, "load_config", lambda: SimpleNamespace(mode="local"))
    responses = iter(
        [
            {"items": [{"item_id": "leaf-12345678"}]},
            {
                "lineage": [
                    {
                        "item_id": "leaf-12345678",
                        "memory_type": "semantic",
                        "content": "Current conclusion",
                    },
                    {
                        "item_id": "root-87654321",
                        "memory_type": "episodic",
                        "content": "Original observation",
                    },
                ],
                "depth": 2,
            },
        ]
    )
    monkeypatch.setattr(
        cli_module, "_daemon_request", lambda *args, **kwargs: next(responses)
    )

    result = CliRunner().invoke(cli_module.cli, ["why", "Why local?"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Note: full decision provenance")
    assert "[semantic] Current conclusion (leaf-123)" in result.output
    assert "[episodic] Original observation (root-876)" in result.output


def test_why_no_results_exits_one_in_remote_and_local_modes(monkeypatch) -> None:
    """Both modes distinguish an empty search from successful output."""
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: SimpleNamespace(mode="remote", api_url="https://test", team_id="team"),
    )
    monkeypatch.setattr(
        cli_module, "_why_remote_request", lambda *args, **kwargs: {"decisions": []}
    )
    remote = CliRunner().invoke(cli_module.cli, ["why", "Missing?"])

    monkeypatch.setattr(config, "load_config", lambda: SimpleNamespace(mode="local"))
    monkeypatch.setattr(
        cli_module, "_daemon_request", lambda *args, **kwargs: {"items": []}
    )
    local = CliRunner().invoke(cli_module.cli, ["why", "Missing?"])

    assert remote.exit_code == 1
    assert "No decisions matched that question." in remote.output
    assert local.exit_code == 1
    assert "No memories matched that question." in local.output


def test_ask_renders_answer_evidence_and_relations(monkeypatch) -> None:
    """Lite `sm ask` renders the daemon's evidence trail below the direct answer."""
    payload = {
        "answer": "No. Zed distrusts Xavier and trusts Yara.",
        "evidence": [
            {"item_id": "memory-123", "content": "Xavier saw Yara steal an amulet."}
        ],
        "relations": [
            {"source": "Zed", "type": "distrusts", "target": "Xavier"},
            {"source": "Zed", "type": "trusts", "target": "Yara"},
        ],
    }
    calls = []

    def daemon_request(*args, **kwargs):
        calls.append((args, kwargs))
        return payload

    monkeypatch.setattr(cli_module, "_daemon_request", daemon_request)

    result = CliRunner().invoke(
        cli_module.cli,
        ["ask", "Does Zed believe Xavier's theft account?", "--limit", "3"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("No. Zed distrusts Xavier and trusts Yara.")
    assert "  Evidence:" in result.output
    assert "    - memory-123: Xavier saw Yara steal an amulet." in result.output
    assert "  Relations:" in result.output
    assert "    - Zed --distrusts--> Xavier" in result.output
    assert calls == [
        (
            ("POST", "/memory/ask"),
            {
                "json": {
                    "question": "Does Zed believe Xavier's theft account?",
                    "limit": 3,
                }
            },
        )
    ]
