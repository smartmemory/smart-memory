"""Preflight every store owner and file before resetting any persisted storage."""

import contextlib
import logging
import os
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

log = logging.getLogger(__name__)
_PATTERNS = (
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.db-journal",
    "*.usearch",
    "*.json",
    "*.jsonl",
)


def _store_files(data_path: Path) -> list[Path]:
    return sorted({path for pattern in _PATTERNS for path in data_path.glob(pattern)})


def _refusal(detail: str) -> RuntimeError:
    return RuntimeError(
        f"Store reset refused: {detail}. Run smartmemory stop first and disconnect MCP clients."
    )


def _check_owners(data_path: Path, files: list[Path]) -> None:
    owners = set()
    for marker in [
        data_path / "daemon.pid",
        data_path / ".worker.pid",
        *data_path.glob("worker.*.pid"),
    ]:
        if not marker.exists():
            continue
        try:
            pid = int(marker.read_text().strip())
            process = psutil.Process(pid)
            # A process born after this marker cannot be its original owner.
            if process.create_time() > marker.stat().st_mtime + 1:
                continue
            if process.status() != psutil.STATUS_ZOMBIE:
                owners.add(f"{marker.name} PID {pid}")
        except (ValueError, psutil.NoSuchProcess):
            continue
        except (OSError, psutil.AccessDenied):
            owners.add(f"unverified owner in {marker.name}")
    # MCP clients have no PID marker. Windows' open-file check complements the
    # authoritative markers, and the exclusive-open pass catches inaccessible owners.
    if os.name == "nt":
        targets = {os.path.normcase(str(path.resolve())) for path in files}
        for process in psutil.process_iter(["pid", "name"]):
            if (
                not (process.info["name"] or "")
                .lower()
                .startswith(("python", "smartmemory"))
            ):
                continue
            try:
                if any(
                    os.path.normcase(file.path) in targets
                    for file in process.open_files()
                ):
                    owners.add(
                        f"{process.info['name']} PID {process.pid} (open store file)"
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    if owners:
        raise _refusal("live owners: " + ", ".join(sorted(owners)))


@contextlib.contextmanager
def _exclusive_files(files: list[Path]):
    """Hold all exclusive Windows DELETE handles until deletion is committed."""
    if os.name != "nt":
        # POSIX permits unlinking open files. Validate access before any unlink.
        with contextlib.ExitStack() as stack:
            for path in files:
                stack.enter_context(path.open("r+b"))
            yield None
        return

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    dispose = kernel.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    handles = []
    marked = []
    try:
        for path in files:
            # DELETE | GENERIC_READ, share mode 0, OPEN_EXISTING. A live SQLite
            # reader or MCP process makes this fail BEFORE any file is marked.
            handle = create(
                str(path.resolve()), 0x00010000 | 0x80000000, 0, None, 3, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(
                    ctypes.get_last_error(), f"busy or inaccessible file {path.name}"
                )
            handles.append(handle)

        def delete_all():
            for handle in handles:
                # FileDispositionInfo (4) contains a native BOOLEAN, one byte.
                flag = ctypes.c_ubyte(1)
                if not dispose(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
                    raise ctypes.WinError(ctypes.get_last_error())
                marked.append(handle)

        yield delete_all
    except BaseException:
        # All handles are still open. Cancel pending deletions before releasing
        # ownership, so failure on a later file cannot discard earlier siblings.
        for handle in marked:
            flag = ctypes.c_ubyte(0)
            if not dispose(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
                log.error(
                    "Could not cancel pending store deletion: Windows error %s",
                    ctypes.get_last_error(),
                )
        raise
    finally:
        for handle in handles:
            close(handle)


def preflight_store_reset(data_path: Path) -> None:
    """Refuse live owners and inaccessible files before closing API storage."""
    files = _store_files(data_path)
    _check_owners(data_path, files)
    try:
        with _exclusive_files(files):
            pass
    except OSError as exc:
        raise _refusal(str(exc)) from exc


def remove_store_files(data_path: Path) -> int:
    """Acquire maintenance ownership, preflight all files, then remove them."""
    _check_owners(data_path, _store_files(data_path))
    try:
        with (
            FileLock(data_path / ".write.lock", timeout=0),
            FileLock(data_path / ".worker.lock", timeout=0),
        ):
            files = _store_files(data_path)
            _check_owners(data_path, files)
            try:
                with _exclusive_files(files) as delete_all:
                    if delete_all is not None:
                        delete_all()
                    else:
                        failed = []
                        for path in files:
                            try:
                                path.unlink()
                            except OSError as exc:
                                log.warning("Clear retained %s: %s", path.name, exc)
                                failed.append(path.name)
                        if failed:
                            raise RuntimeError(
                                f"Clear incomplete: retained {', '.join(failed)}. Run smartmemory stop first."
                            )
            except OSError as exc:
                raise _refusal(str(exc)) from exc
            remaining = [path.name for path in files if path.exists()]
            if remaining:
                raise RuntimeError(
                    f"Clear incomplete: retained {', '.join(remaining)}. Run smartmemory stop first."
                )
            return len(files)
    except Timeout as exc:
        raise _refusal("live daemon or worker holds maintenance ownership") from exc
