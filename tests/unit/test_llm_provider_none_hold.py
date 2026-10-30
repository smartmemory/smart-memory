"""WRAPPER-2.0-LLM-PROVIDER-NONE-PORT-1: llm_provider none sends nothing, on every path.

``none`` is the default and the "no AI" setup choice. Core has no off value: with
no model it accepts an ambient provider key, an OPENAI_BASE_URL or an importable
Claude Agent SDK. Each row drives one wrapper path in the process state it runs in
(daemon, worker, capture worker, CLI) under one ambient route and asserts, at the
transport, that no request was made: httpx's real transport and every non-loopback
socket connect raise and are recorded, and the Claude Agent SDK is a recording fake.
Lite save rows also assert the save asked core for no LLM work (``key_present``).

The groq rows are the other side: a configured provider still reaches its transport
(mocked, it raises), so the hold does not over-block. One explicit row table each.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from smartmemory_app.config import LLM_KEY_ENV_VARS, SmartMemoryConfig, save_config

DUMMY = "dummy-not-a-real-key"
QUESTION = "Which database did we pick?"
NOTE = "We picked SQLite for Lite because it needs no server."

# ---------------------------------------------------------------------------
# Ambient routes core would take with no model chosen
# ---------------------------------------------------------------------------

ENVS = {
    "keys": {"OPENAI_API_KEY": DUMMY, "GROQ_API_KEY": DUMMY},
    "base-url": {"OPENAI_BASE_URL": "http://llm.invalid/v1", "OPENAI_API_KEY": DUMMY},
    "agent-sdk": {},  # no keys; the fake SDK is importable (fixture)
}


class Sent(list):
    """Every LLM transport attempt: ("httpx", host), ("socket", addr) or ("sdk", prompt)."""


@pytest.fixture
def sent(monkeypatch, tmp_path):
    import smartmemory.integration.llm.llm_client as llm_client
    import smartmemory.pipeline.work_graph.spawn as core_spawn
    import smartmemory.utils.llm as core_llm

    from smartmemory_app import storage

    for name in (*LLM_KEY_ENV_VARS, "GEMINI_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(name, "")
    # The code under test writes os.environ directly: setenv first so teardown restores.
    for name in (
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "SMARTMEMORY_CAPTURE_LLM",
        "SMARTMEMORY_CAPTURE_OFFLINE",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    # Restore what apply_runtime_config replaces in core's module.
    for name in (
        "call_openai_chat",
        "_call_claude_agent",
        "_call_bedrock_converse",
        "llm_route_available",
    ):
        monkeypatch.setattr(core_llm, name, getattr(core_llm, name))
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    # apply_runtime_config replaces core's worker spawner: restore it at teardown.
    monkeypatch.setattr(
        core_spawn, "spawn_worker_if_idle", core_spawn.spawn_worker_if_idle
    )

    seen = Sent()

    def http(self, request):
        seen.append(("httpx", request.url.host))
        raise httpx.ConnectError("test transport guard: no LLM request may leave")

    async def ahttp(self, request):
        seen.append(("httpx", request.url.host))
        raise httpx.ConnectError("test transport guard: no LLM request may leave")

    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is None or host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        seen.append(("socket", host))
        raise OSError("test socket guard: no LLM request may leave")

    def sdk_query(*args, **kwargs):
        seen.append(("sdk", kwargs.get("prompt", args[0] if args else "")))
        raise RuntimeError("test Claude Agent SDK: no LLM request may leave")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahttp)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(llm_client, "_claude_agent_query", sdk_query)
    monkeypatch.setattr(llm_client, "CLAUDE_AGENT_SDK_AVAILABLE", False)
    yield seen
    storage._shutdown()


def _ambient(monkeypatch, tmp_path, provider: str, env: str) -> None:
    import smartmemory.integration.llm.llm_client as llm_client

    for name, value in ENVS[env].items():
        monkeypatch.setenv(name, value)
    if env == "agent-sdk":
        monkeypatch.setattr(llm_client, "CLAUDE_AGENT_SDK_AVAILABLE", True)
    save_config(
        SmartMemoryConfig(
            mode="local", data_dir=str(tmp_path / "data"), llm_provider=provider
        )
    )


@pytest.fixture
def placements(monkeypatch):
    """``key_present`` of every Lite save: True means core was told to queue LLM work."""
    import smartmemory.pipeline.work_graph.lite_save as lite_save

    seen: list[bool] = []
    real = lite_save.lite_placement

    def spy(config, key_present, *args, **kwargs):
        seen.append(key_present)
        return real(config, key_present, *args, **kwargs)

    monkeypatch.setattr(lite_save, "lite_placement", spy)
    return seen


def _daemon(monkeypatch):
    from smartmemory_app.storage import apply_runtime_config
    from smartmemory_app.viewer_server import _build_app

    apply_runtime_config()  # viewer_server.main runs it at daemon start
    return TestClient(_build_app(), raise_server_exceptions=False)


def _tour_client(client):
    from smartmemory_app.tour import TourHttpClient

    tour = TourHttpClient("http://127.0.0.1:1")
    tour._client.close()
    tour._client = client
    return tour


# ---------------------------------------------------------------------------
# Path drivers: each returns what the path produced, for the row's own check
# ---------------------------------------------------------------------------


def _cli_ask(monkeypatch, placements):
    from smartmemory_app import cli as cli_mod

    def daemon(*_a, **_k):
        raise AssertionError("sm ask must stop before asking the daemon")

    monkeypatch.setattr(cli_mod, "_memory_request", daemon)
    result = CliRunner().invoke(cli_mod.cli, ["ask", QUESTION])
    assert result.exit_code != 0
    assert "No LLM is configured" in result.output, result.output


def _daemon_ask(monkeypatch, placements):
    client = _daemon(monkeypatch)
    client.post("/memory/ingest", json={"content": NOTE})
    body = client.post("/memory/ask", json={"question": QUESTION}).json()
    assert body.get("code") == "llm_provider_none", body
    assert body.get("synthesized") is False, body


def _tour_ask(monkeypatch, placements):
    from smartmemory_app.tour import TourArcDriver

    tour = _tour_client(_daemon(monkeypatch))
    tour.ingest(NOTE)
    shown = TourArcDriver._ask_answer(types.SimpleNamespace(client=tour), QUESTION)
    assert "No LLM is configured" in shown, shown


def _daemon_ingest(monkeypatch, placements):
    """sm add with the daemon up, the tour's seed and /memory/ingest callers."""
    client = _daemon(monkeypatch)
    response = client.post("/memory/ingest", json={"content": NOTE})
    assert response.status_code == 200, response.text
    assert placements, "the save never reached Lite placement"


def _sm_add_in_process(monkeypatch, placements):
    """sm add with the daemon down: the CLI opens the Lite store itself."""
    from smartmemory_app import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_daemon_request", lambda *a, **k: None)
    result = CliRunner().invoke(cli_mod.cli, ["add", NOTE])
    assert result.exit_code == 0, result.output
    assert placements, "the save never reached Lite placement"


def _launch_event(monkeypatch, placements):
    client = _daemon(monkeypatch)
    response = client.post("/memory/launch/event", json={"event_type": "tour_started"})
    assert response.status_code == 200, response.text


def _hook_jobs(monkeypatch, placements, tmp_path):
    """The daemon's hook consumer: observe, learn and distill jobs."""
    from smartmemory_app.hook_consumer import execute_hook_job

    _daemon(monkeypatch)
    payloads = {
        "observe": {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": NOTE,
        },
        "learn": {"tool_name": "Bash", "error": "SQLite database is locked: " + NOTE},
        "distill": {"last_assistant_message": NOTE + " Decision: keep SQLite."},
    }
    for phase, body in payloads.items():
        path = tmp_path / f"{phase}.json"
        path.write_text(json.dumps({**body, "cwd": str(tmp_path)}))
        execute_hook_job({"phase": phase, "payload_path": str(path)})
    assert placements, "no hook phase reached Lite placement"


def _core_call_llm(monkeypatch, placements):
    """Anything in the daemon that calls core's call_llm (enrichers, lifecycle, re-extraction)."""
    import smartmemory.utils.llm as core_llm

    _daemon(monkeypatch)
    with pytest.raises(Exception) as caught:
        core_llm.call_llm(user_content=QUESTION, max_output_tokens=5)
    assert type(caught.value).__name__ == "LlmProviderNone", repr(caught.value)


def _worker_extraction(monkeypatch, placements):
    """The work-graph worker (sm worker, worker_entry): its route gate and its LLM step."""
    import smartmemory.utils.llm as core_llm
    from smartmemory.background.extraction_worker import _run_llm_extraction

    from smartmemory_app.storage import apply_runtime_config

    apply_runtime_config()  # worker_entry.main and sm worker run it first
    assert core_llm.llm_route_available()[0] is False
    result = _run_llm_extraction(NOTE)
    assert result.get("status") == "llm_failed", result


def _reextract(monkeypatch, placements):
    """sm admin reextract and the startup offer choose ruler mode, never llm."""
    import smartmemory.utils.llm as core_llm

    from smartmemory_app.work_graph import get_reextract_offer

    _daemon(monkeypatch)
    assert get_reextract_offer() is None
    assert core_llm.llm_route_available()[0] is False


def _capture_worker(monkeypatch, placements):
    """Transcript capture with SMARTMEMORY_CAPTURE_LLM=1: Groq-pinned extraction and lessons."""
    import smartmemory.tools.factory as factory
    from smartmemory.pipeline.config import is_costless

    from smartmemory_app import capture_queue, capture_worker
    from smartmemory_app.lesson_classifier import classify_lessons

    monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
    monkeypatch.setenv("GROQ_API_KEY", DUMMY)  # the opt-in requires it
    built = []
    monkeypatch.setattr(
        factory,
        "create_lite_memory",
        lambda **kw: built.append(kw["pipeline_profile"]) or object(),
    )
    capture_worker._memory()
    assert capture_queue.capture_llm_opted_in() is False
    assert built and is_costless(built[0]), "capture built an LLM pipeline"
    with pytest.raises(Exception):  # session lessons' classifier, in this process
        classify_lessons([NOTE], {})


PATHS = [
    # (id, driver, needs tmp_path)
    ("sm-ask-cli", _cli_ask, False),
    ("daemon-ask", _daemon_ask, False),
    ("tour-ask", _tour_ask, False),
    ("daemon-ingest", _daemon_ingest, False),
    ("sm-add-in-process", _sm_add_in_process, False),
    ("launch-event", _launch_event, False),
    ("hook-consumer", _hook_jobs, True),
    ("core-call-llm", _core_call_llm, False),
    ("work-graph-worker", _worker_extraction, False),
    ("reextract", _reextract, False),
    ("capture-worker", _capture_worker, False),
]

# One row per (path, ambient route); built once, one parametrize.
NONE_ROWS = [
    (f"{path_id}-{env}", driver, takes_tmp, env)
    for path_id, driver, takes_tmp in PATHS
    for env in ENVS
]


@pytest.mark.parametrize(
    "driver,takes_tmp,env",
    [row[1:] for row in NONE_ROWS],
    ids=[row[0] for row in NONE_ROWS],
)
def test_provider_none_reaches_no_llm_transport(
    sent, placements, monkeypatch, tmp_path, driver, takes_tmp, env
):
    _ambient(monkeypatch, tmp_path, "none", env)

    if takes_tmp:
        driver(monkeypatch, placements, tmp_path)
    else:
        driver(monkeypatch, placements)

    assert list(sent) == [], f"an LLM transport was reached: {list(sent)}"
    assert True not in placements, "a Lite save queued LLM work under provider none"


# ---------------------------------------------------------------------------
# Not over-blocked: a configured provider still routes (transport mocked)
# ---------------------------------------------------------------------------


def _groq_daemon_ask(monkeypatch, sent, placements):
    client = _daemon(monkeypatch)
    client.post("/memory/ingest", json={"content": NOTE})
    client.post("/memory/ask", json={"question": QUESTION})
    assert ("httpx", "api.groq.com") in sent, list(sent)


def _groq_core_call_llm(monkeypatch, sent, placements):
    import smartmemory.utils.llm as core_llm

    _daemon(monkeypatch)
    with pytest.raises(Exception):
        core_llm.call_llm(user_content=QUESTION, max_output_tokens=5)
    assert ("httpx", "api.groq.com") in sent, list(sent)


def _groq_ingest(monkeypatch, sent, placements):
    client = _daemon(monkeypatch)
    response = client.post("/memory/ingest", json={"content": NOTE})
    assert response.status_code == 200, response.text
    assert placements and all(placements), placements


def _groq_worker(monkeypatch, sent, placements):
    import smartmemory.utils.llm as core_llm
    from smartmemory.background.extraction_worker import _run_llm_extraction

    from smartmemory_app.storage import apply_runtime_config

    apply_runtime_config()
    assert core_llm.llm_route_available()[0] is True
    _run_llm_extraction(NOTE)
    assert ("httpx", "api.groq.com") in sent, list(sent)


def _groq_capture(monkeypatch, sent, placements):
    import smartmemory.tools.factory as factory
    from smartmemory.pipeline.config import is_costless

    from smartmemory_app import capture_queue, capture_worker

    monkeypatch.setenv("SMARTMEMORY_CAPTURE_LLM", "1")
    built = []
    monkeypatch.setattr(
        factory,
        "create_lite_memory",
        lambda **kw: built.append(kw["pipeline_profile"]) or object(),
    )
    capture_worker._memory()
    assert capture_queue.capture_llm_opted_in() is True
    assert built and not is_costless(built[0])


GROQ_ROWS = [
    ("ask", _groq_daemon_ask),
    ("core-call-llm", _groq_core_call_llm),
    ("lite-save", _groq_ingest),
    ("work-graph-worker", _groq_worker),
    ("capture-worker", _groq_capture),
]


@pytest.mark.parametrize(
    "driver", [row[1] for row in GROQ_ROWS], ids=[row[0] for row in GROQ_ROWS]
)
def test_configured_provider_still_routes(
    sent, placements, monkeypatch, tmp_path, driver
):
    monkeypatch.setenv("GROQ_API_KEY", DUMMY)
    save_config(
        SmartMemoryConfig(
            mode="local", data_dir=str(tmp_path / "data"), llm_provider="groq"
        )
    )

    driver(monkeypatch, sent, placements)

    assert {kind for kind, _ in sent} <= {"httpx"}, list(sent)
    assert all(host == "api.groq.com" for _, host in sent), list(sent)


# ---------------------------------------------------------------------------
# Every worker the wrapper causes to start is the held wrapper entry
# ---------------------------------------------------------------------------

WRAPPER_ENTRY = ["-m", "smartmemory_app.worker_entry"]


@pytest.fixture
def spawned(monkeypatch):
    """argv of every detached spawn, core's and the wrapper's: recorded, never run."""
    import smartmemory.pipeline.work_graph.spawn as core_spawn
    import smartmemory.utils.process as process

    seen: list[list[str]] = []

    def record(argv, **_kwargs):
        seen.append([str(arg) for arg in argv])

    monkeypatch.setattr(core_spawn, "spawn_detached", record)
    monkeypatch.setattr(process, "spawn_detached", record)
    return seen


def _assert_wrapper_entry(spawned, tmp_path):
    assert len(spawned) == 1, spawned
    argv = spawned[0]
    assert argv[1:3] == WRAPPER_ENTRY, f"spawned {argv[1:]}, not the held wrapper entry"
    assert argv[argv.index("--idle-exit") + 1] == "300", argv
    assert argv[argv.index("--data-dir") + 1].startswith(str(tmp_path.resolve())), argv


def _spawn_core_after_hold(monkeypatch, tmp_path):
    """Any core caller of spawn_worker_if_idle once the wrapper applied its config."""
    from smartmemory_app.storage import apply_runtime_config

    apply_runtime_config()
    from smartmemory.pipeline.work_graph.spawn import spawn_worker_if_idle

    assert spawn_worker_if_idle(tmp_path / "data") is True


def _spawn_store_on_run_added(monkeypatch, tmp_path):
    """sm add in process, /ingest, MCP local backend: core's save hook on the wrapper store."""
    from smartmemory_app import storage

    storage.get_memory()._on_run_added()


def _spawn_capture_store(monkeypatch, tmp_path):
    """The capture worker's own store."""
    from smartmemory_app import capture_worker

    with capture_worker._memory() as memory:
        memory._on_run_added()


def _spawn_requeue_dead(monkeypatch, tmp_path):
    from smartmemory_app import cli as cli_mod
    from smartmemory_app import work_graph

    graph = types.SimpleNamespace(requeue_dead=lambda: 1)
    monkeypatch.setattr(work_graph, "get_work_graph", lambda: graph)
    result = CliRunner().invoke(cli_mod.cli, ["worker", "requeue-dead"])
    assert result.exit_code == 0, result.output
    assert "Requeued 1 boxes." in result.output


SPAWN_ROWS = [
    # (id, provider, trigger)
    ("core-spawner-after-hold-none", "none", _spawn_core_after_hold),
    ("store-on-run-added-none", "none", _spawn_store_on_run_added),
    ("capture-store-none", "none", _spawn_capture_store),
    ("requeue-dead-none", "none", _spawn_requeue_dead),
    ("store-on-run-added-groq", "groq", _spawn_store_on_run_added),
]


@pytest.mark.parametrize(
    "provider,trigger",
    [row[1:] for row in SPAWN_ROWS],
    ids=[row[0] for row in SPAWN_ROWS],
)
def test_spawned_worker_is_the_held_wrapper_entry(
    sent, spawned, monkeypatch, tmp_path, provider, trigger
):
    if provider == "groq":
        monkeypatch.setenv("GROQ_API_KEY", DUMMY)
    _ambient(monkeypatch, tmp_path, provider, "keys")

    trigger(monkeypatch, tmp_path)

    _assert_wrapper_entry(spawned, tmp_path)
    assert list(sent) == [], f"an LLM transport was reached: {list(sent)}"


def test_spawned_wrapper_worker_still_extracts_with_groq(
    sent, spawned, placements, monkeypatch, tmp_path
):
    """Provider-set control: the entry the wrapper now spawns still sends LLM work to groq."""
    from smartmemory_app import storage
    from smartmemory_app.worker_entry import run_configured_worker

    monkeypatch.setenv("GROQ_API_KEY", DUMMY)
    _ambient(monkeypatch, tmp_path, "groq", "keys")
    storage.get_memory().ingest(NOTE)
    assert placements and all(placements), placements
    _assert_wrapper_entry(spawned, tmp_path)
    data_dir = spawned[0][spawned[0].index("--data-dir") + 1]
    storage._shutdown()
    monkeypatch.setattr(storage, "_memory", None)

    run_configured_worker(data_dir, idle_exit=0.2, lease=60)

    assert ("httpx", "api.groq.com") in sent, list(sent)


# ---------------------------------------------------------------------------
# The reviewer's probe, end to end in real processes: LLM work queued under a
# provider, the user switches to none, a later in-process save spawns the worker
# ---------------------------------------------------------------------------

GUARD = """
import os, socket, sys
sys.modules["claude_agent_sdk"] = None  # never the real Agent SDK in a test process
_log = os.environ["PROVNONE_GUARD_LOG"]
_real = socket.socket.connect
def _connect(self, address):
    host = address[0] if isinstance(address, tuple) else None
    if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
        with open(_log, "a") as log:
            log.write(f"blocked {os.getpid()} {host}\\n")
        raise OSError("test guard: non-loopback connect refused")
    return _real(self, address)
socket.socket.connect = _connect
with open(_log, "a") as log:
    log.write(f"loaded {os.getpid()}\\n")
"""

QUEUE_UNDER_PROVIDER = """
import sys
import smartmemory.pipeline.work_graph.spawn as core_spawn
import smartmemory.utils.process as process
def record(argv, **kwargs):
    print("recorded spawn", argv[1:3])
core_spawn.spawn_detached = record
process.spawn_detached = record
from smartmemory_app import storage
storage.get_memory().ingest(sys.argv[1])
storage._shutdown()
"""

ADD_DAEMON_DOWN = """
import sys
from smartmemory_app import cli
cli._daemon_request = lambda *a, **k: None
cli.cli(["add", sys.argv[1]], standalone_mode=False)
"""


class _Listener(BaseHTTPRequestHandler):
    hits: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).hits.append((self.path, NOTE.encode() in body))
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b'{"error": {"message": "test listener"}}')

    def log_message(self, *_args):
        pass


def _work_nodes(data_dir):
    db = data_dir / "memory.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:  # legacy work_nodes and v2 wg_box
        return [
            row
            for table in ("work_nodes", "wg_box")
            for row in conn.execute(f"SELECT kind, status, last_error FROM {table}")
        ]


def _our_workers(data_dir):
    import psutil

    pids = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        if str(data_dir) in cmdline and "--data-dir" in cmdline:
            pids.append(proc.info["pid"])
    return pids


def test_queued_llm_work_is_not_sent_after_switch_to_none(tmp_path):
    import huggingface_hub.constants as hf_constants
    import psutil

    data_dir = (tmp_path / "data").resolve()
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(GUARD)
    guard_log = tmp_path / "guard.log"
    _Listener.hits = []
    port = int(
        os.environ.get("PROVNONE_TEST_PORT") or 0
    )  # registered test port, else ephemeral
    server = ThreadingHTTPServer(("127.0.0.1", port), _Listener)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.pathsep.join([str(site), os.environ.get("PYTHONPATH", "")]),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SMARTMEMORY_DATA_DIR": str(data_dir),
        "HF_HOME": str(hf_constants.HF_HOME),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SMARTMEMORY_NO_UPDATE_CHECK": "1",
        "PROVNONE_GUARD_LOG": str(guard_log),
        # The ambient route core's own worker would take: a dummy key and a base URL.
        "OPENAI_API_KEY": DUMMY,
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}/v1",
    }

    def configure(provider):
        script = (
            "from smartmemory_app.config import SmartMemoryConfig, save_config\n"
            f"save_config(SmartMemoryConfig(mode='local', data_dir={str(data_dir)!r}, llm_provider={provider!r}))"
        )
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)

    def run(script):
        done = subprocess.run(
            [sys.executable, "-c", script, NOTE],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
        return done

    seen_workers: set[int] = set()
    try:
        # 1. A provider is set: the save queues deferred LLM extraction, spawn recorded only.
        configure("openai")
        first = run(QUEUE_UNDER_PROVIDER)
        assert "recorded spawn" in first.stdout, first.stdout
        queued = [
            r for r in _work_nodes(data_dir) if r[0] == "extract" and r[1] == "pending"
        ]
        assert queued, f"nothing queued under openai: {_work_nodes(data_dir)}"
        before_switch = len(_Listener.hits)
        _Listener.hits.clear()

        # 2. The user chooses no AI.
        configure("none")

        # 3. sm add with the daemon down, spawn not patched: a real worker starts.
        run(ADD_DAEMON_DOWN)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            seen_workers.update(_our_workers(data_dir))
            open_nodes = [
                r for r in _work_nodes(data_dir) if r[1] in ("pending", "running")
            ]
            if _Listener.hits or (seen_workers and not open_nodes):
                break
            time.sleep(0.5)
        nodes = _work_nodes(data_dir)
    finally:
        seen_workers.update(_our_workers(data_dir))
        for pid in seen_workers:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                proc.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                proc.kill()
        server.shutdown()
        server.server_close()

    guard = guard_log.read_text().splitlines() if guard_log.exists() else []
    print(
        f"listener hits before switch={before_switch} after switch={len(_Listener.hits)} "
        f"{_Listener.hits}; workers={sorted(seen_workers)}; nodes={nodes}"
    )
    assert not [line for line in guard if line.startswith("blocked")], guard
    assert seen_workers, "the save under none spawned no worker"
    assert {f"loaded {pid}" for pid in seen_workers} <= set(guard), (
        seen_workers,
        guard,
    )
    assert _Listener.hits == [], (
        f"LLM requests after the switch to none: {_Listener.hits}"
    )
    dead = [r for r in nodes if r[1] == "dead" and "no_llm_route" in (r[2] or "")]
    assert dead, f"the queued LLM box did not go dead no_llm_route: {nodes}"
