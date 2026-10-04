"""Consume shell-only hook errors without replaying private hook input."""

import json
import logging
import time
import threading
from pathlib import Path

from filelock import FileLock, Timeout

from smartmemory_app.crash_reporter import report_exception

log = logging.getLogger(__name__)
HOOKS = frozenset({"orient", "recall", "observe", "learn", "distill", "persist"})
HINT = "Fix: smartmemory setup (repairs hook registration). Ensure its venv Scripts/bin directory is on Git Bash PATH."


class HookShellError(RuntimeError):
    """The shell reached the hook but could not run its lifecycle command."""


def marker_directory() -> Path:
    """Shell/Python marker rendezvous, independent of store configuration."""
    return Path.home() / ".smartmemory"


def consume_in_background(data_dir: Path) -> None:
    """Consume in a bounded worker, backed by durable report enqueue."""
    thread = threading.Thread(
        target=consume_failures,
        args=(data_dir,),
        name="smartmemory-hook-reports",
        daemon=True,
    )
    thread.start()
    thread.join(0.1)


def consume_failures(data_dir: Path) -> None:
    """One bounded chunk per CLI start, cross-process safe and opt-out aware."""
    path = data_dir / "hook-failures.tsv"
    if not path.is_file():
        return
    state_path = data_dir / ".hook-failure-state.json"
    try:
        with FileLock(str(state_path) + ".lock", timeout=0):
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            offset = state.get("offset", 0)
            if offset > path.stat().st_size:
                offset = 0
            recent = {
                key: stamp
                for key, stamp in state.get("recent", {}).items()
                if time.time() - stamp < 86400
            }
            with path.open("rb") as file:
                file.seek(offset)
                chunk = file.read(65536)
            # Do not consume an in-progress append from a concurrent shell hook.
            end = chunk.rfind(b"\n") + 1
            for line in chunk[:end].decode("ascii", errors="replace").splitlines():
                fields = line.split("\t")
                if len(fields) != 3 or fields[0] not in HOOKS:
                    continue
                hook, code, stamp = fields
                if (
                    not code.isdecimal()
                    or not 0 < int(code) <= 255
                    or not stamp.isdecimal()
                ):
                    continue
                signature = f"{hook} exit {code}"
                now = time.time()
                recent[signature] = now
                result = report_exception(
                    HookShellError(signature),
                    source="hook-shell",
                    args=["lifecycle"],
                    data_dir=data_dir,
                    handled=True,
                    dedupe_key=f"hook:{hook}:{code}:{time.strftime('%Y-%m-%d', time.gmtime(now))}",
                    dedupe_seconds=86400,
                )
                if result.status not in {"sent", "queued", "suppressed", "disabled"}:
                    return  # Retain this chunk if durable enqueue itself failed.
            temporary = state_path.with_suffix(".tmp")
            try:
                temporary.write_text(
                    json.dumps({"offset": offset + end, "recent": recent}),
                    encoding="utf-8",
                )
                temporary.replace(state_path)
            finally:
                temporary.unlink(missing_ok=True)
    except Timeout:
        pass  # The CLI holding the consumer lock owns this chunk.
    except (OSError, ValueError) as error:
        log.warning(
            "Hook failure markers could not be consumed (%s)", type(error).__name__
        )


def recent_failures(data_dir: Path) -> list[str]:
    """Read pending marker lines and history without locking or consumption."""
    state_path = data_dir / ".hook-failure-state.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        recent = {
            key
            for key, stamp in state.get("recent", {}).items()
            if time.time() - stamp < 86400
        }
        path = data_dir / "hook-failures.tsv"
        if path.exists():
            with path.open("rb") as file:
                offset = state.get("offset", 0)
                file.seek(offset if offset <= path.stat().st_size else 0)
                chunk = file.read(65536)
            for line in (
                chunk[: chunk.rfind(b"\n") + 1]
                .decode("ascii", errors="replace")
                .splitlines()
            ):
                fields = line.split("\t")
                if (
                    len(fields) == 3
                    and fields[0] in HOOKS
                    and fields[1].isdecimal()
                    and 0 < int(fields[1]) <= 255
                    and fields[2].isdecimal()
                ):
                    recent.add(f"{fields[0]} exit {fields[1]}")
        return [f"Hooks: warning: {key}. {HINT}" for key in sorted(recent)] or [
            "Hooks: OK, no recent shell failures"
        ]
    except (OSError, ValueError) as error:
        return [f"Hooks: warning: history unavailable ({type(error).__name__}). {HINT}"]
