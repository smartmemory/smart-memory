"""Held receipts through real core storage and the consumer boundary."""

from uuid import uuid4

import pytest
from redis import Redis
from smartmemory.pipeline import PipelineConfig
from smartmemory.tools.factory import create_server_memory
from smartmemory.utils.cache import NoOpCache


@pytest.fixture
def core(monkeypatch):
    name = f"test_core_hold_receipt_{uuid4().hex}"
    redis = Redis(host="localhost", port=9010)
    before = set(redis.execute_command("GRAPH.LIST"))
    cfg = PipelineConfig.graph_only(llm_enabled=False)
    cfg.extraction.entity_ruler.enabled = False
    cfg.extraction.llm_extract.extract_decisions = False
    instance = None
    monkeypatch.setattr("smartmemory.progress._get_redis", lambda: None)
    monkeypatch.setattr(
        "smartmemory.streams.pipeline_producer.emit_pipeline_event", lambda **kw: None
    )
    try:
        instance = create_server_memory(
            graph_name=name,
            pipeline_profile=cfg,
            enable_ontology=False,
            observability=False,
            cache=NoOpCache(),
        )
        yield instance
    finally:
        if instance is not None:
            instance.close()
        if name.encode() in redis.execute_command("GRAPH.LIST"):
            redis.execute_command("GRAPH.DELETE", name)
        keys = list(redis.scan_iter(match=f"sm:vchain:head:{name}:*"))
        if keys:
            redis.delete(*keys)
        assert not list(redis.scan_iter(match=f"sm:vchain:head:{name}:*"))
        assert not {
            k
            for k in set(redis.execute_command("GRAPH.LIST")) - before
            if k.startswith(b"test_core_hold_receipt_")
        }
        redis.close()


def held(core, content="Test knowledge.", **kwargs):
    return core.ingest(content, context={"confidence": 0.4}, sync=True)


def test_local_storage_and_daemon_preserve_hold(core, monkeypatch, tmp_path):
    from smartmemory_app import storage, local_api

    monkeypatch.setattr(storage, "get_memory", lambda: core)
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    for sync in (True, False):
        receipt = storage.ingest(
            "Test knowledge.", properties={"confidence": 0.4}, sync=sync
        )
        assert receipt["status"] == "held" and receipt["item_id"] is None
        assert receipt["reason"] == "low_confidence"
    receipt = local_api.ingest_endpoint(
        local_api.IngestRequest(
            content="Test knowledge.", properties={"confidence": 0.4}
        )
    )
    assert receipt["status"] == "held" and receipt["reason"] == "low_confidence"
    assert core._graph.backend._query("MATCH (n:Item) RETURN n") == []


def test_remote_storage_preserves_hold(core, monkeypatch):
    from smartmemory_app.remote_backend import RemoteMemory

    remote = RemoteMemory.__new__(RemoteMemory)
    # Only HTTP transport is substituted. The receipt comes from the real pipeline.
    monkeypatch.setattr(remote, "_request", lambda *a, **kw: held(core))
    receipt = remote.ingest("Test knowledge.")
    assert receipt["status"] == "held" and receipt["item_id"] is None
    assert receipt["reason"] == "low_confidence"


def test_cli_holds_are_excluded_from_added_count(core, monkeypatch):
    from click.testing import CliRunner
    from smartmemory_app import cli

    monkeypatch.setattr(cli, "_memory_request", lambda *a, **kw: held(core))
    result = CliRunner().invoke(cli.cli, ["add", "Test knowledge."])
    assert result.exit_code == 0, result.output
    assert "Held: low_confidence" in result.output and "None" not in result.output
    result = CliRunner().invoke(
        cli.cli, ["add", "--all", "-"], input="Test knowledge.\n"
    )
    assert result.exit_code == 0, result.output
    assert (
        "Held: low_confidence" in result.output and "Added 0 memories" in result.output
    )


@pytest.mark.parametrize("import_claude", [False, True])
def test_tour_does_not_count_held_seeds(core, tmp_path, import_claude):
    from smartmemory_app.tour import TourArcDriver

    class Client:
        def ingest(self, content, **kw):
            return held(core, content)

        def search(self, *a, **kw):
            return {"results": []}

        def recall(self, *a, **kw):
            return {"results": []}

        def stats(self):
            return {}

    if import_claude:
        (tmp_path / "CLAUDE.md").write_text("Test project knowledge.")
    events = []
    result = TourArcDriver(
        Client(), facts=["Test knowledge."], cwd=tmp_path, pace_seconds=0
    ).run_default_arc(include_claude_import=import_claude, emit=events.append)
    assert result.seeded_count == 0 and result.imported_claude is False
    search = next(event for event in events if event.title == "Semantic search")
    assert "facts just stored" not in search.body
    assert "existing memory" in search.body


@pytest.mark.parametrize("phase", ["observe", "distill", "learn"])
def test_lifecycle_preserves_hold(core, monkeypatch, tmp_path, caplog, phase):
    from smartmemory_app import storage
    from smartmemory_app.lifecycle import MemoryLifecycle

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "get_memory", lambda: core)
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    ingest = core.ingest
    monkeypatch.setattr(
        core,
        "ingest",
        lambda content, **kw: ingest(content, context={"confidence": 0.4}, sync=True),
    )
    lifecycle = MemoryLifecycle(f"test_core_hold_receipt_{uuid4().hex}")
    if phase == "observe":
        lifecycle.observe("Read", "test input", "test output")
        assert lifecycle._observation_count == 0
        assert "Observation held: low_confidence" in caplog.text
    elif phase == "distill":
        lifecycle._current_user_turn = "Test question."
        receipt = lifecycle.distill("Test answer.")
        assert receipt["status"] == "held" and receipt["item_id"] is None
        assert lifecycle._current_user_turn == "Test question."
        assert "Turn pair held: low_confidence" in caplog.text
    else:
        receipt = lifecycle.learn("Read", "Test error.")
        assert receipt["status"] == "held" and receipt["item_id"] is None
        assert "Error memory held: low_confidence" in caplog.text
