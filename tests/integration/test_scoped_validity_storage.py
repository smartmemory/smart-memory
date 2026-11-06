"""U11: real Lite read-back catches dropped business validity qualifiers."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from smartmemory import ScopedValidityError
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.temporal.version_tracker import VersionTracker
from smartmemory.tools.factory import create_lite_memory
from smartmemory_app import storage


@pytest.fixture
def card():
    path = Path(os.environ["SCOPED_VALIDITY_FIXTURES"])
    value = json.loads(path.read_text())
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "a4756030405465024f25ba7ba1665b4fd8e1e3cf2972b63ae6e1c8107561aa07"
    )
    return value


@pytest.fixture
def lite(tmp_path, monkeypatch):
    directory = tmp_path / "test_scoped_u11_lite"
    memory = create_lite_memory(
        data_dir=str(directory),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    memory._reranker_config.enabled = False
    monkeypatch.setattr(storage, "get_memory", lambda: memory)
    try:
        yield memory
    finally:
        memory.close()
        shutil.rmtree(directory)
        assert not directory.exists()


def history(memory, card):
    """Persist the pinned fixture in real Lite graph and hashed Version storage."""
    backend = memory._graph.backend
    ids = {}
    for version in card["versions"]:
        identity = "test_scoped_u11_" + uuid4().hex
        ids[version["id"]] = identity
        backend.add_node(
            identity,
            {
                "content": version["content"],
                "scope_go_lives": version["scope_go_lives"],
                "scoped_history": True,
            },
            memory_type="semantic",
        )
        VersionTracker(backend).create_version(
            identity,
            version["content"],
            metadata={"scope_go_lives": version["scope_go_lives"]},
            transaction_time_start=datetime.fromisoformat(version["recorded_at"]),
        )
    backend.add_edge(
        ids["v1"],
        ids["v2"],
        "ROLLOUT_NEXT",
        {"timestamp": card["versions"][1]["recorded_at"]},
    )
    return ids


def search(query="CRF", **kwargs):
    from smartmemory.search.channels import CHANNEL_DEFAULTS

    return storage.search(
        query,
        top_k=1,
        channel_weights={name: float(name == "lexical") for name in CHANNEL_DEFAULTS},
        enable_hybrid=False,
        manual_scan=False,
        **kwargs,
    )


def test_g01_g06_canonical_site_boundaries_and_sponsor(lite, card):
    ids = history(lite, card)
    for row in card["queries"]:
        found = search(
            business_scope={"site": row["site"]} if row["site"] else None,
            valid_at=row["valid_at"],
        )
        assert [item["item_id"] for item in found] == [ids[row["expected"]]], row
        expected = next(v for v in card["versions"] if v["id"] == row["expected"])
        assert found[0]["content"] == expected["content"]
        resolution = found[0]["metadata"]["scoped_validity_resolution"]
        assert resolution["site"] == row["site"]
        assert resolution["selected_item_id"] == ids[row["expected"]]
    # G06: the query matches only v2's content, yet B needs the predecessor outside that hit.
    found = search(
        "Amended", business_scope={"site": "B"}, valid_at="2026-02-08T00:00:00Z"
    )
    assert [item["item_id"] for item in found] == [ids["v1"]]
    assert found[0]["content"] == card["versions"][0]["content"]


def test_g08_legacy_unqualified_search(lite):
    identity = "test_scoped_u11_" + uuid4().hex
    lite._graph.backend.add_node(
        identity,
        {"content": "legacy CRF guidance", "valid_start_time": "2026-01-01T00:00:00Z"},
        memory_type="semantic",
    )
    assert [item["item_id"] for item in search()] == [identity]
    assert [item["item_id"] for item in search(valid_at="2026-02-08T00:00:00Z")] == [
        identity
    ]

    replacement = "test_scoped_u11_" + uuid4().hex
    lite._graph.backend.add_node(
        replacement,
        {
            "content": "legacy amended CRF guidance",
            "valid_start_time": "2026-02-01T00:00:00Z",
        },
        memory_type="semantic",
    )
    assert lite.supersede(
        identity,
        replacement,
        "legacy global amendment",
        valid_from="2026-02-01T00:00:00Z",
    )
    assert [item["item_id"] for item in search()] == [replacement]
    assert [item["item_id"] for item in search(valid_at="2026-02-08T00:00:00Z")] == [
        replacement
    ]


def test_g12_transaction_time_and_schedule_correction(lite, card):
    ids = history(lite, card)
    tracker = VersionTracker(lite._graph.backend)
    tracker.create_version(
        ids["v2"],
        card["versions"][1]["content"],
        metadata={
            "scope_go_lives": [
                {"scope": {"site": "B"}, "valid_from": "2026-03-01T00:00:00Z"}
            ]
        },
        transaction_time_start=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    for as_of, expected in [
        ("2026-01-02T12:00:00Z", "v2"),
        ("2026-01-03T12:00:00Z", "v1"),
    ]:
        found = search(
            business_scope={"site": "B"},
            valid_at="2026-02-20T00:00:00Z",
            as_of_date=as_of,
        )
        assert [item["item_id"] for item in found] == [ids[expected]]


@pytest.mark.parametrize(
    "options",
    [
        {"business_scope": {}},
        {"business_scope": {"country": "US"}},
        {"business_scope": {"site": "*"}},
        {"business_scope": {"site": "B"}, "valid_at": "broken"},
        {"business_scope": {"site": "B"}, "include_superseded": True},
        {"business_scope": {"site": "B"}, "multi_hop": True},
    ],
)
def test_g16_invalid_qualifiers_surface_before_wildcard_shortcut(lite, options):
    with pytest.raises(ScopedValidityError):
        search("*", **options)


def test_g16_site_without_date_uses_core_now(lite, card):
    ids = history(lite, card)
    found = search(business_scope={"site": "B"})
    assert [item["item_id"] for item in found] == [ids["v2"]]
    assert found[0]["metadata"]["scoped_validity_resolution"]["valid_at"]
