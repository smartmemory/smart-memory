"""Read-only diagnostics against real SQLite, native indexes and OS locks."""

import hashlib
import sqlite3

import pytest

from smartmemory_app.diagnostic_process import offline_checks as real_offline_checks


def make_store(path, dimension=384, items=1):
    from smartmemory.stores.vector.backends.usearch import UsearchVectorBackend

    path.mkdir(exist_ok=True)
    with sqlite3.connect(path / "memory.db") as db:
        db.execute(
            "CREATE TABLE nodes(item_id TEXT, memory_type TEXT, properties TEXT)"
        )
        db.executemany(
            "INSERT INTO nodes VALUES (?, 'semantic', '{\"node_category\":\"memory\"}')",
            [(str(i),) for i in range(items)],
        )
    with UsearchVectorBackend(
        collection_name="memory", persist_directory=str(path)
    ) as vector:
        vector.add(
            item_id="0",
            embedding=[1.0] + [0.0] * (dimension - 1),
            metadata={"content": "test"},
        )
    return path


def test_vector_diagnostics_readonly_and_persisted_path(tmp_path):
    from smartmemory_app.store_diagnostics import vector_rows, sqlite_rows

    path = make_store(tmp_path)
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.iterdir()
        if p.is_file()
    }
    rows = vector_rows(path, 384)
    assert any("Vector load: OK" in row for row in rows)
    assert any("Vector dimension: OK" in row for row in rows)
    assert any("Vector count: OK" in row for row in rows)
    assert any("SQLite: OK" in row for row in sqlite_rows(path))
    after = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.iterdir()
        if p.is_file()
    }
    assert before == after


@pytest.mark.parametrize("mode", ["corrupt", "dimension", "count", "absent"])
def test_vector_failures_have_existing_repair_command(tmp_path, mode):
    from smartmemory_app.store_diagnostics import vector_rows

    path = make_store(
        tmp_path,
        dimension=3 if mode == "dimension" else 384,
        items=30 if mode == "count" else 1,
    )
    index = next(path.glob("*.usearch"))
    if mode == "corrupt":
        index.write_bytes(b"broken index")
    elif mode == "absent":
        index.unlink()
    rows = "\n".join(vector_rows(path, 384))
    prefix = {
        "corrupt": "Vector load",
        "dimension": "Vector dimension",
        "count": "Vector count",
        "absent": "Vector file",
    }[mode]
    assert f"{prefix}: warning:" in rows
    assert "smartmemory admin reindex" in rows


def test_write_lock_active_and_stale(tmp_path):
    import os
    from filelock import FileLock
    from smartmemory_app.store_diagnostics import lock_row

    lock = FileLock(tmp_path / ".write.lock")
    with lock:
        (tmp_path / ".worker.pid").write_text(str(os.getpid()))
        assert "Write lock: warning: live" in lock_row(tmp_path)
    (tmp_path / ".worker.pid").unlink()
    assert "ownership unverified" in lock_row(tmp_path)


def test_sqlite_corruption_has_explicit_reset_warning(tmp_path):
    from smartmemory_app.store_diagnostics import sqlite_rows

    (tmp_path / "memory.db").write_bytes(b"not SQLite")
    rows = "\n".join(sqlite_rows(tmp_path))
    assert "SQLite: warning:" in rows
    assert "smartmemory clear --yes" in rows and "deletes" in rows


def test_offline_probe_deadline_kills_blocking_native_import(monkeypatch, tmp_path):
    import os
    import time
    import psutil

    shadow = tmp_path / "blocking"
    package = shadow / "usearch"
    package.mkdir(parents=True)
    pid_file = tmp_path / "test_F_probe.pid"
    (package / "__init__.py").write_text(
        f"import os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )
    monkeypatch.setenv(
        "PYTHONPATH", str(shadow) + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    started = time.monotonic()
    rows = real_offline_checks("native", budget=0.5)
    assert time.monotonic() - started < 2
    assert "timed out" in rows[-1]
    assert pid_file.exists()
    assert not psutil.pid_exists(int(pid_file.read_text()))
