"""CLI-HINTS-FUNNEL-1: every hint points one way, toward the account.

One home for the wording and the rate limit, so a hint site imports a constant instead of
spelling its own command:

* Not logged in: ``sm login``, and nothing else. No hint suggests local mode.
* Local mode: a periodic (rate-limited) line saying memory is on this machine only and
  ``sm login`` shares it. No hint ever suggests ``sm setup --local``.
* Remote and logged in: no hint.

The periodic line follows the update-check rules (``update_check.trailing_notices``): never on
a non-TTY, ``--json``, a hook command, or when ``SMARTMEMORY_NO_UPDATE_CHECK`` is set. Like
that check, a failure here is logged at DEBUG and never changes a command's outcome.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

LOGIN_HINT = "Run: sm login"
LOCAL_ONLY_HINT = (
    "Memory is on this machine only. `sm login` shares it across agents and machines."
)

LOCAL_HINT_FILENAME = ".local-hint.json"
LOCAL_HINT_INTERVAL_SECONDS = 7 * 24 * 60 * 60


def _state_path() -> Path:
    from smartmemory_app.update_check import data_dir

    return data_dir() / LOCAL_HINT_FILENAME


def local_mode_hint(mode: str, *, now: float | None = None) -> str | None:
    """The local-mode line when ``mode`` is local and one is due, else None.

    Stamps the time it was shown, so the next one is a full interval away. A state file that
    cannot be read counts as "never shown"; one that cannot be written means the hint may repeat,
    which is the harmless side to fail on.
    """
    if (
        mode != "local"
    ):  # remote is logged in; unconfigured has its own `sm login` sites
        return None
    stamp = time.time() if now is None else now
    path = _state_path()
    try:
        shown_at = json.loads(path.read_text(encoding="utf-8")).get("shown_at")
    except Exception as exc:  # missing, unreadable or corrupt: all mean "never shown"
        log.debug("hints: no readable state at %s: %s", path, exc)
        shown_at = None
    if (
        isinstance(shown_at, (int, float))
        and 0 <= stamp - shown_at < LOCAL_HINT_INTERVAL_SECONDS
    ):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"shown_at": stamp}), encoding="utf-8")
    except Exception as exc:
        log.debug("hints: could not write %s: %s", path, exc)
    return LOCAL_ONLY_HINT


def funnel_notices() -> list[str]:
    """Funnel lines for the current config, for ``update_check.trailing_notices``."""
    from smartmemory_app.config import load_config

    hint = local_mode_hint(load_config().mode)
    return [hint] if hint else []
