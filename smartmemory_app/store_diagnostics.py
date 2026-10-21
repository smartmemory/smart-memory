"""Read-only local store and dependency probes, run in a killable offline child."""

import contextlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

REINDEX = "Fix: smartmemory admin reindex (re-embeds saved items)"
LEXICAL = "Fix: smartmemory rebuild --lexical"
RESET = (
    "Fix: restore a backup. "
    "Last resort: smartmemory stop, then smartmemory clear --yes (deletes all local memories)"
)


def _read_db(path: Path):
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3
    )
    deadline = time.monotonic() + 1
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return contextlib.closing(connection)


def sqlite_rows(data_dir: Path) -> list[str]:
    path = data_dir / "memory.db"  # create_lite_memory's graph persistence file.
    rows = []
    for database in [path, *data_dir.glob("*.fts.db")]:
        if not database.exists():
            rows.append(
                f"SQLite: warning: {database.name} absent. Fix: smartmemory setup"
            )
            continue
        try:
            with _read_db(database) as db:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise sqlite3.DatabaseError(result)
                rows.append(
                    f"SQLite: OK ({database.name}, quick_check=ok, version={sqlite3.sqlite_version}, read-only)"
                )
        except sqlite3.Error as exc:
            rows.append(
                f"SQLite: warning: {database.name}: {type(exc).__name__}: {exc}. {RESET}"
            )
    try:
        # No virtual tables are created, even in the diagnostic process.
        with sqlite3.connect(":memory:") as db:
            db.execute("SELECT fts5_source_id()").fetchone()
        rows.append("SQLite FTS5: OK")
    except sqlite3.Error:
        rows.append(
            f"SQLite FTS5: warning: unavailable. Fix: reinstall Python with SQLite FTS5 support, then {LEXICAL}"
        )
    return rows


def _item_count(data_dir: Path) -> int | None:
    path = data_dir / "memory.db"
    if not path.exists():
        return None
    with _read_db(path) as db:
        return db.execute(
            "SELECT count(*) FROM nodes WHERE memory_type != 'Version' "
            "AND coalesce(json_extract(properties, '$.node_category'), 'memory')='memory'"
        ).fetchone()[0]


def vector_rows(data_dir: Path, expected_dimension: int | None) -> list[str]:
    """Discover collection paths from committed usearch state, including reindex snapshots."""
    from usearch.index import Index

    rows = []
    collections = []
    for path in data_dir.glob("*.fts.db"):
        with _read_db(path) as db:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec_state_%'"
            ).fetchall()
            for (table,) in tables:
                quoted = table.replace('"', '""')
                state = dict(db.execute(f'SELECT k, v FROM "{quoted}"'))
                filename = json.loads(state.get("index_file", "null"))
                dimension = json.loads(state.get("dim", "null"))
                if filename is not None and Path(filename).name != filename:
                    raise ValueError("Invalid index filename in vector state")
                collections.append((filename, dimension, state.get("collection_name")))
    # Legacy snapshots with no committed state are inspected without migration.
    if not collections:
        collections = [
            (path.name, None, path.stem) for path in data_dir.glob("*.usearch")
        ]
    if not collections:
        return [
            f"Vector file: warning: absent for configured data directory. {REINDEX}"
        ]
    items = _item_count(data_dir)
    for filename, state_dimension, collection in collections:
        if filename is None or not (data_dir / filename).is_file():
            rows.append(f"Vector file: warning: absent ({collection}). {REINDEX}")
            continue
        path = data_dir / filename
        rows.append(f"Vector file: OK ({filename})")
        try:
            # Native read-only memory map. Unicode Windows paths use immutable bytes,
            # matching the core's Unicode-safe restoration without flushing anything.
            index = Index.restore(
                path.read_bytes()
                if os.name == "nt" and not str(path).isascii()
                else str(path),
                view=True,
            )
            if index is None:
                raise ValueError("Index.restore returned None")
            dimension, vectors = index.ndim, len(index)
            rows.append(f"Vector load: OK ({collection}, read-only)")
            expected = expected_dimension if collection == "memory" else state_dimension
            if expected is None:
                rows.append(
                    f"Vector dimension: warning: configured model dimension unavailable ({dimension} stored). "
                    f"Fix: smartmemory setup"
                )
            elif dimension != expected or state_dimension not in (None, dimension):
                rows.append(
                    f"Vector dimension: warning: {dimension} stored, {expected} configured, "
                    f"{state_dimension} in state. {REINDEX}"
                )
            else:
                rows.append(f"Vector dimension: OK ({dimension})")
            if collection != "memory":
                rows.append(
                    f"Vector count: OK ({vectors} in dedicated collection {collection}, "
                    f"graph comparison applies to default collection)"
                )
            elif items is None:
                rows.append(
                    f"Vector count: warning: item count unavailable, {vectors} vectors. {REINDEX}"
                )
            elif abs(vectors - items) > max(5, items * 0.1):
                rows.append(
                    f"Vector count: warning: {vectors} vectors vs {items} items (large mismatch). {REINDEX}"
                )
            else:
                rows.append(f"Vector count: OK ({vectors} vectors vs {items} items)")
            del index
        except Exception as exc:
            rows.append(f"Vector load: warning: {type(exc).__name__}: {exc}. {REINDEX}")
    return rows


def lock_row(data_dir: Path) -> str:
    """Classify healthy configured owners separately from reset's strict preflight."""
    import psutil
    from smartmemory_app.daemon import _health_response, _data_dir
    from smartmemory_app.store_reset import _store_files
    from smartmemory.pipeline.work_graph.spawn import _is_worker_process

    verified = {}
    problems = []
    health = {}
    try:
        health = _health_response(2).json()
    except Exception:
        pass  # Ownership remains unverified and is reported below.
    for marker in [
        data_dir / "daemon.pid",
        data_dir / ".worker.pid",
        *data_dir.glob("worker.*.pid"),
    ]:
        if not marker.exists():
            continue
        try:
            pid = int(marker.read_text().strip())
            process = psutil.Process(pid)
            if (
                process.create_time() > marker.stat().st_mtime + 1
                or process.status() == psutil.STATUS_ZOMBIE
            ):
                problems.append(f"stale {marker.name} PID {pid}")
                continue
            arguments = process.cmdline()
            if marker.name == "daemon.pid":
                ours = (
                    "smartmemory_app.viewer_server" in " ".join(arguments)
                    and health.get("service") == "smartmemory"
                    and health.get("status") == "ok"
                    and health.get("pid") == pid
                    and health.get("mode") != "remote"
                    and _data_dir().resolve() == data_dir.resolve()
                    and Path(
                        process.environ().get(
                            "SMARTMEMORY_DATA_DIR", health.get("data_dir", "")
                        )
                    )
                    .expanduser()
                    .resolve()
                    == data_dir.resolve()
                )
                role = "daemon"
            else:
                environment = process.environ()
                configured = environment.get("SMARTMEMORY_DATA_DIR")
                if "--data-dir" in arguments:
                    configured = arguments[arguments.index("--data-dir") + 1]
                ours = (
                    _is_worker_process(pid, arguments)
                    and configured is not None
                    and (Path(configured).expanduser().resolve() == data_dir.resolve())
                )
                role = "worker"
            if ours:
                verified[pid] = role
            else:
                problems.append(
                    f"live store owner {marker.name} PID {pid} (not verified as our healthy {role})"
                )
        except (ValueError, psutil.NoSuchProcess):
            problems.append(f"stale or invalid {marker.name}")
        except (OSError, psutil.AccessDenied, IndexError):
            problems.append(f"unverified owner in {marker.name}")
    if os.name == "nt":
        targets = {
            os.path.normcase(str(path.resolve())) for path in _store_files(data_dir)
        }
        for process in psutil.process_iter(["pid", "name"]):
            if process.pid in verified or process.pid == os.getpid():
                continue
            try:
                if any(
                    os.path.normcase(file.path) in targets
                    for file in process.open_files()
                ):
                    problems.append(
                        f"foreign process {process.info['name']} PID {process.pid} (open store file)"
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    if problems:
        return (
            f"Write lock: warning: {', '.join(problems)}. "
            "Fix: inspect stale markers and stop conflicting owners, then retry"
        )
    if verified:
        owners = ", ".join(
            f"running SmartMemory {role}, PID {pid}"
            for pid, role in sorted(verified.items())
        )
        return f"Write lock: OK (held by the {owners})"
    if not (data_dir / ".write.lock").exists():
        return "Write lock: OK (absent)"
    return (
        "Write lock: warning: ownership unverified (lock file exists, no verified owner metadata). "
        "Fix: smartmemory stop and disconnect MCP clients before writing; no lock was acquired or deleted."
    )


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def dependency_rows(data_dir: Path):
    import keyring
    from smartmemory_app.config import load_config

    backend = keyring.get_keyring()
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    fallback = ""
    if os.name == "nt":
        from smartmemory_app.windows_credentials import key_path

        fallback = f", protected Windows fallback {'present' if key_path().is_file() else 'available (not used)'}"
    status = (
        "OK"
        if backend.priority > 0 or os.name == "nt"
        else "warning: no usable backend"
    )
    hint = ". Fix: smartmemory setup --mode remote" if status != "OK" else ""
    yield f"Keyring: {status} ({name}{fallback}){hint}"
    if os.name == "nt":
        from smartmemory_app.setup import _resolve_hook_shell

        try:
            yield f"Git Bash: OK ({_resolve_hook_shell()})"
        except Exception as exc:
            yield f"Git Bash: warning: {exc}. Fix: install Git for Windows, then smartmemory setup"
    else:
        yield "Git Bash: OK (not required on this platform)"
    from huggingface_hub import constants

    for name, directory in (
        ("data directory", data_dir),
        ("HF cache", Path(constants.HF_HUB_CACHE)),
    ):
        free = shutil.disk_usage(_existing_parent(directory)).free
        hint = (
            ". Fix: free disk space, then smartmemory setup" if free < 1024**3 else ""
        )
        yield f"Free disk {name}: {'warning: <1 GB' if free < 1024**3 else 'OK'} ({free // (1024**2)} MiB){hint}"
    import psutil

    port = load_config().daemon_port
    try:
        listeners = [
            c
            for c in psutil.net_connections(kind="tcp")
            if c.laddr.port == port and c.status == psutil.CONN_LISTEN
        ]
        for connection in listeners:
            process = psutil.Process(connection.pid) if connection.pid else None
            command = " ".join(process.cmdline()) if process else ""
            if (
                "smartmemory_app.viewer_server" in command
                or "smartmemory" in command
                and " start" in command
            ):
                yield f"Daemon port: OK ({port}, SmartMemory listener)"
            else:
                yield (
                    f"Daemon port: warning: {port} held by a non-SmartMemory or unverified process. "
                    f"Fix: choose a free SMARTMEMORY_DAEMON_PORT, then smartmemory start"
                )
        if not listeners:
            yield f"Daemon port: OK ({port}, free)"
    except (psutil.Error, OSError) as exc:
        yield (
            f"Daemon port: warning: owner unavailable ({type(exc).__name__}). "
            f"Fix: choose a free SMARTMEMORY_DAEMON_PORT, then smartmemory start"
        )


def configured_dimension(service) -> int | None:
    from smartmemory.plugins.embedding import DEFAULT_LOCAL_MODEL
    from smartmemory.utils.hf_models import canonical_id, resolve_local_path

    if canonical_id(service.local_model_name()) == DEFAULT_LOCAL_MODEL:
        from smartmemory.plugins.embedding_onnx import OnnxMiniLMEmbedder

        return OnnxMiniLMEmbedder.dimension
    try:
        backend = (service.backend_name or "").split("/", 1)[0]
        if backend == "onnxruntime":
            backend = "onnx"
        snapshot = Path(resolve_local_path(service.local_model_name(), backend=backend))
        config = json.loads((snapshot / "config.json").read_text())
        return config.get("hidden_size") or config.get("d_model")
    except (OSError, ValueError, RuntimeError):
        return None


def embedding_row(service, dimension: int | None) -> str:
    try:
        vector = service._embed_local("SmartMemory installation check")
        if vector is None or not len(vector):
            raise ValueError("No embedding produced")
        if dimension is None or len(vector) != dimension:
            return f"Embedding runtime: warning: produced {len(vector)} dimensions, expected {dimension}. {REINDEX}"
        return f"Embedding runtime: OK ({service.backend_name}, offline, {len(vector)} dimensions)"
    except (ImportError, OSError) as exc:
        return (
            f"Embedding runtime: warning: {type(exc).__name__}: {exc}. "
            f'Fix: python -m pip install --force-reinstall "smartmemory-core[onnx]", then smartmemory setup'
        )
    except Exception as exc:
        return (
            f"Embedding runtime: warning: {type(exc).__name__}: {exc}. "
            f"Fix: smartmemory setup (downloads or warms the configured model)"
        )


def diagnostic_rows(task="doctor"):
    from smartmemory_app.config import load_config
    from smartmemory_app.install_check import native_library_checks

    cfg = load_config()
    data_dir = Path(cfg.data_dir).expanduser()
    for phase, name, call in (
        ("owners", "Write lock", lambda: [lock_row(data_dir)]),
        ("sqlite", "SQLite", lambda: sqlite_rows(data_dir)),
        ("dependencies", "Dependencies", lambda: dependency_rows(data_dir)),
    ):
        if task not in ("doctor", phase):
            continue
        try:
            yield from call()
        except Exception as exc:
            yield f"{name}: warning: {type(exc).__name__}: {exc}. Fix: smartmemory doctor"
    if task in ("doctor", "native"):
        yield from native_library_checks()
    if task not in ("doctor", "vectors", "embedding"):
        return
    if cfg.embedding_provider != "local":
        yield (
            "Embedding runtime: skipped: configured non-local provider (no network or billable embedding sent)."
        )
        dimension = None
        service = None
    else:
        try:
            os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = cfg.embedding_provider
            from smartmemory.plugins.embedding import EmbeddingService

            service = EmbeddingService()
            dimension = configured_dimension(service)
        except Exception as exc:
            service, dimension = None, None
            yield f"Embedding runtime: warning: {type(exc).__name__}: {exc}. Fix: smartmemory setup"
    if task in ("doctor", "vectors"):
        try:
            yield from vector_rows(data_dir, dimension)
        except Exception as exc:
            yield f"Vector load: warning: {type(exc).__name__}: {exc}. {REINDEX}"
    if service is not None and task in ("doctor", "embedding"):
        yield embedding_row(service, dimension)
