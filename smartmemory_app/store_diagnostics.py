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
    """Reuse reset's owner validation and inspect existing locks without creating them."""
    from smartmemory_app.store_reset import _check_owners, _store_files

    owners = ""
    try:
        _check_owners(data_dir, _store_files(data_dir))
    except RuntimeError as exc:
        owners = f" ({exc})"
    path = data_dir / ".write.lock"
    if not path.exists():
        if owners:
            return (
                f"Write lock: warning: live store owner{owners}. "
                f"Fix: smartmemory stop and disconnect MCP clients, then retry"
            )
        return "Write lock: OK (absent)"
    if owners:
        return (
            f"Write lock: warning: live store owner{owners}. "
            f"Fix: smartmemory stop and disconnect MCP clients, then retry"
        )
    # Windows byte-range locks and POSIX flock have no portable non-acquiring
    # ownership query. F_GETLK queries POSIX record locks, not filelock's flock.
    # An existing file alone cannot establish either liveness or staleness.
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
    yield (
        f"Keyring: {status} ({name}{fallback}). Fix: smartmemory setup --mode remote"
    )
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
        yield (
            f"Free disk {name}: {'warning: <1 GB' if free < 1024**3 else 'OK'} ({free // (1024**2)} MiB). "
            f"Fix: free disk space, then smartmemory setup"
        )
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
        snapshot = Path(
            resolve_local_path(service.local_model_name(), backend=service.backend_name)
        )
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


def diagnostic_rows():
    from smartmemory_app.config import load_config
    from smartmemory_app.install_check import native_library_checks

    cfg = load_config()
    data_dir = Path(cfg.data_dir).expanduser()
    for name, call in (
        ("Write lock", lambda: [lock_row(data_dir)]),
        ("SQLite", lambda: sqlite_rows(data_dir)),
        ("Dependencies", lambda: dependency_rows(data_dir)),
    ):
        try:
            yield from call()
        except Exception as exc:
            yield f"{name}: warning: {type(exc).__name__}: {exc}. Fix: smartmemory doctor"
    yield from native_library_checks()
    if cfg.embedding_provider != "local":
        yield (
            "Embedding runtime: skipped: configured non-local provider (no network or billable embedding sent). "
            "Fix: smartmemory setup"
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
    try:
        yield from vector_rows(data_dir, dimension)
    except Exception as exc:
        yield f"Vector load: warning: {type(exc).__name__}: {exc}. {REINDEX}"
    if service is not None:
        yield embedding_row(service, dimension)
