"""Wrapper work-graph contract using real SQLite/usearch stores."""

import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from filelock import FileLock


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from smartmemory_app import config, storage
    from smartmemory_app.local_api import api

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SMARTMEMORY_OBSERVABILITY", "false")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (False, "test")
    )
    cfg = config.SmartMemoryConfig(mode="local", data_dir=str(tmp_path))
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(storage, "load_config", lambda: cfg)
    monkeypatch.setattr(storage, "is_configured", lambda: True)
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    app = FastAPI()
    app.mount("/memory", api)
    lock = FileLock(tmp_path / ".worker.lock")
    lock.acquire()
    try:
        with TestClient(app) as client:
            yield storage.get_memory(), client, lock
    finally:
        lock.release()
        storage._shutdown()


def route_cli(monkeypatch, client):
    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr("smartmemory_app.cli._daemon_request", request)


@pytest.mark.parametrize("daemon_up", [False, True])
def test_keyless_add(lite, monkeypatch, daemon_up):
    from smartmemory_app.cli import cli

    memory, client, _ = lite
    if daemon_up:
        route_cli(monkeypatch, client)
    else:
        monkeypatch.setattr("smartmemory_app.cli._daemon_request", lambda *a, **k: None)
    result = CliRunner().invoke(cli, ["add", "Alice works at Anthropic in London."])
    assert result.exit_code == 0, result.output
    assert memory._work_graph.stats()["pending"] == 1
    nodes = memory.graph.get_all_nodes_scoped()
    assert any(n.get("extraction_status") == "ruler_only" for n in nodes)
    assert not any(n.get("extraction_status") == "llm_enriched" for n in nodes)


def test_keyless_hook_save(lite):
    from smartmemory_app.lifecycle import MemoryLifecycle

    memory, _, _ = lite
    MemoryLifecycle("test-work-graph").observe(
        "Bash", {"command": "pwd"}, "A project directory"
    )
    assert memory._work_graph.stats()["runs"] == 1


@pytest.mark.parametrize("queued", [False, True])
def test_endpoint_never_enqueues_legacy(lite, monkeypatch, queued):
    _, client, _ = lite
    enqueue = Mock(side_effect=AssertionError("legacy queue must not be written"))
    monkeypatch.setattr("smartmemory_app.enrichment_queue.enqueue", enqueue)
    monkeypatch.setattr(
        "smartmemory_app.storage.ingest",
        lambda *a, **k: {"item_id": "item", "queued": queued},
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert client.post("/memory/ingest", json={"content": "hello"}).status_code == 200
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    "flags,mode,count",
    [
        ([], "ruler", 1),
        (["--yes"], "llm", 1),
        (["--yes", "--all"], "llm", 2),
        (["--yes", "--ruler"], "ruler", 1),
    ],
)
def test_reextract_flags(lite, monkeypatch, flags, mode, count):
    from smartmemory_app import storage
    from smartmemory_app.cli import cli

    memory, _, _ = lite
    storage.ingest("Alice works at Anthropic.", origin="cli:add")
    other = storage.ingest("Bob lives in Paris.", origin="cli:add")
    memory.update_properties(other, {"extraction_status": "llm_enriched"})
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (bool(flags), "test")
    )
    monkeypatch.setattr(
        "smartmemory_app.cli._daemon_request",
        Mock(side_effect=AssertionError("no daemon wait")),
    )
    result = CliRunner().invoke(cli, ["admin", "reextract", *flags], input="y\n")
    assert result.exit_code == 0, result.output
    assert f"{count} memories; about" in result.output
    assert f"mode: {mode}" in result.output
    assert f"Queued {count} re-extraction runs" in result.output
    assert ("Queue re-extraction?" in result.output) == ("--yes" not in flags)
    assert memory._work_graph.stats()["runs"] == 2 + count
    assert memory._work_graph.get_meta("reextract_decision") == "accepted"


def test_reextract_decline_and_abort(lite):
    from smartmemory_app import storage
    from smartmemory_app.cli import cli

    memory, _, _ = lite
    storage.ingest("Alice works at Anthropic.", origin="cli:add")
    runner = CliRunner()
    result = runner.invoke(cli, ["admin", "reextract"], input="n\n")
    assert result.exit_code != 0
    assert memory._work_graph.stats()["runs"] == 1
    assert memory._work_graph.get_meta("reextract_decision") is None
    result = runner.invoke(cli, ["admin", "reextract", "--decline"])
    assert result.exit_code == 0, result.output
    assert memory._work_graph.get_meta("reextract_decision") == "declined"
    assert memory._work_graph.stats()["runs"] == 1


def test_status_offer_and_requeue(lite, monkeypatch):
    from smartmemory_app import storage
    from smartmemory_app.cli import cli

    memory, _, _ = lite
    storage.ingest("Alice works at Anthropic.", origin="cli:add")
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (True, "test")
    )
    from smartmemory_app.viewer_server import _build_app

    health_client = TestClient(_build_app())
    monkeypatch.setattr(
        "smartmemory_app.daemon.get_status",
        lambda: health_client.get("/health").json(),
    )
    from smartmemory.pipeline.work_graph.reextract import reextract_offer

    expected_offer = reextract_offer(memory, memory._work_graph)
    assert health_client.get("/health").json()["reextract_offer"] == expected_offer
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert expected_offer in result.output
    assert "pending=1, running=0, dead=0" in result.output
    assert (
        "1 memories were saved without an LLM. Run `sm admin reextract`"
        in result.output
    )
    result = CliRunner().invoke(cli, ["admin", "reextract", "--decline"])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "memories were saved without an LLM" not in result.output
    assert health_client.get("/health").json()["reextract_offer"] is None
    box = memory._work_graph.claim(10)
    memory._work_graph.fail(box, "test", retryable=False)
    result = CliRunner().invoke(cli, ["worker", "requeue-dead"])
    assert result.exit_code == 0, result.output
    assert memory._work_graph.stats()["pending"] == 1
    assert memory._work_graph.stats()["dead"] == 0


def test_api_reextract(lite):
    from smartmemory_app import storage

    memory, client, _ = lite
    storage.ingest("Alice works at Anthropic.", origin="cli:add")
    response = client.post("/memory/reextract", json={"ruler": True, "all": True})
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 1
    assert response.json()["mode"] == "ruler"
    assert memory._work_graph.stats()["runs"] == 2


def test_daemon_reads_worker_extract_fresh(lite, tmp_path, monkeypatch):
    from smartmemory_app.cli import cli

    memory, client, lock = lite
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (True, "test")
    )
    response = client.post(
        "/memory/ingest",
        json={"content": "Alice works at Anthropic.", "context": {"origin": "cli:add"}},
    )
    assert response.status_code == 200, response.text
    item_id = response.json()["item_id"]
    # Prime both the core cache and the real daemon read routes before another process writes.
    assert memory.get(item_id).metadata["extraction_status"] == "ruler_only"
    assert client.get(f"/memory/{item_id}").json()["extraction_status"] == "ruler_only"
    client.get("/memory/list")
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    (boundary / "sitecustomize.py").write_text("""
import smartmemory.utils.llm as llm
llm.llm_route_available = lambda: (True, "test")
llm.call_llm = lambda **kwargs: ({}, "{}")
from smartmemory.background import extraction_worker
extraction_worker._run_llm_extraction = lambda content: {"status": "ok", "extraction": {"entities": [], "relations": []}}
""")
    lock.release()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "smartmemory.cli",
                "--data-dir",
                str(tmp_path),
                "worker",
                "run",
                "--idle-exit",
                "0.2",
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(boundary) + os.pathsep + os.environ["PYTHONPATH"],
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        lock.acquire(timeout=5)
    assert memory._work_graph.stats()["done"] >= 1
    assert (
        client.get(f"/memory/{item_id}").json()["extraction_status"] == "llm_enriched"
    )
    listed = client.get("/memory/list").json()["items"]
    assert (
        next(n for n in listed if n["item_id"] == item_id)["metadata"][
            "extraction_status"
        ]
        == "llm_enriched"
    )
    route_cli(monkeypatch, client)
    result = CliRunner().invoke(cli, ["get", item_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["extraction_status"] == "llm_enriched"
    with FileLock(tmp_path / ".write.lock", timeout=0):
        pass
    assert not list(tmp_path.glob("*.pid"))


def test_daemon_starts_one_core_worker(tmp_path, monkeypatch):
    from smartmemory_app import daemon

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("smartmemory_app.config.llm_key_present", lambda: False)
    popen = Mock(return_value=Mock(pid=123456))
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    daemon._start_workers(4)
    popen.assert_called_once()
    command = popen.call_args.args[0]
    assert command == [
        sys.executable,
        "-m",
        "smartmemory_app.worker_entry",
        "--data-dir",
        str(tmp_path),
    ]
    assert not list(tmp_path.glob("worker.*.pid"))


def test_setup_and_startup_offer(lite, monkeypatch, capsys, caplog):
    from smartmemory_app import setup, storage, viewer_server

    storage.ingest("Alice works at Anthropic.", origin="cli:add")
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (True, "test")
    )
    status = TestClient(viewer_server._build_app()).get("/health").json()
    monkeypatch.setattr(setup, "_install_launchd_plist", lambda: False)
    monkeypatch.setattr(
        "smartmemory_app.daemon.start_daemon",
        lambda **kw: status,
    )
    constructor = Mock(side_effect=AssertionError("setup must use daemon memory"))
    with monkeypatch.context() as boundary:
        boundary.setattr(storage, "get_memory", constructor)
        setup._start_daemon_local()
    constructor.assert_not_called()
    assert "1 memories were saved without an LLM" in capsys.readouterr().out
    monkeypatch.setattr(viewer_server, "_warm_backend", lambda: True)
    monkeypatch.setattr(viewer_server, "_sync_hooks", lambda: None)
    with caplog.at_level("INFO"):
        thread = viewer_server._start_background_warmup()
        thread.join(timeout=5)
    assert caplog.text.count("1 memories were saved without an LLM") == 1
    viewer_server._set_startup_state(None)


def test_daemon_worker_process_stops_cleanly(tmp_path, monkeypatch):
    import time

    from smartmemory.pipeline.work_graph.spawn import worker_is_running
    from smartmemory_app import daemon

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    real_popen = subprocess.Popen
    processes = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", start)
    try:
        daemon._start_workers(3)
        deadline = time.monotonic() + 30
        while not worker_is_running(tmp_path) and time.monotonic() < deadline:
            assert processes[0].poll() is None, (tmp_path / "worker.log").read_text()
            time.sleep(0.05)
        assert worker_is_running(tmp_path)
        daemon._start_workers(3)
        assert len(processes) == 1
        try:
            subprocess.run(
                ["ps", "-o", "command=", "-p", str(processes[0].pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except PermissionError:
            # Sandbox denies ps: retain real child, lock, signals and exit.
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda command, **kw: subprocess.CompletedProcess(
                    command, 0, " ".join(processes[0].args), ""
                ),
            )
        daemon._stop_workers()
        assert processes[0].wait(timeout=45) == 0
    finally:
        daemon._stop_workers()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    assert not list(tmp_path.glob("worker.*.pid"))
    assert not worker_is_running(tmp_path)
    with FileLock(tmp_path / ".write.lock", timeout=0):
        pass


def test_status_down_never_constructs_memory(tmp_path, monkeypatch):
    from smartmemory_app import config, storage
    from smartmemory_app.cli import cli

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(
        config, "load_config", lambda: config.SmartMemoryConfig(mode="local")
    )
    monkeypatch.setattr("smartmemory_app.daemon.get_status", lambda: None)
    monkeypatch.setattr("smartmemory_app.daemon.should_be_running", lambda: False)
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (True, "test")
    )
    constructor = Mock(side_effect=AssertionError("status must not construct memory"))
    monkeypatch.setattr(storage, "get_memory", constructor)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    constructor.assert_not_called()
    assert "Work:       pending=0, running=0, dead=0" in result.output
    assert "SmartMemory daemon is not running." in result.output
    assert "memories were saved without an LLM" not in result.output


def test_status_explains_pending_work_with_missing_spacy(tmp_path, monkeypatch):
    from smartmemory_app import config
    from smartmemory_app.cli import cli

    monkeypatch.setattr(
        config, "load_config", lambda: config.SmartMemoryConfig(mode="local")
    )
    monkeypatch.setattr(
        "smartmemory_app.work_graph.get_work_status",
        lambda: {"pending": 2, "running": 0, "dead": 0, "worker_running": False},
    )
    monkeypatch.setattr("spacy.util.is_package", lambda name: False)
    monkeypatch.setattr("smartmemory_app.daemon.get_status", lambda: None)
    monkeypatch.setattr("smartmemory_app.daemon.should_be_running", lambda: False)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "pending=2" in result.output
    assert "WARNING: 2 queued work box(es) cannot drain" in result.output
    assert "sm setup" in result.output


def test_degraded_start_explains_pending_work_with_missing_spacy(monkeypatch, capsys):
    from smartmemory_app.cli import _report_start_status

    monkeypatch.setattr(
        "smartmemory_app.work_graph.get_work_status",
        lambda: {"pending": 2, "running": 0, "dead": 0, "worker_running": False},
    )
    monkeypatch.setattr("spacy.util.is_package", lambda name: False)
    _report_start_status(
        {"status": "degraded", "degraded_reason": "spaCy model missing"}
    )
    output = capsys.readouterr()
    assert "spaCy model missing" in output.out
    assert "WARNING: 2 queued work box(es) cannot drain" in output.err


def test_setup_uses_verified_core_spacy_installer(monkeypatch):
    import click
    from smartmemory.errors import MissingModelError

    from smartmemory_app.setup import _ensure_spacy

    seen = []
    monkeypatch.setattr(
        "smartmemory.tools.factory._ensure_spacy_model",
        lambda model: seen.append(model),
    )
    _ensure_spacy("en_core_web_md")
    assert seen == ["en_core_web_md"]

    def missing(model):
        raise MissingModelError("model still missing from this Python")

    monkeypatch.setattr("smartmemory.tools.factory._ensure_spacy_model", missing)
    with pytest.raises(click.ClickException, match="model still missing"):
        _ensure_spacy("en_core_web_sm")
