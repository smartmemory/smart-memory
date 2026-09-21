"""DIST-UPDATE-HINT-1: once-a-day update hint + first-run / upgrade tour nudge.

Two independent, purely additive notices printed after a `sm` command:

1. **Update available** — the newest `smartmemory` version on PyPI, refreshed at
   most once every 24 h and cached in ``<data_dir>/.update-check.json``.
2. **Upgrade notice** — the wrapper version changed since the last `sm` run, read
   from ``<data_dir>/.last-seen-version``. The `sm tour` nudge is appended only
   when :data:`smartmemory_app.tour.TOUR_VERSION` also changed, so a routine
   patch upgrade does not re-advertise a tour the user already saw.

The update check is the one place in this codebase where a silent failure is the
correct behaviour (it runs after EVERY command, so a WARNING on a flaky network
would be pure noise). It is therefore explicitly exempted from the
no-silent-degradation rule: every failure path logs at DEBUG and returns ``None``.
Nothing here ever raises into a command's exit path.

Suppressed entirely (no network, no file writes, no output) when:
  - ``SMARTMEMORY_NO_UPDATE_CHECK`` is truthy,
  - stdout is not a TTY (hooks, pipes, CI),
  - the command is a hook entry point that feeds a model prompt (`sm recall`,
    `sm lifecycle …`) or asked for machine-readable output (``--json``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

log = logging.getLogger(__name__)

PYPI_JSON_URL = "https://pypi.org/pypi/smartmemory/json"
FETCH_TIMEOUT_SECONDS = 2.0
CHECK_INTERVAL_SECONDS = 24 * 60 * 60

UPDATE_CACHE_FILENAME = ".update-check.json"
LAST_SEEN_FILENAME = ".last-seen-version"

NO_UPDATE_CHECK_ENV = "SMARTMEMORY_NO_UPDATE_CHECK"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Commands whose stdout is consumed by a model prompt or a machine. A trailing
# hint here corrupts the payload, so the whole notice path is skipped.
SUPPRESSED_COMMANDS = frozenset({"recall", "lifecycle"})


def version_lt(a: str, b: str) -> bool:
    """Return True if version string ``a`` is strictly less than ``b``.

    Prefers packaging.version (an indirect dep) for PEP 440 correctness; falls
    back to a tuple-of-ints compare so nothing hard-fails on a missing dep.
    """
    try:
        from packaging.version import Version

        return Version(a) < Version(b)
    except Exception:

        def _tuple(v: str) -> tuple[int, ...]:
            # Take the leading numeric release segment ("1.4.32rc1" -> (1, 4, 32)).
            parts: list[int] = []
            for chunk in v.split(".")[:3]:
                num = ""
                for ch in chunk:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                parts.append(int(num) if num else 0)
            return tuple(parts)

        return _tuple(a) < _tuple(b)


def is_disabled() -> bool:
    """True when the user opted out via ``SMARTMEMORY_NO_UPDATE_CHECK``."""
    return os.environ.get(NO_UPDATE_CHECK_ENV, "").strip().lower() in _TRUTHY


def data_dir() -> Path:
    """The SmartMemory data dir (``~/.smartmemory`` unless overridden)."""
    from smartmemory_app.storage import _resolve_data_dir

    return _resolve_data_dir()


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # missing / unreadable / corrupt — all recoverable
        log.debug("update-check: could not read %s: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.debug("update-check: could not write %s: %s", path, exc)


def _fetch_latest() -> str | None:
    """One HTTP GET against the PyPI JSON API. None on any failure."""
    try:
        with urlopen(PYPI_JSON_URL, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        version = payload.get("info", {}).get("version")
        return version if isinstance(version, str) and version else None
    except Exception as exc:
        log.debug("update-check: PyPI lookup failed: %s", exc)
        return None


def latest_version(*, now: float | None = None, force: bool = False) -> str | None:
    """Newest `smartmemory` version on PyPI, at most one network call per 24 h.

    Set ``force`` for an explicit user-requested check that bypasses the cache.
    Returns None when opted out, when the network call fails, or when a previous
    failure is still inside its 24 h back-off window.
    """
    if is_disabled():
        return None
    stamp = time.time() if now is None else now
    cache_path = data_dir() / UPDATE_CACHE_FILENAME

    cached = None if force else _read_json(cache_path)
    if cached is not None:
        checked_at = cached.get("checked_at")
        if (
            isinstance(checked_at, (int, float))
            and stamp - checked_at < CHECK_INTERVAL_SECONDS
        ):
            latest = cached.get("latest")
            return latest if isinstance(latest, str) and latest else None

    latest = _fetch_latest()
    # Stamp failures too, so an offline machine pays the 2 s timeout once a day
    # rather than on every command.
    _write_json(cache_path, {"checked_at": stamp, "latest": latest})
    return latest


def update_hint(current: str, *, now: float | None = None) -> str | None:
    """The `update available` line, or None when already current."""
    if not current or current == "dev":
        return None
    latest = latest_version(now=now)
    if not latest or not version_lt(current, latest):
        return None
    return f"smartmemory {latest} available — pip install -U smartmemory"


def upgrade_notices(current: str, tour_version: "Callable[[], int]") -> list[str]:
    """The `Updated to <ver>` line (plus tour nudge), and record what was seen.

    ``tour_version`` is a callable, not an int: resolving it imports
    :mod:`smartmemory_app.tour` (textual + tiktoken), which is far too heavy to
    pay on every command. The wrapper version is the gate — the tour ships
    inside the wrapper, so its version cannot change while the wrapper's stays
    put — and the import only happens on a first run or a real upgrade.

    Returns an empty list on a first-ever run (there is nothing to compare, and
    `sm setup` owns the first-run message) and whenever the wrapper is unchanged.
    """
    if not current or current == "dev":
        return []
    path = data_dir() / LAST_SEEN_FILENAME
    seen = _read_json(path)
    seen_wrapper = seen.get("wrapper") if seen else None
    seen_tour = seen.get("tour") if seen else None
    if seen_wrapper == current:
        return []

    tour = tour_version()
    notices: list[str] = []
    if seen_wrapper:
        notices.append(f"Updated to {current}")
        if seen_tour != tour:
            notices.append("Run 'sm tour' to see what's new")
    _write_json(path, {"wrapper": current, "tour": tour})
    return notices


def _tour_version() -> int:
    """Resolve the tour version. Imported lazily — `tour` pulls textual+tiktoken."""
    from smartmemory_app.tour import TOUR_VERSION

    return TOUR_VERSION


def _suppressed(command: str | None, argv: list[str]) -> bool:
    if is_disabled():
        return True
    if command in SUPPRESSED_COMMANDS:
        return True
    if "--json" in argv:
        return True
    try:
        return not sys.stdout.isatty()
    except Exception:
        return True


def trailing_notices(
    command: str | None = None, *, argv: list[str] | None = None
) -> list[str]:
    """Every line the CLI should print after ``command``, in order.

    Never raises: a failure anywhere in here must not change a command's outcome.
    """
    try:
        if _suppressed(command, argv if argv is not None else sys.argv):
            return []
        from smartmemory_app import __version__

        lines = upgrade_notices(__version__, _tour_version)
        hint = update_hint(__version__)
        if hint:
            lines.append(hint)
        return lines
    except Exception as exc:
        log.debug("update-check: trailing notices skipped: %s", exc)
        return []
