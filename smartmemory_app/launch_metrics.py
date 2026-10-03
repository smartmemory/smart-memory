"""LAUNCH-METRICS-1 — CLI-side launch event emission.

Remote mode POSTs directly to the hosted /memory/launch/event route. Local
mode POSTs to the daemon, which appends to ``launch_events.jsonl`` in the data
dir (local data stays local). An absent daemon is logged at DEBUG, unexpected
failures at WARNING. Observability must never break a CLI command.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)


VALID_EVENT_TYPES = frozenset(
    {
        "install.start",
        "setup.complete",
        "mcp.install",
        "index.start",
        "index.complete",
        "recall.invoke",
        "recall.first",
        "recall.accepted",
        "decision.create",
    }
)


def _daemon_url() -> Optional[str]:
    try:
        from smartmemory_app.config import load_config

        return f"http://127.0.0.1:{load_config().daemon_port}"
    except Exception:
        return None


def emit(event_type: str, props: Optional[Mapping[str, Any]] = None) -> bool:
    """Best-effort emit. Returns True on apparent success, False otherwise.

    Honors ``SMARTMEMORY_DISABLE_LAUNCH_METRICS=1`` for opt-out.
    """
    if os.environ.get("SMARTMEMORY_DISABLE_LAUNCH_METRICS") == "1":
        return False
    if event_type not in VALID_EVENT_TYPES:
        log.warning("launch_metrics: rejected unknown event_type=%s", event_type)
        return False

    try:
        import httpx  # type: ignore
    except Exception:
        return False

    payload = {"event_type": event_type, "props": dict(props or {})}
    started = time.monotonic()
    result: list[bool] = []
    target = ["daemon"]

    def send() -> None:
        try:
            from smartmemory_app.config import get_api_key, load_config

            cfg = load_config()
            if cfg.mode == "remote":
                target[0] = "hosted service"
                key = get_api_key()
                if not key:
                    log.warning(
                        "launch_metrics: no remote API key event=%s", event_type
                    )
                    return
                url = f"{cfg.api_url.rstrip('/')}/memory/launch/event"
                headers = {"Authorization": f"Bearer {key}"}
                if cfg.team_id:
                    headers["X-Workspace-Id"] = cfg.team_id
                # Remote setup and RemoteMemory honor proxy env vars too.
                with httpx.Client(trust_env=True) as client:
                    r = client.post(url, json=payload, headers=headers, timeout=2.0)
            else:
                base = _daemon_url()
                if not base:
                    return
                # Localhost must bypass proxies, including optional SOCKS extras.
                with httpx.Client(trust_env=False) as client:
                    r = client.post(
                        f"{base}/memory/launch/event", json=payload, timeout=2.0
                    )
            if 200 <= r.status_code < 300:
                result.append(True)
            else:
                log.warning(
                    "launch_metrics: %s rejected event=%s status=%s",
                    target[0],
                    event_type,
                    r.status_code,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Only telemetry is lost when the daemon is stopped, never user data.
            level = logging.DEBUG if target[0] == "daemon" else logging.WARNING
            log.log(
                level,
                "launch_metrics: %s unreachable event=%s err=%s",
                target[0],
                event_type,
                type(exc).__name__,
            )
        except Exception as exc:
            # Never include a remote URL or credentials reflected in an exception.
            log.warning(
                "launch_metrics: %s emit failed event=%s err=%s",
                target[0],
                event_type,
                type(exc).__name__,
            )
        finally:
            if not result:
                result.append(False)

    # HTTP timeouts are per phase. Bound config/keychain, DNS, proxy setup and
    # transport together so telemetry never holds up setup beyond two seconds.
    try:
        thread = threading.Thread(
            target=send, name="smartmemory-launch-event", daemon=True
        )
        thread.start()
        thread.join(max(0.0, 2.0 - (time.monotonic() - started)))
    except Exception as exc:
        log.warning(
            "launch_metrics: emit failed event=%s err=%s",
            event_type,
            type(exc).__name__,
        )
        return False
    if not result:
        # The daemon may still be timing out its connect phase at this deadline.
        # This is telemetry only, no user data is lost. Unexpected exceptions
        # and reachable HTTP errors retain their WARNING in send().
        log.log(
            logging.DEBUG if target[0] == "daemon" else logging.WARNING,
            "launch_metrics: %s emit exceeded two-second budget event=%s",
            target[0],
            event_type,
        )
    return bool(result and result[0])
