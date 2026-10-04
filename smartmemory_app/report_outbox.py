"""Durable privacy-filtered reports at the fixed hook-marker rendezvous."""

import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

import psutil
from filelock import FileLock

log = logging.getLogger(__name__)
MAX_REPORTS = 50
MAX_AGE = 14 * 86400
MAX_CORRUPT = 10
# At least twice the report transport's 5-second timeout, with contention headroom.
CLAIM_LEASE_SECONDS = 60


def directory() -> Path:
    from smartmemory_app.hook_failures import marker_directory

    return marker_directory() / "report-outbox"


def queued_count() -> int:
    """Read-only, including in-flight and expired entries until a sender prunes."""
    root = directory()
    return len(list(root.glob("*.json"))) + len(list(root.glob("*.sending")))


def _atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _quarantine(path: Path) -> None:
    """Called under the queue lock. Keep a bounded, inspectable failure record."""
    path.rename(path.with_name(path.name + "." + uuid.uuid4().hex + ".corrupt"))
    log.warning("Quarantined malformed report state/envelope: %s", path.name)
    files = sorted(path.parent.glob("*.corrupt"), key=lambda p: p.stat().st_mtime)
    for old in files[:-MAX_CORRUPT]:
        old.unlink(missing_ok=True)


def _stamp(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _timestamps(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and _stamp(v) for k, v in value.items()
    )


def _state(root: Path, name: str) -> dict:
    path = root / name
    default = {"day": "", "count": 0, "recent": {}} if name == ".reservations" else {}
    if not path.exists():
        return default
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if name == ".reservations":
            valid = (
                isinstance(state, dict)
                and isinstance(state.get("day"), str)
                and type(state.get("count")) is int
                and state["count"] >= 0
                and _timestamps(state.get("recent"))
            )
        else:
            valid = _timestamps(state)
        if not valid:
            raise ValueError("invalid report state schema")
        return state
    except (ValueError, TypeError):
        _quarantine(path)
        return default


def _envelope(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            raise ValueError("invalid envelope")
        props = value["payload"].get("properties")
        if (
            not isinstance(props, dict)
            or not isinstance(props.get("report_id"), str)
            or not props["report_id"]
        ):
            raise ValueError("invalid report identity")
        value.setdefault("created", path.stat().st_mtime)
        if not _stamp(value["created"]):
            raise ValueError("invalid report timestamp")
        return value
    except (ValueError, TypeError):
        _quarantine(path)
        return None


def _remember(root: Path, fingerprint: str, created: float) -> None:
    """Reconstruct dedupe from the report before its last signature is removed."""
    now = time.time()
    state = _state(root, ".reservations")
    state["recent"] = {k: v for k, v in state["recent"].items() if now - v < 86400}
    state["recent"][fingerprint] = max(created, state["recent"].get(fingerprint, 0))
    _atomic(root / ".reservations", state)


def _claim_parts(path: Path) -> tuple[str, str, str, float]:
    identity, lease = path.stem.rsplit(".", 1)
    if "-" in lease:
        claimed_at = int(lease.split("-", 1)[0]) / 1_000_000
    else:
        # Claims written before leases used only PID + process creation time.
        identity, claimed_at = path.stem, path.stat().st_mtime
    fingerprint, pid, started = identity.rsplit(".", 2)
    return fingerprint, pid, started, claimed_at


def _claim_alive(path: Path) -> bool:
    # PID + creation time avoid PID reuse. A bounded lease recovers lost results.
    _, pid, started, claimed_at = _claim_parts(path)
    if time.time() - claimed_at >= CLAIM_LEASE_SECONDS:
        return False
    try:
        process = psutil.Process(int(pid))
        return (
            abs(process.create_time() - int(started) / 1_000_000) < 0.01
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (ValueError, psutil.NoSuchProcess):
        return False
    except psutil.AccessDenied:
        return True  # A live but uninspectable sender retains its claim.


def _pending(root: Path) -> list[Path]:
    """Recover dead/expired senders and remove acknowledged files under the queue lock."""
    receipts = _state(root, ".delivered")
    files = sorted(
        [*root.glob("*.json"), *root.glob("*.sending")], key=lambda p: p.stat().st_mtime
    )
    pending = []
    for path in files:
        envelope = _envelope(path)
        if envelope is None:
            continue
        try:
            fingerprint = (
                _claim_parts(path)[0] if path.suffix == ".sending" else path.stem
            )
        except ValueError:
            _quarantine(path)
            continue
        if envelope["payload"]["properties"]["report_id"] in receipts:
            _remember(root, fingerprint, envelope["created"])
            path.unlink(missing_ok=True)
            continue
        if path.suffix == ".sending":
            try:
                if _claim_alive(path):
                    continue
            except ValueError:
                _quarantine(path)
                continue
            destination = root / (fingerprint + ".json")
            path.rename(destination)
            path = destination
        if time.time() - path.stat().st_mtime > MAX_AGE:
            path.unlink(missing_ok=True)
        else:
            pending.append(path)
    return pending


def enqueue(payload: dict, fingerprint: str, dedupe_seconds: int) -> tuple[dict, bool]:
    """Persist first, then reserve. A pending/in-flight identity is reused."""
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".queue.lock"), timeout=0.1):
        files = _pending(root)
        for path in [*files, *root.glob(fingerprint + ".*.sending")]:
            if path.stem == fingerprint or path.suffix == ".sending":
                envelope = _envelope(path)
                if envelope is not None:
                    return envelope["payload"], True
        now = time.time()
        state = _state(root, ".reservations")
        recent = {k: v for k, v in state["recent"].items() if now - v < 86400}
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        count = state["count"] if state["day"] == day else 0
        if (
            fingerprint in recent and now - recent[fingerprint] < dedupe_seconds
        ) or count >= 20:
            return payload, False
        claims = len(list(root.glob("*.sending")))
        if claims >= MAX_REPORTS:
            raise OSError("report outbox busy")  # Retain the marker for retry.
        for old in files[: max(0, len(files) + claims - MAX_REPORTS + 1)]:
            old.unlink(missing_ok=True)
        _atomic(root / (fingerprint + ".json"), {"payload": payload, "created": now})
        recent[fingerprint] = now
        _atomic(
            root / ".reservations", {"day": day, "count": count + 1, "recent": recent}
        )
        return payload, True


def flush(post, allowed) -> set[str]:
    """One mutation lock, immutable claims, no network work under the lock."""
    root = directory()
    if not root.exists():
        return set()
    delivered = set()
    with FileLock(str(root / ".queue.lock"), timeout=0.1):
        if not allowed():
            for path in [*root.glob("*.json"), *root.glob("*.sending")]:
                path.unlink(missing_ok=True)
            return set()
        files = _pending(root)
    for path in files:
        claim = None
        try:
            with FileLock(str(root / ".queue.lock"), timeout=0.1):
                if not path.exists():
                    continue  # Another sender claimed or retention removed it.
                if not allowed():
                    path.unlink(missing_ok=True)
                    continue
                envelope = _envelope(path)
                if envelope is None:
                    continue
                _remember(root, path.stem, envelope["created"])
                if envelope["payload"]["properties"]["report_id"] in _state(
                    root, ".delivered"
                ):
                    path.unlink(missing_ok=True)
                    continue
                started = int(psutil.Process().create_time() * 1_000_000)
                lease = f"{int(time.time() * 1_000_000)}-{uuid.uuid4().hex}"
                claim = root / f"{path.stem}.{os.getpid()}.{started}.{lease}.sending"
                path.rename(claim)
            try:
                success = post(envelope["payload"])
            except Exception as exc:
                log.warning("Queued report send failed (%s)", type(exc).__name__)
                success = False
            with FileLock(str(root / ".queue.lock"), timeout=0.1):
                if not claim.exists():
                    continue  # Opt-out or lease recovery removed this exact claim.
                if success:
                    report_id = envelope["payload"]["properties"]["report_id"]
                    receipts = _state(root, ".delivered")
                    receipts = {
                        k: v for k, v in receipts.items() if time.time() - v < MAX_AGE
                    }
                    receipts[report_id] = time.time()
                    _atomic(
                        root / ".delivered",
                        dict(
                            sorted(receipts.items(), key=lambda item: item[1])[
                                -MAX_REPORTS:
                            ]
                        ),
                    )
                    delivered.add(report_id)
                    claim.unlink(missing_ok=True)
                else:
                    claim.rename(path)
        except OSError as exc:
            log.warning("Queued report unavailable (%s)", type(exc).__name__)
    return delivered


def was_delivered(report_id: str) -> bool:
    """Receipt lookup with malformed bookkeeping recovery under the queue lock."""
    root = directory()
    if not root.exists():
        return False
    with FileLock(str(root / ".queue.lock"), timeout=0.1):
        return report_id in _state(root, ".delivered")
