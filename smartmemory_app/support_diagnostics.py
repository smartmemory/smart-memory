"""Bounded local support checks and a portable, redacted support archive."""

import json
import os
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from smartmemory_app.diagnostics import redact_credentials
from smartmemory_app.install_check import PROXY_ENV_VARS

DEFAULT_MODELS = (
    "sentence-transformers/all-MiniLM-L6-v2",
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
)


def _best_effort(call):
    try:
        return call()
    except Exception as exc:
        return redact_credentials(f"warning: unavailable ({type(exc).__name__}: {exc})")


def proxy_host(value: str) -> str:
    """Return only scheme and host, never credentials, paths or queries."""
    try:
        url = urlsplit(value if "://" in value else "http://" + value)
        return f"{url.scheme}://{url.hostname or '<unknown>'}"
    except Exception:
        return "<invalid proxy>"


def proxy_summary() -> str:
    entries = [
        f"env {name}={proxy_host(os.environ[name])}"
        for name in PROXY_ENV_VARS
        if os.environ.get(name)
    ]
    system = _best_effort(urllib.request.getproxies)
    if isinstance(system, dict):
        entries += [
            f"system {name}={proxy_host(value)}"
            for name, value in system.items()
            if name != "no"
        ]
    else:
        entries.append(f"system proxies: {system}")
    return ", ".join(entries) or "none"


def hf_settings() -> str:
    """Read Hub's effective constants, including its import-time timeout values."""
    from huggingface_hub import constants

    return (
        f"cache={constants.HF_HUB_CACHE} endpoint={proxy_host(constants.ENDPOINT)} "
        f"HF_HUB_DOWNLOAD_TIMEOUT={constants.HF_HUB_DOWNLOAD_TIMEOUT} "
        f"HF_HUB_ETAG_TIMEOUT={constants.HF_HUB_ETAG_TIMEOUT}"
    )


def log_download_start(logger, model: str, backend: str) -> None:
    try:
        proxies = proxy_summary()
        proxy = "no" if proxies == "none" else f"yes ({proxies})"
        logger.info(
            "Model preparation start: model=%s backend=%s %s proxy=%s",
            model,
            backend,
            _best_effort(hf_settings),
            proxy,
        )
    except Exception:
        pass


def download_failure(logger, model: str, exc: BaseException) -> str:
    """Log the original traceback and return a safe recovery instruction."""
    cause, seen = exc, set()
    timeout = False
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        timeout |= (
            "timeout" in type(cause).__name__.lower()
            or "timed out" in str(cause).lower()
        )
        cause = cause.__cause__ or cause.__context__
    guidance = (
        (
            "Set HF_HUB_DOWNLOAD_TIMEOUT to a larger value for slow connections, "
            "optionally set HF_ENDPOINT to your Hugging Face mirror, then retry smartmemory setup."
        )
        if timeout
        else "Retry smartmemory setup after checking network access."
    )
    try:
        logger.warning(
            "Model preparation failed: %s: %s. %s",
            model,
            redact_credentials(str(exc)),
            guidance,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    except Exception:
        pass
    message = f"{exc}. {guidance}" if timeout else str(exc)
    return redact_credentials(message)


def _probe(host: str) -> str:
    import httpx

    started = time.monotonic()
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if urllib.request.proxy_bypass(host):
        proxy = None
    with httpx.Client(timeout=1.5, trust_env=False, proxy=proxy) as client:
        with client.stream(
            "HEAD", f"https://{host}", follow_redirects=False
        ) as response:
            return f"reachable ({(time.monotonic() - started) * 1000:.0f} ms, HTTP {response.status_code})"


def network_checks(*, budget: float = 4.0) -> list[str]:
    """Use one wall-clock deadline even if OS DNS resolution ignores timeouts."""
    hosts = ("huggingface.co", "pypi.org", "api.smartmemory.ai")
    results = {}

    def run(host):
        results[host] = _best_effort(lambda: _probe(host))

    threads = [
        threading.Thread(target=run, args=(host,), daemon=True) for host in hosts
    ]
    deadline = time.monotonic() + budget
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    return [
        redact_credentials(
            f"Network {host}: {results.get(host, 'warning: check timed out')}"
        )
        for host in hosts
    ]


def _cache_status() -> str:
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    rows = [f"HF cache: {cache}", hf_settings()]
    for model in DEFAULT_MODELS:
        # Read-only file inspection. Do not resolve models through network-capable loaders.
        snapshots = cache / ("models--" + model.replace("/", "--")) / "snapshots"
        present = False
        onnx_present = False
        if snapshots.exists():
            for snapshot in snapshots.iterdir():
                onnx_present |= (snapshot / "onnx/model.onnx").is_file() and (
                    snapshot / "tokenizer.json"
                ).is_file()
                weights = any(
                    p.is_file() for p in snapshot.glob("*.safetensors")
                ) or any(p.is_file() for p in snapshot.glob("pytorch_model*.bin"))
                if (snapshot / "config.json").is_file() and weights:
                    present = True
        rows.append(
            f"Model {model}: {'present (torch config and weights)' if present else 'missing or incomplete'}"
        )
        if model == DEFAULT_MODELS[0]:
            rows.append(
                f"Default embedding ONNX files: {'present' if onnx_present else 'missing or incomplete'}"
            )
    return "\n".join(rows)


def _daemon_status() -> str:
    from smartmemory_app.daemon import _health_response, _pid_file

    try:
        response = _health_response(1.0)
        response.raise_for_status()
        status = response.json().get("status", "unknown")
        return f"Daemon: running, health={status}"
    except Exception:
        return (
            f"Daemon: unhealthy or stopped (PID marker present={_pid_file().exists()})"
        )


def _writable(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=data_dir) as file:
        file.write(b"support write check")
    return f"Data directory: {data_dir}, writable"


def local_checks() -> list[str]:
    from smartmemory_app.bug_report import debug_log_path

    rows = network_checks()
    for name, call in (
        ("Proxy", proxy_summary),
        ("Models", _cache_status),
        ("Daemon", _daemon_status),
        ("Storage", lambda: _writable(debug_log_path().parent)),
    ):
        rows.append(redact_credentials(f"{name}: {_best_effort(call)}"))
    return rows


def support_texts(doctor_output: str, message: str = "") -> dict[str, str]:
    """Build shared, privacy-filtered archive and upload content."""
    from smartmemory_app.bug_report import (
        debug_log_path,
        gather_environment,
    )
    from smartmemory_app.config import load_config
    from smartmemory_app.report_privacy import (
        private_text,
        read_private_log_tail,
        safe_log_text,
    )

    def config_text():
        # Effective fields only. Never upload arbitrary raw TOML or credentials.
        config = {
            key: value
            for key, value in asdict(load_config()).items()
            if not any(
                word in key.lower() for word in ("key", "token", "password", "secret")
            )
        }
        return "Effective config:\n" + json.dumps(config, indent=2)

    texts = {
        "doctor.txt": private_text(doctor_output),
        "environment.txt": private_text(
            _best_effort(lambda: gather_environment().browser_info)
        ),
        "config.txt": private_text(_best_effort(config_text)),
    }
    if message:
        texts["message.txt"] = private_text(message)
    for name in ("cli-debug.log", "daemon.log"):
        texts[name] = safe_log_text(
            _best_effort(
                lambda name=name: read_private_log_tail(debug_log_path().parent / name)
            )
            or "Log unavailable or empty"
        )
    return texts


def write_support_bundle(
    path: Path,
    doctor_output: str,
    *,
    message: str = "",
    texts: dict[str, str] | None = None,
) -> Path:
    """Atomically write the same privacy-filtered content used by manual sends."""
    texts = texts if texts is not None else support_texts(doctor_output, message)
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".zip", delete=False
        ) as temp:
            temp_path = Path(temp.name)
        with zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, value in texts.items():
                archive.writestr(name, redact_credentials(value))
        temp_path.replace(path)
        return path
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def default_bundle_path() -> Path:
    from smartmemory_app.bug_report import debug_log_path

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return debug_log_path().parent / f"smartmemory-support-{timestamp}.zip"
