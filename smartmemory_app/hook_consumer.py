"""The one consumer of the hook write queue (HOOK-WRITE-QUEUE-1, daemon-sole-consumer).

The observe, learn and distill hooks only append jobs to ``<data dir>/hook-queue/``
(pure bash, ``capture_queue`` documents the layout). In local mode the daemon is the
only process that reads them: :func:`start_in_daemon` runs :func:`consume` on its own
thread, off the event loop, and each job runs the write phase in-process against the
daemon's store. One process, one writer. Nothing goes over HTTP and no hook process
writes the store. While the daemon is down the jobs wait on disk (the hook sheds past
``SMARTMEMORY_HOOK_QUEUE_MAX`` with a WARNING) and drain when it starts again.

Remote mode has no daemon (``setup`` wires hooks without one), so there the hooks
start ``smartmemory lifecycle drain-hooks`` (:func:`run_remote_drainer`), which runs
the same :func:`consume` loop until the queue stays idle. Its writes go to the hosted
service, not to a local store. The hooks start it only when ``remote-drain`` exists in
the queue dir; ``setup`` remote mode creates that marker and the daemon removes it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from filelock import FileLock, Timeout

from smartmemory_app import capture_queue as queue

log = logging.getLogger(__name__)

# HOOK-LOAD-CAP-1's per-phase hook caps (observe 3, learn 2, distill 2) moved here:
# the queue is drained by at most this many concurrent jobs, nothing is dropped.
DEFAULT_HOOK_PARALLEL = 3
DEFAULT_HOOK_MAX_ATTEMPTS = 5
DEFAULT_HOOK_IDLE_EXIT = 20.0
LOCK_RETRY_SECONDS = 5.0


def _env_number(name: str, default, cast, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("%s=%r is not a number, using %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%r is below %s, using %s", name, raw, minimum, default)
        return default
    return value


def execute_hook_job(job: dict) -> None:
    """Run one queued phase in this process: the mapping the daemon's /lifecycle routes use.

    Raises PermanentHookError for a payload that can never succeed; any other
    exception (a store failure, surfaced by ``strict``) is retried.
    """
    from smartmemory_app.lifecycle import (
        STRICT_KEY,
        USER_TURN_KEY,
        WORKSPACE_KEY,
        run_phase,
    )
    from smartmemory_app.lifecycle_api import _get_lifecycle

    try:
        raw = Path(job["payload_path"]).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise queue.PermanentHookError(f"payload file missing: {exc}") from exc
    except (OSError, UnicodeDecodeError, TypeError, KeyError) as exc:
        raise queue.PermanentHookError(f"payload unreadable: {exc}") from exc
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise queue.PermanentHookError(f"payload is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise queue.PermanentHookError("payload is not a JSON object")
    request = {**body, STRICT_KEY: True}
    if job.get("workspace_id"):
        request[WORKSPACE_KEY] = job["workspace_id"]
    if job["phase"] == "distill":
        # The prompt this response answers, as the session state held it when the
        # Stop hook fired. Reading live state here would pair it with a later prompt.
        user_turn = ""
        if job.get("state_snapshot"):
            try:
                state = json.loads(
                    Path(job["state_snapshot"]).read_text(encoding="utf-8")
                )
                user_turn = (
                    state.get("current_user_turn") if isinstance(state, dict) else None
                ) or ""
            except (OSError, ValueError) as exc:
                log.warning(
                    "Distill prompt snapshot unreadable, pairing the response with no prompt: %s",
                    exc,
                )
        request[USER_TURN_KEY] = user_turn
    run_phase(_get_lifecycle(request), job["phase"], request)


def _write_pid(root: Path) -> None:
    pid_path = root / "worker.pid"
    temporary = pid_path.with_name(f".worker.pid.{os.getpid()}.tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="utf-8")
    temporary.replace(pid_path)


def _clear_pid(root: Path) -> None:
    pid_path = root / "worker.pid"
    try:
        if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink()
    except (OSError, ValueError):
        pass


def _holder_alive(root: Path) -> bool:
    from smartmemory.utils.process import (
        pid_alive,
    )  # never signals (os.kill(pid, 0) kills on Windows)

    try:
        pid = int((root / "worker.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def _settle(root: Path, inflight: dict, max_attempts: int) -> int:
    finished = 0
    for future in [f for f in inflight if f.done()]:
        job = inflight.pop(future)
        finished += 1
        try:
            future.result()
        except queue.PermanentHookError as exc:
            queue.fail_hook_job(
                root, job, str(exc), max_attempts=max_attempts, permanent=True
            )
        except Exception as exc:  # noqa: BLE001 - every other failure is retried
            queue.fail_hook_job(
                root, job, f"{type(exc).__name__}: {exc}", max_attempts=max_attempts
            )
        else:
            queue.complete_hook_job(job)
    return finished


def consume(
    root: str | Path | None = None,
    *,
    execute=execute_hook_job,
    stop: threading.Event | None = None,
    parallel: int | None = None,
    max_attempts: int | None = None,
    idle_exit: float | None = None,
    poll: float = 0.1,
) -> int:
    """Run queued hook jobs until ``stop`` is set, or the queue stays empty ``idle_exit`` seconds.

    One consumer per queue (``worker.lock``); jobs of one session run strictly in
    queue order, at most ``parallel`` sessions at once. Returns jobs finished.
    ``idle_exit=None`` never exits on idle (the daemon). With an idle exit (the
    remote drainer) the handshake with the hooks, which spawn a drainer only when
    worker.pid names no live process, holds: the holder unlinks worker.pid, then
    rescans before it exits, so a record published meanwhile is never stranded.
    """
    root = queue.hook_queue_dir(root)
    stop = stop or threading.Event()
    parallel = parallel or _env_number(
        "SMARTMEMORY_HOOK_DRAIN_MAX", DEFAULT_HOOK_PARALLEL, int, 1
    )
    max_attempts = max_attempts or _env_number(
        "SMARTMEMORY_HOOK_MAX_ATTEMPTS", DEFAULT_HOOK_MAX_ATTEMPTS, int, 1
    )
    lock = FileLock(str(root / "worker.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if idle_exit is not None:
            if _holder_alive(root):
                return (
                    0  # the live holder rescans after our hook's record was published
                )
            lock.acquire()  # the holder is exiting; take over once it lets go
            idle_exit = 0.0 if not queue.hook_jobs(root) else idle_exit
        else:
            log.warning(
                "Hook queue %s is held by another consumer (worker.lock); hook jobs wait until it lets go",
                root,
            )
            while not stop.is_set():
                try:
                    lock.acquire(timeout=0)
                    break
                except Timeout:
                    stop.wait(LOCK_RETRY_SECONDS)
            else:
                return 0
    finished = 0
    try:
        queue.recover_running(root, max_attempts=max_attempts)
        _write_pid(root)
        inflight: dict = {}
        idle_since = None
        with ThreadPoolExecutor(
            max_workers=parallel, thread_name_prefix="smartmemory-hook-job"
        ) as pool:
            while True:
                finished += _settle(root, inflight, max_attempts)
                if stop.is_set():
                    if not inflight:
                        return finished
                    wait(list(inflight), timeout=poll, return_when=FIRST_COMPLETED)
                    continue
                queue.adopt_spill(
                    root
                )  # HOOK-SPILL-INTERIM-1's spilled payloads join the queue
                pending = queue.hook_jobs(root)
                busy = {job["session_id"] for job in inflight.values()}
                now = time.time()
                for job in pending:
                    if len(inflight) >= parallel:
                        break
                    session = job["session_id"]
                    if session in busy:
                        continue
                    busy.add(session)  # only the oldest job of a session may start
                    if job.get("not_before", 0) > now:
                        continue
                    claimed = queue.claim_hook_job(root, job)
                    if claimed is not None:
                        inflight[pool.submit(execute, claimed)] = claimed
                if inflight:
                    idle_since = None
                    wait(list(inflight), timeout=poll, return_when=FIRST_COMPLETED)
                    continue
                if pending or idle_exit is None:
                    idle_since = None
                    stop.wait(poll)  # backoff-delayed jobs only, or the daemon idling
                    continue
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since < idle_exit:
                    time.sleep(poll)
                    continue
                _clear_pid(root)
                if queue.hook_jobs(root):
                    _write_pid(root)
                    idle_since = None
                    continue
                return finished
    finally:
        _clear_pid(root)
        lock.release()


def run_remote_drainer(root: str | Path | None = None, **kwargs) -> int:
    """``smartmemory lifecycle drain-hooks``: remote mode only. Local mode's consumer is the daemon."""
    from smartmemory_app.config import load_config

    if load_config().mode != "remote":
        log.warning(
            "drain-hooks runs in remote mode only; in local mode the daemon is the one "
            "consumer of the hook queue (start it with `smartmemory start`)"
        )
        return 0
    # The drainer serves every session, so the spawning session's pin is not the
    # job's: each record carries the pin its own hook saw (workspace_id).
    os.environ.pop("SMARTMEMORY_WORKSPACE_ID", None)
    if "idle_exit" not in kwargs:
        kwargs["idle_exit"] = _env_number(
            "SMARTMEMORY_HOOK_WORKER_IDLE", DEFAULT_HOOK_IDLE_EXIT, float, 0.0
        )
    return consume(root, **kwargs)


def set_remote_drain(enabled: bool, root: str | Path | None = None) -> None:
    """Create or remove the marker that tells the hooks to start the remote drainer."""
    marker = queue.hook_queue_dir(root) / "remote-drain"
    if enabled:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)


class DaemonConsumer:
    """The daemon's consumer thread; ``stop()`` waits for in-flight jobs before the store closes."""

    def __init__(self, root: str | Path | None = None, **kwargs) -> None:
        self.root = queue.hook_queue_dir(root)
        self._stop = threading.Event()
        self._kwargs = kwargs
        self.thread = threading.Thread(
            target=self._run, name="smartmemory-hook-consumer", daemon=True
        )

    def _run(self) -> None:
        try:
            consume(self.root, stop=self._stop, idle_exit=None, **self._kwargs)
        except Exception:  # noqa: BLE001 - a dead consumer strands every hook write
            log.warning(
                "Hook queue consumer stopped on an error; hook jobs wait on disk until the daemon restarts",
                exc_info=True,
            )

    def start(self) -> "DaemonConsumer":
        set_remote_drain(False, self.root)
        counts = queue.hook_counts(self.root)
        try:
            counts["spill"] = sum(
                1
                for n in os.listdir(queue.hook_spill_dir(self.root))
                if n.endswith(".json") and not n.startswith(".")
            )
        except FileNotFoundError:
            counts["spill"] = 0
        log.info("Hook queue consumer starting on %s: %s", self.root, counts)
        self.thread.start()
        return self

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout)
        if self.thread.is_alive():
            log.warning(
                "Hook queue consumer still running after %.0fs at shutdown; its jobs stay in running/ and are retried on the next start",
                timeout,
            )


def start_in_daemon(**kwargs) -> DaemonConsumer:
    return DaemonConsumer(**kwargs).start()
