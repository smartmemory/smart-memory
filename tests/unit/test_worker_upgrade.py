"""Worker upgrade, shared configuration, and cross-launch-path shutdown."""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from smartmemory_app import daemon, storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_OBSERVABILITY", "false")
    monkeypatch.setattr(
        daemon, "_launchd_plist_path", lambda label: tmp_path / f"{label}.plist"
    )
    monkeypatch.setattr(daemon, "_launchd_loaded", lambda label: False)


def cleanup(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_stop_on_demand_worker_without_wrapper_pid(tmp_path, monkeypatch):
    from smartmemory.pipeline.work_graph.spawn import worker_pid, worker_is_running

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from smartmemory.pipeline.work_graph.worker import run_worker; "
            "import sys; run_worker(sys.argv[1], idle_exit=0, lease=60)",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            subprocess.run(
                ["ps", "-o", "command=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except PermissionError:
            monkeypatch.setattr(
                subprocess,
                "run",
                lambda command, **kw: subprocess.CompletedProcess(
                    command, 0, " ".join(process.args), ""
                ),
            )
        deadline = time.monotonic() + 40
        while worker_pid(tmp_path) != process.pid and time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.05)
        assert worker_pid(tmp_path) == process.pid
        assert not list(tmp_path.glob("worker.*.pid"))
        daemon._stop_workers()
        assert process.wait(timeout=45) == 0
        assert not worker_is_running(tmp_path)
    finally:
        cleanup(process)


def test_stop_continues_when_live_legacy_ps_fails(tmp_path, monkeypatch, caplog):
    from smartmemory.pipeline.work_graph.spawn import worker_is_running, worker_pid

    children = []
    try:
        legacy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        children.append(legacy)
        core = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from smartmemory.pipeline.work_graph.worker import run_worker; "
                "import sys; run_worker(sys.argv[1], idle_exit=0, lease=60)",
                str(tmp_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(core)
        pidfile = tmp_path / "worker.0.pid"
        pidfile.write_text(str(legacy.pid))
        real_run = subprocess.run
        events = []

        def inspect(command, **kwargs):
            if command[0] == "ps":
                if command[command.index("-p") + 1] == str(legacy.pid):
                    events.append("retire")
                    return subprocess.CompletedProcess(
                        command, 2, "", "legacy inspection denied"
                    )
                try:
                    return real_run(command, **kwargs)
                except PermissionError:
                    # Sandbox denies ps; keep the real core lock, signals and exit.
                    assert command[command.index("-p") + 1] == str(core.pid)
                    return subprocess.CompletedProcess(
                        command, 0, " ".join(core.args), ""
                    )
            return real_run(command, **kwargs)

        monkeypatch.setattr(subprocess, "run", inspect)
        deadline = time.monotonic() + 40
        while worker_pid(tmp_path) != core.pid and time.monotonic() < deadline:
            assert core.poll() is None, core.stderr.read()
            time.sleep(0.05)
        assert worker_pid(tmp_path) == core.pid
        assert worker_is_running(tmp_path)

        # Record actual signals, allowing liveness probes but no legacy signal.
        real_kill = os.kill
        signals = []

        def kill(pid, sig):
            signals.append((pid, sig))
            return real_kill(pid, sig)

        monkeypatch.setattr(os, "kill", kill)
        monkeypatch.setattr(daemon.sys, "platform", "darwin")
        loaded = {daemon._LAUNCHD_WORKER_LABEL, daemon._LAUNCHD_DAEMON_LABEL}
        monkeypatch.setattr(daemon, "_launchd_loaded", lambda label: label in loaded)

        def bootout(label):
            events.append(label)
            loaded.remove(label)
            return True

        monkeypatch.setattr(daemon, "_launchd_bootout", bootout)
        monkeypatch.setattr(daemon, "is_running", lambda **kwargs: False)
        daemon._pid_file().write_text("123")
        with caplog.at_level(logging.WARNING, logger=daemon.log.name):
            daemon.stop_daemon()
        assert core.wait(timeout=45) == 0
        assert not worker_is_running(tmp_path)
        assert not daemon._pid_file().exists()
        assert events == [
            "retire",
            daemon._LAUNCHD_WORKER_LABEL,
            daemon._LAUNCHD_DAEMON_LABEL,
        ]
        assert pidfile.read_text() == str(legacy.pid)
        assert legacy.poll() is None
        assert not any(pid == legacy.pid and sig != 0 for pid, sig in signals)
        assert any(
            record.levelno == logging.WARNING
            and "continuing shutdown" in record.getMessage()
            and "legacy inspection denied" in record.getMessage()
            for record in caplog.records
        )
        assert caplog.text.count("processing rows remain unrecovered") == 1

        # The preserved identity must still block the next start.
        with pytest.raises(RuntimeError, match="Cannot inspect live legacy PID"):
            daemon.start_daemon()
        assert pidfile.read_text() == str(legacy.pid)
        assert legacy.poll() is None
        assert not worker_is_running(tmp_path)
        assert not any(pid == legacy.pid and sig != 0 for pid, sig in signals)
    finally:
        for child in children:
            cleanup(child)


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("real_inspection", [True, False])
def test_start_retires_only_identified_legacy_process(
    tmp_path, monkeypatch, legacy, real_inspection
):
    if real_inspection:
        try:
            subprocess.run(
                ["ps", "-o", "command=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                check=True,
            )
        except PermissionError as exc:
            pytest.skip(f"Sandbox forbids ps command-line inspection: {exc}")
    args = [sys.executable, "-c", "import time; time.sleep(120)"]
    if legacy:
        args.append("smartmemory_app.enrichment_worker")
    real_popen = subprocess.Popen
    process = real_popen(args)
    reaper = threading.Thread(target=process.wait, daemon=True)
    reaper.start()
    if not real_inspection:

        def inspect(command, **kwargs):
            assert command == ["ps", "-o", "command=", "-p", str(process.pid)]
            return subprocess.CompletedProcess(
                command, 0, " ".join(args) if process.poll() is None else "", ""
            )

        monkeypatch.setattr(daemon.subprocess, "run", inspect)
    try:
        (tmp_path / "worker.0.pid").write_text(str(process.pid))
        spawn = Mock()
        monkeypatch.setattr(
            daemon.subprocess,
            "Popen",
            lambda command, **kwargs: (
                real_popen(command, **kwargs)
                if command[0] == "ps"
                else spawn(command, **kwargs)
            ),
        )
        daemon._start_workers()
        spawn.assert_called_once()
        assert "smartmemory_app.worker_entry" in spawn.call_args.args[0]
        if legacy:
            assert process.wait(timeout=5) == -15
        else:
            assert process.poll() is None
        assert not (tmp_path / "worker.0.pid").exists()
    finally:
        cleanup(process)


def test_legacy_entry_refuses_to_consume(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "smartmemory_app.enrichment_worker", "--loop"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert "WARNING" in result.stderr
    assert "smartmemory_app.worker_entry" in result.stderr
    assert not (tmp_path / "enrichment_queue.db").exists()


def configure(tmp_path, monkeypatch):
    for key in (
        "OPENAI_BASE_URL",
        "SMARTMEMORY_LLM_MODEL",
        "SMARTMEMORY_EMBEDDING_PROVIDER",
        "SMARTMEMORY_LLM_PROVIDER",
        "SMARTMEMORY_LLM_BASE_URL",
        "LLM_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "config" / "smartmemory" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[smartmemory]\nmode="local"\n[local]\nllm_provider="ollama"\nllm_model="qwen2.5"\nembedding_provider="local"\n'
    )


def provider_env():
    return {
        key: os.environ.get(key)
        for key in (
            "OPENAI_BASE_URL",
            "SMARTMEMORY_LLM_MODEL",
            "SMARTMEMORY_EMBEDDING_PROVIDER",
        )
    }


EXPECTED = {
    "OPENAI_BASE_URL": "http://localhost:11434/v1",
    "SMARTMEMORY_LLM_MODEL": "qwen2.5",
    "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
}


def test_worker_entry_applies_persisted_config(tmp_path, monkeypatch):
    from smartmemory_app import worker_entry

    configure(tmp_path, monkeypatch)
    observed = []

    def run(data_dir, *, idle_exit, lease):
        observed.append((data_dir, idle_exit, lease, provider_env()))

    monkeypatch.setattr("smartmemory.pipeline.work_graph.worker.run_worker", run)
    monkeypatch.setattr(sys, "argv", ["worker_entry", "--data-dir", str(tmp_path)])
    worker_entry.main()
    assert observed == [(str(tmp_path), 0, 60, EXPECTED)]


def test_save_on_demand_child_inherits_config(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)
    monkeypatch.setattr(
        "smartmemory.utils.llm.llm_route_available", lambda: (False, "test")
    )
    real_popen = subprocess.Popen
    children = []

    # Exercise real storage save -> core callback -> on-demand spawner. Replace
    # only the final worker command with a child that records its inherited env.
    def capture(command, **kwargs):
        assert command[2] == "smartmemory.cli"
        destination = tmp_path / "child-env.json"
        script = (
            "import os,json,pathlib; pathlib.Path("
            + repr(str(destination))
            + ").write_text(json.dumps(dict(os.environ)))"
        )
        child = real_popen([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        "smartmemory.pipeline.work_graph.spawn.subprocess.Popen", capture
    )
    try:
        storage.ingest("Alice works in London.", origin="cli:add")
        assert children
        for child in children:
            assert child.wait(timeout=15) == 0
        env = json.loads((tmp_path / "child-env.json").read_text())
        assert {key: env[key] for key in EXPECTED} == EXPECTED
    finally:
        for child in children:
            cleanup(child)
        storage._shutdown()


def test_upgrade_reuses_installer_before_start(tmp_path, monkeypatch):
    from smartmemory_app import setup

    plist = tmp_path / f"{daemon._LAUNCHD_WORKER_LABEL}.plist"
    plist.write_text("smartmemory_app.enrichment_worker")
    events = []
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    monkeypatch.setattr(
        daemon, "_launchd_bootout", lambda label: events.append("unload")
    )
    monkeypatch.setattr(
        daemon, "_retire_legacy_workers", lambda: events.append("retire")
    )

    def install():
        events.append("install")
        plist.write_text("smartmemory_app.worker_entry")
        return True

    monkeypatch.setattr(setup, "_install_launchd_plist", install)
    monkeypatch.setattr(
        daemon.subprocess, "Popen", lambda *a, **k: events.append("spawn")
    )
    daemon._start_workers()
    assert events == ["retire", "install", "retire", "spawn"]


def test_live_daemon_replaces_retired_legacy_worker(monkeypatch):
    events = []
    monkeypatch.setattr(
        daemon, "_upgrade_worker_agent", lambda: events.append("upgrade")
    )
    monkeypatch.setattr(
        daemon, "_retire_legacy_workers", lambda: events.append("retire") or True
    )
    monkeypatch.setattr(daemon, "get_status", lambda: {"status": "ok"})
    monkeypatch.setattr(daemon, "_start_workers", lambda n: events.append("start"))
    assert daemon.start_daemon() == {"status": "ok"}
    assert events == ["upgrade", "retire", "start"]


def legacy_queue(tmp_path):
    from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph

    store = SQLiteWorkGraph(str(tmp_path / "memory.db"))
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        conn.execute("CREATE TABLE enrichment_queue (item_id TEXT, status TEXT)")
        conn.execute("INSERT INTO enrichment_queue VALUES ('flight', 'processing')")
    return store


@pytest.mark.parametrize("failure", ["nonzero", "empty", "oserror"])
def test_failed_ps_blocks_recovery_and_replacement(
    tmp_path, monkeypatch, caplog, failure
):
    store = legacy_queue(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pidfile = tmp_path / "worker.0.pid"
        pidfile.write_text(str(process.pid))

        def inspect(command, **kwargs):
            if failure == "oserror":
                raise OSError("inspection denied")
            return subprocess.CompletedProcess(
                command, 2 if failure == "nonzero" else 0, "", "inspection failed"
            )

        monkeypatch.setattr(subprocess, "run", inspect)
        spawn = Mock()
        monkeypatch.setattr(subprocess, "Popen", spawn)
        with pytest.raises(RuntimeError, match="Cannot inspect live legacy PID"):
            daemon._start_workers()
        assert pidfile.exists()
        assert process.poll() is None
        assert store.stats()["runs"] == 0
        spawn.assert_not_called()
        assert "1 processing rows remain unrecovered" in caplog.text
    finally:
        cleanup(process)


def test_dead_legacy_pid_needs_no_ps(tmp_path, monkeypatch):
    store = legacy_queue(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        process.wait(10)
        (tmp_path / "worker.0.pid").write_text(str(process.pid))
        monkeypatch.setattr(
            subprocess, "run", Mock(side_effect=AssertionError("ps must not run"))
        )
        assert daemon._retire_legacy_workers()
        assert store.stats()["runs"] == 1
        assert not (tmp_path / "worker.0.pid").exists()
    finally:
        cleanup(process)


def test_retired_inflight_job_recovered_before_spawn(tmp_path, monkeypatch):
    store = legacy_queue(tmp_path)
    # Real child claims the pending row, simulating interruption at extraction.
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        conn.execute("UPDATE enrichment_queue SET status='pending'")
    script = (
        "import sqlite3,sys,time; "
        "c=sqlite3.connect(sys.argv[1]); "
        "c.execute(\"UPDATE enrichment_queue SET status='processing' WHERE status='pending'\"); "
        "c.commit(); print('claimed',flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "memory.db")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "claimed"
        threading.Thread(target=process.wait, daemon=True).start()
        (tmp_path / "worker.0.pid").write_text(str(process.pid))
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda command, **kw: subprocess.CompletedProcess(
                command, 0, "python -m smartmemory_app.enrichment_worker --loop", ""
            ),
        )

        def spawn(*args, **kwargs):
            assert process.poll() == -15
            assert store.stats()["runs"] == 1
            with sqlite3.connect(tmp_path / "memory.db") as conn:
                assert (
                    conn.execute("SELECT status FROM enrichment_queue").fetchone()[0]
                    == "migrated"
                )

        monkeypatch.setattr(subprocess, "Popen", spawn)
        daemon._start_workers()
        assert store.migrate_enrichment_queue(recover_processing=True) == 0
    finally:
        cleanup(process)


@pytest.mark.parametrize("bootout_ok", [True, False])
def test_legacy_launchd_without_pidfile(tmp_path, monkeypatch, caplog, bootout_ok):
    store = legacy_queue(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        threading.Thread(target=process.wait, daemon=True).start()
        plist = tmp_path / f"{daemon._LAUNCHD_WORKER_LABEL}.plist"
        plist.write_text("from smartmemory_app.enrichment_worker import main; main()")
        monkeypatch.setattr(daemon.sys, "platform", "darwin")
        states = iter([f"state = running\npid = {process.pid}\n", None])
        monkeypatch.setattr(daemon, "_legacy_agent_state", lambda: next(states))

        def bootout(label):
            if bootout_ok:
                process.terminate()
            return bootout_ok

        monkeypatch.setattr(daemon, "_launchd_bootout", bootout)
        if bootout_ok:
            assert daemon._retire_legacy_workers()
            assert process.poll() == -15
            assert store.stats()["runs"] == 1
        else:
            with pytest.raises(RuntimeError, match="positively booted out"):
                daemon._retire_legacy_workers()
            assert process.poll() is None
            assert store.stats()["runs"] == 0
            assert (tmp_path / "worker.launchd.pid").exists()
            assert "1 processing rows remain unrecovered" in caplog.text
    finally:
        cleanup(process)
