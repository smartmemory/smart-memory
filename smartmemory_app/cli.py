"""SmartMemory CLI — daemon management + memory operations.

DIST-DAEMON-1: All memory commands (add, ingest, search, recall) try the
daemon HTTP API first (<200ms). Falls back to direct storage calls if daemon
is not running (~22s cold start).
"""

import logging
import json
import os
from pathlib import Path

import click

log = logging.getLogger(__name__)


def _configure_cli_logging() -> None:
    """Install the CLI's root logging policy (DIST-CLI-QUIET-1).

    Default WARNING so user-facing commands stay clean of pipeline INFO chatter,
    while degradation warnings remain visible (no-silent-degradation rule).
    Installing a root handler up front also neutralizes import-time
    ``logging.basicConfig()`` calls in third-party deps (e.g. fastcoref), which
    are no-ops once the root logger already has a handler.

    Override with ``SMARTMEMORY_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR``.
    """
    level_name = os.environ.get("SMARTMEMORY_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


# DIST-INSTALL-RESOLVE-1: conservative floor for smartmemory-core. A core BELOW
# this is from the dead-`/auth/me` era — a pip-backtracked install (e.g. wrapper
# 1.1.5 → core 0.7.1) that 401s at first API call. `smartmemory doctor` flags it.
MIN_CORE_VERSION = "1.0.0"


def _version_lt(a: str, b: str) -> bool:
    """Return True if version string `a` is strictly less than `b`.

    Prefers packaging.version (an indirect dep) for PEP 440 correctness; falls
    back to a tuple-of-ints compare so doctor never hard-fails on a missing dep.
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


def _parse_extra_props(args: list[str]) -> dict[str, str]:
    """Parse Click extra args (--key value pairs) into a property dict."""
    props = {}
    i = 0
    while i < len(args):
        if (
            args[i].startswith("--")
            and i + 1 < len(args)
            and not args[i + 1].startswith("--")
        ):
            props[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    return props


def _daemon_url() -> str:
    from smartmemory_app.config import load_config

    return f"http://127.0.0.1:{load_config().daemon_port}"


_DAEMON_NOT_RUNNING_MSG = (
    "SmartMemory daemon is not running. Run `sm start` (first time: `sm setup`)."
)
_DAEMON_LOG_HINT = "~/.smartmemory/daemon.log"


def _daemon_request(method: str, path: str, timeout: int = 120, **kwargs):
    """Try daemon HTTP API. Returns parsed JSON.

    Retries once on connection drop — handles the case where the daemon
    auto-restarts after a pip upgrade (version guard middleware exits the
    process, launchd restarts it within ~5s).

    Uses trust_env=False so proxy env vars (ALL_PROXY/HTTP(S)_PROXY, SOCKS)
    never interfere with local 127.0.0.1 daemon calls (L4).

    Raises click.ClickException with a user-friendly message on transport
    errors or HTTP 5xx responses — no raw tracebacks (L1).
    """
    import httpx
    import time

    for attempt in range(2):
        try:
            with httpx.Client(trust_env=False) as client:
                r = client.request(
                    method, f"{_daemon_url()}{path}", timeout=timeout, **kwargs
                )
            r.raise_for_status()
            return r.json() if r.status_code != 204 else {}
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if attempt == 0:
                time.sleep(2)  # wait for launchd to restart daemon (~1.2s startup)
                continue
            # Daemon unreachable: return None so callers fall back to direct local
            # storage (add/search/get all branch on None). Commands with no local
            # fallback surface _DAEMON_NOT_RUNNING_MSG themselves.
            click.echo(
                f"({_DAEMON_NOT_RUNNING_MSG} Using direct local access.)", err=True
            )
            return None
        except httpx.HTTPStatusError as e:
            # Surface server errors (e.g. 501 for unsupported filters) to caller
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            if e.response.status_code >= 500:
                raise click.ClickException(f"{detail}  (check {_DAEMON_LOG_HINT})")
            raise click.ClickException(detail)
        except httpx.ReadTimeout:
            raise click.ClickException(
                f"SmartMemory daemon is not responding (timeout).  (check {_DAEMON_LOG_HINT})"
            )


def _lifecycle_via_daemon(path: str, body: dict, timeout: float = 5.0):
    """POST one lifecycle phase to the warm daemon. Returns parsed JSON, or None.

    DIST-DAEMON-1 promised every memory command tries the daemon first and falls
    back to direct storage; the `lifecycle` group never got wired up, so every
    hook fired a fresh interpreter and paid the ~12-14s embedder cold start. That
    is what made UserPromptSubmit cross Claude Code's 30s kill line (measured over
    120 prompts: median 4.9s, p90 15.2s, max 28.4s).

    Deliberately NOT `_daemon_request`: this is the hook critical path, so it is
    fail-fast rather than resilient — no 2s retry sleep, short timeout, and silent
    on stderr. Any failure returns None and the caller runs the phase in-process,
    exactly as before. The daemon is an accelerator here, never a dependency.
    """
    try:
        import httpx

        with httpx.Client(trust_env=False) as client:
            r = client.post(f"{_daemon_url()}{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


@click.group()
@click.version_option(package_name="smartmemory", prog_name="smartmemory")
def cli() -> None:
    """SmartMemory — persistent AI memory system."""
    _configure_cli_logging()


# Register setup/uninstall commands from setup module
from smartmemory_app.setup import setup as _setup_cmd, uninstall as _uninstall_cmd  # noqa: E402

cli.add_command(_setup_cmd, name="setup")
# `sm init` — alias for `sm setup`. Same Click command registered under a
# second name so option/flag parity is automatic.
cli.add_command(_setup_cmd, name="init")
cli.add_command(_uninstall_cmd, name="uninstall")

# `sm code …` and `sm mcp …` subgroups
from smartmemory_app.cli_code import code_group as _code_group  # noqa: E402
from smartmemory_app.cli_mcp import mcp_group as _mcp_group  # noqa: E402

cli.add_command(_code_group, name="code")
cli.add_command(_mcp_group, name="mcp")


# ── `sm provenance …` — code-to-conversation provenance (CORE-CODE-PROVENANCE-1) ──
def _codex_path_on_or_after(path, root, since: str) -> bool:
    """A rollout lives at ``<root>/YYYY/MM/DD/rollout-*.jsonl``; keep it iff its
    path-date dir is on/after ``since`` (YYYY-MM-DD). Cheapest robust filter — no
    per-file stat. Paths not matching the date layout are kept (not filtered out)."""
    try:
        parts = path.relative_to(root).parts  # (YYYY, MM, DD, filename)
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}" >= since
    except Exception:
        pass
    return True


@cli.group("provenance")
def provenance_group() -> None:
    """Code-to-conversation provenance (CORE-CODE-PROVENANCE-1)."""


@provenance_group.command("import-codex")
@click.option(
    "--codex-dir",
    default=None,
    show_default=True,
    help="Codex sessions root (default: ~/.codex/sessions).",
)
@click.option(
    "--since",
    default=None,
    help="Only import rollouts whose path-date dir is on/after YYYY-MM-DD.",
)
@click.option("--dry-run", is_flag=True, help="Parse and count, but do not persist.")
def provenance_import_codex(codex_dir, since, dry_run) -> None:
    """Import Codex apply_patch authorship into the graph as code_provenance evidence."""
    from pathlib import Path

    from smartmemory.provenance.extract import codex_session_edits, iter_codex_sessions
    from smartmemory_app.storage import persist_provenance

    root = (
        Path(codex_dir).expanduser()
        if codex_dir
        else (Path.home() / ".codex" / "sessions")
    )
    if not root.exists():
        click.echo(f"No Codex sessions directory at {root}")
        return

    sessions = rows = edges = no_patch = 0
    for path in iter_codex_sessions(root):
        if since and not _codex_path_on_or_after(path, root, since):
            continue
        se = codex_session_edits(path)
        if se is None:
            no_patch += 1
            continue
        sessions += 1
        rows += len(se.edits)
        if not dry_run:
            res = persist_provenance(se)
            edges += int(res.get("edges", 0))

    verb = "Would import" if dry_run else "Imported"
    click.echo(
        f"{verb} {rows} evidence rows across {sessions} sessions "
        f"({edges} edges; {no_patch} rollouts had no apply_patch)."
    )


# ── Daemon lifecycle ────────────────────────────────────────────────────────


@cli.command("start")
@click.option(
    "--num-workers",
    default=1,
    show_default=True,
    help="Number of enrichment worker processes.",
)
def start_cmd(num_workers: int) -> None:
    """Start the SmartMemory daemon and enrichment workers."""
    from smartmemory_app.daemon import start_daemon, is_running

    if is_running():
        click.echo("SmartMemory daemon is already running.")
        return
    click.echo("Starting SmartMemory daemon (loading models)...")
    try:
        # Stream the daemon's own startup progress (backend load, embedding warmup,
        # etc.) so a ~22s cold start shows what it's doing instead of hanging silent.
        start_daemon(num_workers=num_workers, on_log=lambda ln: click.echo(f"  {ln}"))
        click.echo("Daemon ready.")
    except Exception as e:
        click.echo(f"Failed to start daemon: {e}", err=True)
        raise SystemExit(1)


@cli.command("stop")
def stop_cmd() -> None:
    """Stop the SmartMemory daemon."""
    from smartmemory_app.daemon import stop_daemon, is_running

    if not is_running(require_healthy=False):
        click.echo("Daemon is not running.")
        return
    stop_daemon()
    click.echo("Daemon stopped.")


@cli.command("restart")
@click.option(
    "--num-workers",
    default=1,
    show_default=True,
    help="Number of enrichment worker processes.",
)
def restart_cmd(num_workers: int) -> None:
    """Restart the SmartMemory daemon and enrichment workers."""
    from smartmemory_app.daemon import stop_daemon, start_daemon, is_running

    if is_running(require_healthy=False):
        click.echo("Stopping daemon...")
        stop_daemon()
    click.echo("Starting daemon...")
    start_daemon(num_workers=num_workers, on_log=lambda ln: click.echo(f"  {ln}"))
    click.echo("Daemon ready.")


@cli.command("warm")
@click.option(
    "--no-reranker",
    is_flag=True,
    help="Warm only the embedder, skip the reranker model.",
)
def warm_cmd(no_reranker: bool) -> None:
    """Pre-load the local models so the first add/search is instant.

    The first add() otherwise pays a cold embedder load (~12s, or ~38s the first
    time the model downloads) and the first search() pays a cold reranker load.
    Run this once after install — or before a demo — to move that cost off the
    user's first real call (DIST-LITE-WARMSTART-1).
    """
    import time

    from smartmemory_app.warm import warm_models

    click.echo("Warming local models (one-time; subsequent runs are cached)...")
    t0 = time.perf_counter()
    warm_models(reranker=not no_reranker)
    click.echo(
        f"Models warm in {time.perf_counter() - t0:.1f}s. First add/search will now be fast."
    )


@cli.command("status")
def status_cmd() -> None:
    """Show SmartMemory daemon status."""
    from smartmemory_app.daemon import get_status

    info = get_status()
    if info is None:
        click.echo("SmartMemory daemon is not running.")
        click.echo("Start with: smartmemory start")
        return
    click.echo(f"SmartMemory daemon: {info.get('status', '?')}")
    # DIST-LOCAL-REMOTE-AWARENESS-1: surface lite-vs-cloud detachment. The daemon
    # /health response reports mode ("lite" | "remote"); in lite mode the store is
    # a local SQLite graph on THIS machine, not the cloud account — so a cloud
    # dashboard showing "0 memories" is expected, not a sync failure.
    _mode = info.get("mode")
    if _mode:
        _mode_label = "lite (local-only)" if _mode == "lite" else _mode
        click.echo(f"  Mode:       {_mode_label}")
    click.echo(f"  Memories:   {info.get('memories', '?')}")
    _llm = info.get("llm_provider", "?")
    # Flag the silent-failure case: a provider is configured but no key is present,
    # so extraction is actually disabled despite the config saying otherwise.
    if info.get("llm_key_present") is False and _llm not in ("none", "?"):
        click.echo(f"  LLM:        {_llm} (no API key — extraction disabled)")
    else:
        click.echo(f"  LLM:        {_llm}")
    click.echo(f"  Embeddings: {info.get('embedding_provider', '?')}")
    click.echo(f"  PID:        {info.get('pid', '?')}")
    async_info = info.get("async_enrichment", {})
    if async_info.get("enabled"):
        pending = async_info.get("pending", 0)
        done = async_info.get("done", 0)
        failed = async_info.get("failed", 0)
        click.echo(f"  Queue:      pending={pending}, done={done}, failed={failed}")
    else:
        click.echo("  Queue:      (no table)")

    # DIST-LOCAL-REMOTE-AWARENESS-1: in lite mode, explain the local↔cloud
    # boundary so an empty cloud dashboard isn't read as data loss. The cloud
    # web dashboard reads the remote account; these memories live on this
    # machine and were never pushed to the cloud.
    if _mode == "lite":
        click.echo(
            "\nNote: lite (local-only) mode — these memories are stored on THIS "
            "machine (local SQLite graph), not in your cloud account. A cloud "
            "dashboard at smartmemory.ai showing 0 memories is expected, not a "
            "sync failure."
        )


@cli.command("viewer")
@click.option("--port", default=None, type=int, help="Port override.")
def viewer_cmd(port: int | None) -> None:
    """Open the knowledge graph viewer in the browser."""
    import webbrowser
    from smartmemory_app.daemon import start_daemon, is_running, _port

    if not is_running():
        click.echo("Starting daemon...")
        start_daemon()
    p = port or _port()
    webbrowser.open(f"http://localhost:{p}")


@cli.command("tour")
@click.option("--keep", is_flag=True, help="Keep the isolated tour store after exit.")
@click.option("--code", is_flag=True, help="Show the code-intelligence tour branch.")
@click.option(
    "--port", default=None, type=int, help="Port override for the tour daemon."
)
@click.option(
    "--no-viewer", is_flag=True, help="Run without opening the browser viewer."
)
def tour_cmd(keep: bool, code: bool, port: int | None, no_viewer: bool) -> None:
    """Run the guided SmartMemory onboarding tour."""
    from smartmemory_app import tour

    tour.run_tour(keep=keep, code=code, port=port, no_viewer=no_viewer)


@cli.command("worker")
@click.option(
    "--loop", is_flag=True, help="Poll continuously instead of drain-and-exit."
)
def worker_cmd(loop: bool) -> None:
    """Run the enrichment worker (Tier 2 LLM extraction).

    Drains the SQLite enrichment queue. Use --loop for continuous polling.
    """
    # The worker's module-level basicConfig is a no-op under the CLI's root
    # handler; its INFO progress lines are the command's purpose, so raise the
    # level here unless the user pinned one explicitly.
    if "SMARTMEMORY_LOG_LEVEL" not in os.environ:
        logging.getLogger().setLevel(logging.INFO)
    from smartmemory_app.enrichment_worker import drain_queue, run_loop

    if loop:
        run_loop()
    else:
        n = drain_queue()
        click.echo(f"Processed {n} jobs")


# ── Memory operations ───────────────────────────────────────────────────────

_VALID_MEMORY_TYPES = {
    "pending",
    "semantic",
    "episodic",
    "procedural",
    "zettel",
    "reasoning",
    "opinion",
    "observation",
    "decision",
}


def _validate_memory_type(ctx, param, value: str) -> str:
    if value not in _VALID_MEMORY_TYPES:
        sorted_types = sorted(_VALID_MEMORY_TYPES)
        raise click.BadParameter(
            f"Invalid type '{value}'. Valid types: {', '.join(sorted_types)}"
        )
    return value


_warm_notice_shown = False


def _warm_notice() -> None:
    """Show a one-time notice if a direct (no-daemon) op is about to pay a cold model
    load, so first-run isn't a silent multi-second hang (DIST-LITE-WARMSTART-1). The
    daemon path already prints "loading models" at start; this covers direct CLI ops.
    """
    global _warm_notice_shown
    if _warm_notice_shown:
        return
    from smartmemory_app.warm import is_warm

    if not is_warm():
        click.echo(
            "First run: loading local models (~10–40s, one-time). "
            "Tip: run 'smartmemory warm' to pre-load.",
            err=True,
        )
        _warm_notice_shown = True


@cli.command(
    "add",
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    ),
)
@click.argument("text", default="-")
@click.option(
    "--type",
    "memory_type",
    default="episodic",
    show_default=True,
    callback=_validate_memory_type,
)
@click.option(
    "--all",
    "as_whole",
    is_flag=True,
    help="Add stdin as one memory instead of line-by-line.",
)
@click.pass_context
def add_cmd(ctx, text: str, memory_type: str, as_whole: bool) -> None:
    """Add text as a memory. Use - or pipe stdin to read from a file.

    When reading from stdin, each non-empty line becomes a separate memory.
    Use --all to add the entire input as a single memory instead.

    Examples:
        smartmemory add "Alice leads Project Atlas"
        smartmemory add - < notes.txt              # line-by-line
        smartmemory add --all - < document.txt     # whole file
        echo "some text" | smartmemory add -

    Supports arbitrary property flags: --project atlas --domain legal
    """
    import sys

    if text == "-":
        if sys.stdin.isatty():
            raise click.ClickException(
                'No input. Pipe text or use: smartmemory add "text"'
            )
        raw = sys.stdin.read()
        if not raw.strip():
            raise click.ClickException("Content cannot be empty.")
        chunks = (
            [raw.strip()]
            if as_whole
            else [ln.strip() for ln in raw.splitlines() if ln.strip()]
        )
        if not chunks:
            raise click.ClickException("Content cannot be empty.")
        props = _parse_extra_props(ctx.args)
        ids = []
        warning = None
        for chunk in chunks:
            # DIST-LITE-QUIET-1: the CLI declares its producer to the (producer-neutral)
            # daemon so writes are attributed cli:add (tier 1), not origin='unknown'.
            body: dict = {
                "content": chunk,
                "memory_type": memory_type,
                "context": {"origin": "cli:add"},
            }
            if props:
                body["properties"] = props
            result = _daemon_request("POST", "/memory/ingest", json=body)
            if result:
                ids.append(result.get("item_id", "?"))
                warning = warning or result.get("warning")
            else:
                from smartmemory_app.storage import ingest
                from smartmemory_app.remote_backend import RemoteBackendError

                _warm_notice()
                # DIST-LITE-QUIET-1: attribute local CLI writes (else origin='unknown').
                try:
                    ids.append(
                        ingest(chunk, memory_type, properties=props, origin="cli:add")
                    )
                except RemoteBackendError as e:
                    raise click.ClickException(
                        f"Add failed — could not reach the SmartMemory service: {e}"
                    )
        click.echo(f"Added {len(ids)} memories")
        for item_id in ids:
            click.echo(item_id)
        if warning:
            # Surfaced once for the whole batch — the daemon degraded to Tier-1.
            click.echo(f"⚠  {warning}", err=True)
        return
    if not text.strip():
        raise click.ClickException("Content cannot be empty.")
    props = _parse_extra_props(ctx.args)
    # DIST-LITE-QUIET-1: declare the CLI producer to the producer-neutral daemon.
    body: dict = {
        "content": text,
        "memory_type": memory_type,
        "context": {"origin": "cli:add"},
    }
    if props:
        body["properties"] = props
    result = _daemon_request("POST", "/memory/ingest", json=body)
    if result:
        click.echo(result.get("item_id", "?"))
        if result.get("warning"):
            click.echo(f"⚠  {result['warning']}", err=True)
    else:
        from smartmemory_app.storage import ingest
        from smartmemory_app.remote_backend import RemoteBackendError

        _warm_notice()
        # DIST-LITE-QUIET-1: attribute local CLI writes (else origin='unknown').
        try:
            click.echo(ingest(text, memory_type, properties=props, origin="cli:add"))
        except RemoteBackendError as e:
            raise click.ClickException(
                f"Add failed — could not reach the SmartMemory service: {e}"
            )


@cli.command("recall")
@click.option("--cwd", default=None, help="Current working directory for context.")
@click.option("--top-k", default=10, show_default=True)
@click.option(
    "--query", default=None, help="Optional prompt query (UserPromptSubmit hook)."
)
@click.option(
    "--workspace", "workspace_id", default=None, help="Workspace ID override."
)
@click.option(
    "--strict/--no-strict",
    default=False,
    help="Drop legacy items without workspace_id (kills cross-workspace leak).",
)
@click.option(
    "--no-snapshot",
    "no_snapshot",
    is_flag=True,
    default=False,
    help="Skip snapshot frame.",
)
def recall_cmd(
    cwd: str, top_k: int, query: str, workspace_id: str, strict: bool, no_snapshot: bool
) -> None:
    """Recall memories (SessionStart / UserPromptSubmit hook)."""
    params = {
        "cwd": cwd or "",
        "top_k": top_k,
        "query": query or "",
        "workspace_id": workspace_id or "",
        "include_snapshot": "false" if no_snapshot else "true",
        "strict": "true" if strict else "false",
    }
    result = _daemon_request("GET", "/memory/recall", params=params)
    if result:
        context = result.get("context", "")
    else:
        from smartmemory_app.storage import recall

        _warm_notice()
        context = recall(
            cwd,
            top_k,
            query=query,
            workspace_id=workspace_id,
            include_snapshot=not no_snapshot,
            strict=strict,
        )
    if context:
        click.echo(context)


@cli.command("retag")
@click.option(
    "--content",
    "content_substring",
    required=True,
    help="Substring to match in item content (case-insensitive).",
)
@click.option(
    "--origin",
    "new_origin",
    required=True,
    help='New origin value, e.g. "seed:demo" (tier 4, hidden from recall).',
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show items that would be retagged without modifying.",
)
@click.option(
    "--limit", default=100, show_default=True, help="Max items to retag in one run."
)
def retag_cmd(
    content_substring: str, new_origin: str, dry_run: bool, limit: int
) -> None:
    """Retag items by content match (HOOK-RECALL-RELEVANCE-1 G3.C).

    Use to clean up legacy seed/fixture data that leaks into recall:

      smartmemory retag --content "Alice leads Project Atlas" --origin seed:demo

    seed:* is tier 4, so retagged items disappear from recall but stay in
    storage for audit / re-classification later.
    """
    from smartmemory_app.storage import get_memory

    mem = get_memory()
    backend = getattr(getattr(mem, "_graph", None), "backend", None)
    if backend is None:
        click.echo("error: no graph backend available", err=True)
        raise SystemExit(1)

    needle = content_substring.lower()
    matched, updated = [], 0
    snapshot = backend.serialize() if hasattr(backend, "serialize") else {"nodes": []}
    for raw in snapshot.get("nodes", []):
        # Content lives in `properties.content` for SQLite/FalkorDB serialize shape;
        # fall back to top-level `content` for forward-compat.
        props = raw.get("properties") or {}
        content = (
            (props.get("content") if isinstance(props, dict) else None)
            or raw.get("content")
            or ""
        )
        if needle not in content.lower():
            continue
        item_id = raw.get("item_id") or raw.get("id")
        if not item_id:
            continue
        matched.append((item_id, content[:80]))
        if len(matched) >= limit:
            break

    if not matched:
        click.echo(f"No items match content substring {content_substring!r}.")
        return

    click.echo(f"Found {len(matched)} item(s) matching {content_substring!r}:")
    for iid, preview in matched:
        click.echo(f"  {iid[:12]} {preview!r}")

    if dry_run:
        click.echo(f"(dry run — re-run without --dry-run to retag with origin={new_origin!r})")
        return

    for iid, _ in matched:
        try:
            backend.set_properties(iid, {"origin": new_origin})
            updated += 1
        except Exception as exc:
            click.echo(f"  failed to retag {iid[:12]}: {exc}", err=True)
    click.echo(f"Retagged {updated}/{len(matched)} items with origin={new_origin!r}.")


@cli.command(
    "search",
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    ),
)
@click.argument("query")
@click.option("--top-k", default=5, show_default=True)
@click.option(
    "--include-reference",
    is_flag=True,
    default=False,
    help="Include reference data in results",
)
@click.pass_context
def search_cmd(ctx, query: str, top_k: int, include_reference: bool) -> None:
    """Search memories by semantic similarity. Use '*' to list all.

    Supports property filters: --project atlas --domain legal
    """
    props = _parse_extra_props(ctx.args)
    body: dict = {"query": query, "top_k": top_k}
    if props:
        body["filters"] = props
    if include_reference:
        body["include_reference"] = True
    results = _daemon_request("POST", "/memory/search", json=body)
    if results is None:
        from smartmemory_app.storage import search
        from smartmemory_app.remote_backend import RemoteBackendError

        # One-shot in-process search: this interpreter exits right after the
        # query, so the reranker's background load can never finish in time —
        # "async" here means every result comes back in fusion order with a
        # cold-fallback warning. Opt into the synchronous load instead; on top
        # of the ~3.5s embed load this path already blocks on, the marginal
        # cost is ~0.25s (measured 2026-08-31). The daemon path above stays
        # async — a long-lived process warms at boot (CORE-SEARCH-WARMSTART-1).
        os.environ.setdefault("SMARTMEMORY_RERANK_BLOCK", "1")
        try:
            results = search(
                query, top_k, filters=props, include_reference=include_reference
            )
        except NotImplementedError as e:
            raise click.ClickException(str(e))
        except RemoteBackendError as e:
            raise click.ClickException(
                f"Search failed — could not reach the SmartMemory service: {e}"
            )
    # The daemon returns the CORE-CRUD-LIST contract shape {"items": [...]};
    # the storage fallback returns a bare list. Unwrap so we always iterate
    # result dicts (iterating the dict directly yielded its keys -> "items"
    # string -> AttributeError: 'str' object has no attribute 'get').
    if isinstance(results, dict):
        results = results.get("items", [])
    if not results:
        click.echo("No results.")
        return
    for r in results:
        if not isinstance(r, dict):
            continue
        content = r.get("content", "")[:200]
        mem_type = r.get("memory_type", "?")
        item_id = r.get("item_id", "?")
        # CORE-PROPS-1: Tilde marker for low-confidence memories
        conf = r.get("confidence", 1.0)
        conf_marker = "~" if isinstance(conf, (int, float)) and conf < 0.5 else ""
        # CORE-PROPS-1 Phase 2: stale marker
        stale_marker = "⚠" if r.get("stale") else ""
        click.echo(f"{stale_marker}{conf_marker}[{mem_type}] {item_id[:8]}  {content}")


def _why_remote_request(cfg, method: str, path: str, **kwargs) -> dict:
    """Send an authenticated read request to the hosted SmartMemory service."""
    import httpx

    from smartmemory_app.config import get_api_key

    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
        "X-Workspace-Id": cfg.team_id,
    }
    api_url = cfg.api_url.rstrip("/")
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.request(
                method,
                f"{api_url}{path}",
                headers=headers,
                timeout=30,
                **kwargs,
            )
        response.raise_for_status()
        return response.json()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
        raise click.ClickException(
            f"Could not reach the SmartMemory service at {api_url}."
        )
    except httpx.ReadTimeout:
        raise click.ClickException("SmartMemory service did not respond in time.")
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except Exception:
            detail = exc.response.text
        raise click.ClickException(str(detail))
    except httpx.HTTPError as exc:
        raise click.ClickException(f"SmartMemory service request failed: {exc}")


def _why_title(item: dict) -> str:
    """Return a human-readable title for a decision or memory item."""
    return str(item.get("content") or item.get("title") or "(untitled)")


def _why_date(item: dict) -> str:
    """Return the most useful available display date for an item.

    Decisions carry top-level created_at/updated_at; memory evidence resolved by
    the provenance route carries its timestamp in metadata.created_at (top-level
    created_at is absent), with bi-temporal transaction_time as a last resort.
    """
    metadata = item.get("metadata") or {}
    return str(
        item.get("created_at")
        or item.get("updated_at")
        or (metadata.get("created_at") if isinstance(metadata, dict) else None)
        or item.get("transaction_time")
        or "unknown date"
    )


def _why_truncate(content: object, limit: int = 100) -> str:
    """Truncate content to a readable terminal preview."""
    text = str(content or "")
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _render_why_provenance(
    question: str, provenance: dict, matches: list[dict]
) -> None:
    """Render hosted decision provenance in a concise terminal form."""
    decision = provenance.get("decision") or {}
    click.echo(f"Q: {question}\n")
    click.echo(
        f"DECISION  {_why_title(decision)}  ({decision.get('status', 'unknown')}, {_why_date(decision)})"
    )
    rationale = decision.get("rationale") or decision.get("reasoning")
    if rationale:
        click.echo(f"  rationale: {rationale}")

    superseded = provenance.get("superseded") or []
    if superseded:
        click.echo(
            "  supersedes: "
            + ", ".join(
                f"{_why_title(item)} ({_why_date(item)})" for item in superseded
            )
        )

    evidence = provenance.get("evidence") or []
    if evidence:
        click.echo("  evidence:")
        for item in evidence:
            memory = item.get("memory", item) if isinstance(item, dict) else {}
            if not isinstance(memory, dict):
                continue
            click.echo(
                f"    - [{memory.get('memory_type', '?')}] "
                f"{_why_truncate(memory.get('content'))} ({_why_date(memory)})"
            )

    also_matched = [_why_title(item) for item in matches[1:] if isinstance(item, dict)]
    if also_matched:
        click.echo("\nAlso matched: " + ", ".join(also_matched))


@cli.command("why")
@click.argument("question")
@click.option("--top-k", default=3, show_default=True, type=click.IntRange(min=1))
@click.option("--json", "as_json", is_flag=True, help="Print raw provenance JSON.")
def why_cmd(question: str, top_k: int, as_json: bool) -> None:
    """Explain the closest decision or derivation for QUESTION."""
    from smartmemory_app.config import load_config

    cfg = load_config()
    if cfg.mode == "remote":
        # The service route uses topic/limit and returns {"decisions": [...]}.
        search = _why_remote_request(
            cfg,
            "GET",
            "/memory/decisions/search",
            params={"topic": question, "limit": top_k},
        )
        matches = search.get("decisions", []) if isinstance(search, dict) else []
        if not matches:
            click.echo("No decisions matched that question.", err=True)
            raise SystemExit(1)
        decision_id = matches[0].get("decision_id")
        if not decision_id:
            raise click.ClickException(
                "SmartMemory service returned a decision without an ID."
            )
        provenance = _why_remote_request(
            cfg,
            "GET",
            f"/memory/decisions/{decision_id}/provenance",
        )
        if as_json:
            click.echo(json.dumps(provenance))
            return
        _render_why_provenance(question, provenance, matches)
        return

    click.echo(
        "Note: full decision provenance (supersession, evidence) requires service mode; "
        "showing derivation lineage of the closest match."
    )
    results = _daemon_request(
        "POST", "/memory/search", json={"query": question, "top_k": top_k}
    )
    if results is None:
        raise click.ClickException(_DAEMON_NOT_RUNNING_MSG)
    hits = results.get("items", []) if isinstance(results, dict) else results or []
    if not hits:
        click.echo("No memories matched that question.", err=True)
        raise SystemExit(1)
    item_id = hits[0].get("item_id") if isinstance(hits[0], dict) else None
    if not item_id:
        raise click.ClickException(
            "SmartMemory daemon returned a memory without an ID."
        )
    lineage_result = _daemon_request("GET", f"/memory/{item_id}/lineage")
    if lineage_result is None:
        raise click.ClickException(_DAEMON_NOT_RUNNING_MSG)
    lineage = (
        lineage_result.get("lineage", []) if isinstance(lineage_result, dict) else []
    )
    for item in lineage:
        if not isinstance(item, dict):
            continue
        click.echo(
            f"[{item.get('memory_type', '?')}] {item.get('content', '')} "
            f"({str(item.get('item_id', ''))[:8]})"
        )


@cli.command("get")
@click.argument("item_id")
def get_cmd(item_id: str) -> None:
    """Fetch a single memory by item ID."""
    try:
        result = _daemon_request("GET", f"/memory/{item_id}")
    except click.ClickException:
        raise  # surface daemon HTTP errors cleanly
    except Exception:
        result = None

    if result is None:
        from smartmemory_app.storage import get

        result = get(item_id)

    if not result:
        click.echo("Memory not found.", err=True)
        raise SystemExit(1)

    click.echo(json.dumps(result, indent=2, sort_keys=True))


# ── Lifecycle (DIST-AGENT-HOOKS-1) ──────────────────────────────────────────


@cli.group("lifecycle")
def lifecycle_group() -> None:
    """Automatic memory lifecycle commands (called by hooks)."""


@lifecycle_group.command("orient")
def lifecycle_orient() -> None:
    """Orient phase: recall context at session start."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    out = _lifecycle_via_daemon("/lifecycle/orient", body)
    if out is not None:
        result = out.get("context") or ""
    else:
        session_id = body.get("session_id", "unknown")
        cwd = body.get("cwd")

        from smartmemory_app.lifecycle import MemoryLifecycle
        from smartmemory_app.lifecycle_config import LifecycleConfig

        lc = MemoryLifecycle(
            session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
        )
        result = lc.orient(cwd=cwd)
    if result:
        click.echo(result)


def _as_text(value) -> str:
    """Deprecated shim — canonical implementation lives in lifecycle.as_text.

    Kept so the daemon path (lifecycle_api) and the in-process path cannot drift
    apart again; see that docstring for the dict-slicing crash it prevents.
    """
    from smartmemory_app.lifecycle import as_text

    return as_text(value)


@lifecycle_group.command("recall")
def lifecycle_recall() -> None:
    """Recall phase: inject prompt-relevant context."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    out = _lifecycle_via_daemon("/lifecycle/recall", body)
    if out is not None:
        result = out.get("context") or ""
    else:
        session_id = body.get("session_id", "unknown")
        prompt = body.get("prompt", "")

        from smartmemory_app.lifecycle import MemoryLifecycle
        from smartmemory_app.lifecycle_config import LifecycleConfig

        lc = MemoryLifecycle(
            session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
        )
        result = lc.recall(prompt, cwd=body.get("cwd"))
    if result:
        click.echo(result)


@lifecycle_group.command("observe")
def lifecycle_observe() -> None:
    """Observe phase: capture tool call."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    if _lifecycle_via_daemon("/lifecycle/observe", body) is not None:
        return

    session_id = body.get("session_id", "unknown")

    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    lc = MemoryLifecycle(
        session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
    )
    lc.observe(
        tool_name=body.get("tool_name", "unknown"),
        tool_input=body.get("tool_input", {}),
        tool_result=_as_text(body.get("tool_response")),
        transcript_path=body.get("transcript_path"),
        cwd=body.get("cwd"),
    )


@lifecycle_group.command("distill")
def lifecycle_distill() -> None:
    """Distill phase: pair response with stored prompt."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    if _lifecycle_via_daemon("/lifecycle/distill", body) is not None:
        return

    session_id = body.get("session_id", "unknown")

    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    lc = MemoryLifecycle(
        session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
    )
    lc.distill(response=body.get("last_assistant_message", ""))


@lifecycle_group.command("learn")
def lifecycle_learn() -> None:
    """Learn phase: capture error pattern."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    if _lifecycle_via_daemon("/lifecycle/learn", body) is not None:
        return

    session_id = body.get("session_id", "unknown")

    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    lc = MemoryLifecycle(
        session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
    )
    lc.learn(
        tool_name=body.get("tool_name", "unknown"),
        error=_as_text(body.get("error") or body.get("tool_response")),
    )


@lifecycle_group.command("persist")
def lifecycle_persist() -> None:
    """Persist phase: save session summary."""
    import sys

    body = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}

    if _lifecycle_via_daemon("/lifecycle/persist", body) is not None:
        return

    session_id = body.get("session_id", "unknown")

    from smartmemory_app.lifecycle import MemoryLifecycle
    from smartmemory_app.lifecycle_config import LifecycleConfig

    lc = MemoryLifecycle(
        session_id, LifecycleConfig.from_config(_load_lifecycle_toml())
    )
    lc.persist()


@lifecycle_group.command("status")
def lifecycle_status() -> None:
    """Show lifecycle configuration and session stats."""
    from smartmemory_app.lifecycle_config import LifecycleConfig

    cfg = LifecycleConfig.from_config(_load_lifecycle_toml())
    click.echo(f"Lifecycle enabled: {cfg.enabled}")
    click.echo(f"Recall strategy: {cfg.recall_strategy.value}")
    click.echo(f"Orient budget: {cfg.orient_budget} tokens")
    click.echo(f"Recall budget: {cfg.recall_budget} tokens")
    click.echo(
        f"Observe: {cfg.observe_tool_calls}, Distill: {cfg.distill_turns}, Learn: {cfg.learn_from_errors}"
    )


def _load_lifecycle_toml() -> dict:
    """Load [lifecycle] section from config.toml."""
    try:
        import tomllib
        from smartmemory_app.config import config_path

        path = config_path()
        if path.exists():
            with open(path, "rb") as f:
                return tomllib.load(f).get("lifecycle", {})
    except Exception:
        pass
    return {}


# ── Discovery + config ──────────────────────────────────────────────────────


@cli.command("models")
@click.option(
    "--provider",
    default=None,
    help="Filter by provider (ollama, lmstudio, groq, openai)",
)
def models_cmd(provider: str | None) -> None:
    """List available models from local and cloud providers."""
    import httpx

    providers_to_check = (
        [provider] if provider else ["ollama", "lmstudio", "groq", "openai"]
    )

    for p in providers_to_check:
        if p == "ollama":
            try:
                r = httpx.get("http://localhost:11434/api/tags", timeout=3)
                r.raise_for_status()
                models = r.json().get("models", [])
                if models:
                    click.echo(f"\nOllama ({len(models)} models):")
                    for m in models:
                        name = m.get("name", "?")
                        size = m.get("size", 0)
                        size_gb = f"{size / 1e9:.1f}GB" if size else "?"
                        click.echo(f"  ollama/{name}  ({size_gb})")
                else:
                    click.echo("\nOllama: running but no models pulled")
            except Exception:
                click.echo("\nOllama: not running (start with: ollama serve)")

        elif p == "lmstudio":
            try:
                r = httpx.get("http://localhost:1234/v1/models", timeout=3)
                r.raise_for_status()
                models = r.json().get("data", [])
                if models:
                    click.echo(f"\nLM Studio ({len(models)} models):")
                    for m in models:
                        click.echo(f"  lmstudio/{m.get('id', '?')}")
                else:
                    click.echo("\nLM Studio: running but no models loaded")
            except Exception:
                click.echo(
                    "\nLM Studio: not running (start LM Studio and load a model)"
                )

        elif p == "groq":
            import os

            key = os.environ.get("GROQ_API_KEY")
            if not key:
                click.echo("\nGroq: GROQ_API_KEY not set (get one at console.groq.com)")
                continue
            try:
                r = httpx.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=5,
                )
                r.raise_for_status()
                models = sorted(r.json().get("data", []), key=lambda m: m.get("id", ""))
                if models:
                    click.echo(f"\nGroq ({len(models)} models):")
                    for m in models:
                        click.echo(f"  groq/{m.get('id', '?')}")
            except Exception as e:
                click.echo(f"\nGroq: API error ({e})")

        elif p == "openai":
            import os

            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                click.echo("\nOpenAI: OPENAI_API_KEY not set")
                continue
            try:
                r = httpx.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=5,
                )
                r.raise_for_status()
                models = sorted(r.json().get("data", []), key=lambda m: m.get("id", ""))
                chat_models = [
                    m
                    for m in models
                    if any(
                        m.get("id", "").startswith(prefix)
                        for prefix in ("gpt-4", "gpt-3.5", "o1", "o3", "o4")
                    )
                ]
                if chat_models:
                    click.echo(f"\nOpenAI ({len(chat_models)} chat models):")
                    for m in chat_models:
                        click.echo(f"  openai/{m.get('id', '?')}")
            except Exception as e:
                click.echo(f"\nOpenAI: API error ({e})")

        elif p == "claude-agent":
            click.echo(
                "\nClaude Agent SDK: uses Claude Code OAuth (no model selection needed)"
            )

        elif p == "anthropic":
            click.echo(
                "\nAnthropic: claude-3-5-haiku-latest, claude-sonnet-4-5-20250514, claude-opus-4-6-20250603"
            )


@cli.command("doctor")
def doctor_cmd() -> None:
    """Diagnose a broken install (DIST-INSTALL-RESOLVE-1).

    Detects the pip-backtracked-wrapper trap: a fresh `pip install smartmemory`
    in a polluted environment can backtrack to an ancient wrapper that pins a
    `smartmemory-core` from the dead-`/auth/me` era, which 401s at first use.
    Checks the installed core against a conservative floor and reports the
    Python version.
    """
    import sys
    from importlib.metadata import version as _pkg_version, PackageNotFoundError

    ok = True

    # ── Python version ──────────────────────────────────────────────────────
    py = sys.version_info
    py_str = f"{py.major}.{py.minor}.{py.micro}"
    if py >= (3, 11):
        click.echo(f"✓ Python {py_str} (>=3.11 OK)")
    else:
        ok = False
        click.echo(
            f"✗ Python {py_str} is too old — SmartMemory requires >=3.11. "
            "Old Python forces pip to backtrack onto a legacy wrapper.",
            err=True,
        )

    # ── smartmemory-core version ────────────────────────────────────────────
    try:
        core = _pkg_version("smartmemory-core")
    except PackageNotFoundError:
        ok = False
        click.echo(
            "✗ smartmemory-core is not installed. Reinstall in a clean venv:\n"
            "    python -m venv .venv && source .venv/bin/activate\n"
            "    pip install smartmemory",
            err=True,
        )
    else:
        if _version_lt(core, MIN_CORE_VERSION):
            ok = False
            click.echo(
                f"✗ smartmemory-core {core} is too old (floor is {MIN_CORE_VERSION}).\n"
                "  You likely have a pip-backtracked install: pip could not satisfy the\n"
                "  current wrapper's dependency tree in this environment and walked back to\n"
                "  an ancient wrapper (e.g. 1.1.5 → core 0.7.1) that talks to a dead API and\n"
                "  401s at first use. This usually happens when installing into a polluted\n"
                "  environment (e.g. the Anaconda base env).\n"
                "  Fix: reinstall in a clean, isolated environment:\n"
                "    python -m venv .venv && source .venv/bin/activate\n"
                "    pip install --upgrade smartmemory",
                err=True,
            )
        else:
            click.echo(f"✓ smartmemory-core {core} OK")

    if not ok:
        raise SystemExit(1)
    click.echo("\nAll checks passed.")


@cli.command("config")
@click.argument("key", required=False)
@click.argument("value", required=False)
def config_cmd(key: str | None, value: str | None) -> None:
    """View or change SmartMemory configuration.

    \b
    smartmemory config                  Show all settings
    smartmemory config llm_provider     Show one setting
    smartmemory config llm_provider groq  Change a setting
    """
    from smartmemory_app.config import load_config, save_config, config_path

    cfg = load_config()

    if key is None:
        from smartmemory_app import __version__ as wrapper_version

        try:
            from importlib.metadata import version as _pkg_version

            core_version = _pkg_version("smartmemory-core")
        except Exception:
            core_version = "?"
        click.echo(f"SmartMemory v{wrapper_version} (core {core_version})")
        click.echo(f"Config: {config_path()}\n")
        click.echo(f"  mode              = {cfg.mode}")
        click.echo(f"  llm_provider      = {cfg.llm_provider}")
        click.echo(f"  llm_model         = {cfg.llm_model or '(auto)'}")
        click.echo(f"  embedding_provider = {cfg.embedding_provider}")
        click.echo(f"  daemon_port       = {cfg.daemon_port}")
        click.echo(f"  coreference       = {cfg.coreference}")
        click.echo(f"  data_dir          = {cfg.data_dir}")
        click.echo(f"  api_url           = {cfg.api_url}")
        click.echo(f"  api_key_set       = {cfg.api_key_set}")
        click.echo(f"  team_id           = {cfg.team_id or '(none)'}")
        return

    settable = {
        "llm_provider": {
            "values": [
                "groq",
                "claude-agent",
                "anthropic",
                "openai",
                "ollama",
                "lmstudio",
                "none",
            ]
        },
        "llm_model": {"values": None},
        "embedding_provider": {"values": ["local", "openai", "ollama"]},
        "daemon_port": {"values": None},
        "coreference": {"values": ["true", "false"]},
        "data_dir": {"values": None},
        "mode": {"values": ["local", "remote"]},
    }

    if key not in settable:
        current = getattr(cfg, key, None)
        if current is not None:
            click.echo(f"{key} = {current}")
        else:
            click.echo(f"Unknown key: {key}")
            click.echo(f"Settable keys: {', '.join(settable)}")
        return

    if value is None:
        click.echo(f"{key} = {getattr(cfg, key)}")
        allowed = settable[key]["values"]
        if allowed:
            click.echo(f"Allowed: {', '.join(allowed)}")
        return

    allowed = settable[key]["values"]
    if allowed and value not in allowed:
        click.echo(f"Invalid value '{value}'. Allowed: {', '.join(allowed)}")
        return

    coerced: object = value
    if key == "coreference":
        coerced = value.lower() == "true"
    elif key == "daemon_port":
        coerced = int(value)

    setattr(cfg, key, coerced)
    save_config(cfg)
    click.echo(f"{key} = {value}")


# ── Admin subgroup ─────────────────────────────────────────────────────────


@cli.group("admin")
def admin_group() -> None:
    """Administrative commands (import, export, reindex, etc.)."""


@admin_group.command("import")
@click.argument("path", type=click.Path(exists=True), metavar="OKF_BUNDLE_DIR")
@click.option(
    "--mode",
    "import_mode",
    default="full",
    type=click.Choice(["full", "direct"]),
    show_default=True,
    help="full=ingest pipeline, direct=add() skip extraction",
)
@click.option(
    "--batch-size", default=100, show_default=True, help="Checkpoint interval"
)
@click.option("--resume", is_flag=True, help="Resume from last checkpoint")
@click.option("--dry-run", is_flag=True, help="Validate only, don't import")
@click.option("--domain-filter", default=None, help="Filter by metadata.domain")
@click.option(
    "--legacy-jsonl",
    is_flag=True,
    help="Read the legacy single-file JSONL corpus instead of an OKF bundle directory",
)
def import_cmd(
    path: str,
    import_mode: str,
    batch_size: int,
    resume: bool,
    dry_run: bool,
    domain_filter: str | None,
    legacy_jsonl: bool,
) -> None:
    """Import an OKF bundle directory into SmartMemory (default).

    PATH is an OKF bundle directory by default; pass ``--legacy-jsonl`` to read
    a legacy single-file JSONL corpus.
    """
    from smartmemory.corpus.importer import CorpusImporter

    if legacy_jsonl:
        from smartmemory.corpus.reader import CorpusReader

        reader = CorpusReader(path)
        header = reader.read_header()
        click.echo(
            f"Corpus: source={header.source}, domain={header.domain or '(none)'}"
        )
        total = header.item_count or reader.count_records()
    else:
        from smartmemory.okf import is_reserved

        total = sum(
            1 for page in Path(path).rglob("*.md") if not is_reserved(page.name)
        )
        click.echo(f"OKF bundle: {total} pages")

    if dry_run:
        click.echo("Dry run — validating records...")

    try:
        from rich.progress import (
            Progress,
            SpinnerColumn,
            TimeElapsedColumn,
            MofNCompleteColumn,
        )

        use_rich = True
    except ImportError:
        use_rich = False

    if not dry_run:
        from smartmemory_app.storage import get_memory

        sm = get_memory()
    else:
        sm = None

    importer = CorpusImporter(
        smart_memory=sm,
        mode=import_mode,
        batch_size=batch_size,
        domain_filter=domain_filter,
        legacy_jsonl=legacy_jsonl,
    )

    if use_rich and total > 0:
        with Progress(
            SpinnerColumn(),
            "[progress.description]{task.description}",
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Importing", total=total)

            def on_progress(stats, record):
                progress.update(task, completed=stats.total)

            stats = importer.run(
                path, resume=resume, dry_run=dry_run, progress_callback=on_progress
            )
    else:
        stats = importer.run(path, resume=resume, dry_run=dry_run)

    action = "Validated" if dry_run else "Imported"
    click.echo(
        f"{action} {stats.imported} records ({stats.errors} errors, {stats.rate:.1f} items/sec)"
    )


@admin_group.command("export")
@click.argument("path", type=click.Path(), metavar="OKF_BUNDLE_DIR")
@click.option("--memory-type", default=None, help="Filter by memory type")
@click.option(
    "--include-entities", is_flag=True, help="Include extracted entities/relations"
)
@click.option("--limit", default=0, help="Max records to export (0=all)")
@click.option(
    "--source",
    default="smartmemory-export",
    help="Source tag in the legacy JSONL corpus header only",
)
@click.option(
    "--domain", default="", help="Domain tag in the legacy JSONL corpus header only"
)
@click.option(
    "--legacy-jsonl",
    is_flag=True,
    help="Write the legacy single-file JSONL corpus instead of an OKF bundle directory",
)
def export_cmd(
    path: str,
    memory_type: str | None,
    include_entities: bool,
    limit: int,
    source: str,
    domain: str,
    legacy_jsonl: bool,
) -> None:
    """Export memories to an OKF bundle directory (default).

    PATH is an OKF bundle directory by default; pass ``--legacy-jsonl`` to
    write a legacy single-file JSONL corpus. ``--source`` and ``--domain``
    apply only to legacy JSONL output.
    """
    from smartmemory.corpus.exporter import CorpusExporter
    from smartmemory_app.storage import get_memory

    sm = get_memory()
    exporter = CorpusExporter(
        smart_memory=sm,
        memory_type=memory_type,
        include_entities=include_entities,
        limit=limit,
        legacy_jsonl=legacy_jsonl,
    )
    count = exporter.run(path, source=source, domain=domain)
    output_format = "legacy JSONL" if legacy_jsonl else "OKF bundle"
    click.echo(f"Exported {count} records to {path} ({output_format})")


# `sm export` / `sm import` aliases reuse the administrative command objects.
cli.add_command(export_cmd, name="export")
cli.add_command(import_cmd, name="import")


# ── Wikidata mining ────────────────────────────────────────────────────────


@admin_group.command("mine")
@click.option(
    "--domain", "domain_qid", default=None, help="Single P31 QID (e.g. Q9143)"
)
@click.option(
    "--domain-file",
    default=None,
    type=click.Path(exists=True),
    help="JSON config file with multiple domains",
)
@click.option("--all-defaults", is_flag=True, help="Mine expanded default domains")
@click.option("--incremental", default=None, help="Only entities newer than ISO date")
@click.option(
    "--limit", default=5000, show_default=True, help="Per-domain entity limit"
)
@click.option("--output", "-o", default="./mining-output/", help="Output directory")
@click.option(
    "--format",
    "output_format",
    default="both",
    type=click.Choice(["corpus", "snapshot", "both"]),
    show_default=True,
)
@click.option(
    "--quota-limit", default=0, help="Max SPARQL queries per run (0=unlimited)"
)
def mine_cmd(
    domain_qid: str | None,
    domain_file: str | None,
    all_defaults: bool,
    incremental: str | None,
    limit: int,
    output: str,
    output_format: str,
    quota_limit: int,
) -> None:
    """Mine Wikidata for entities via SPARQL."""
    from smartmemory.grounding.miner import (
        EXPANDED_DOMAINS,
        WikidataMiner,
        load_domain_config,
    )

    if domain_file:
        domains = load_domain_config(domain_file)
    elif domain_qid:
        domains = {domain_qid: domain_qid}
    elif all_defaults:
        domains = EXPANDED_DOMAINS
    else:
        click.echo("Specify --domain, --domain-file, or --all-defaults")
        raise SystemExit(1)

    miner = WikidataMiner(quota_limit=quota_limit, limit_per_domain=limit)
    click.echo(f"Mining {len(domains)} domain(s)...")
    result = miner.mine_domains(
        domains=domains,
        output_dir=output,
        output_format=output_format,
        incremental_since=incremental,
    )
    click.echo(
        f"Mined {len(result.entities)} unique entities ({result.total_queries} queries)"
    )
    if result.quota_exhausted:
        click.echo("Quota exhausted — resume with same command to continue")
    for domain_name, count in result.domain_counts.items():
        click.echo(f"  {domain_name}: {count}")


# ── REBEL conversion ───────────────────────────────────────────────────────


@admin_group.command("convert-rebel")
@click.option(
    "--output", "-o", required=True, type=click.Path(), help="Output corpus JSONL path"
)
@click.option("--limit", default=0, help="Max samples to convert (0=all)")
@click.option("--domain", default=None, help="Domain keyword filter (tech, science)")
@click.option(
    "--split", default="train", show_default=True, help="HuggingFace dataset split"
)
def convert_rebel_cmd(output: str, limit: int, domain: str | None, split: str) -> None:
    """Convert REBEL dataset (HuggingFace) to corpus JSONL."""
    from smartmemory.corpus.rebel import REBELConverter

    converter = REBELConverter(domain=domain, limit=limit, split=split)
    click.echo(f"Converting REBEL ({domain or 'all'}, limit={limit or 'unlimited'})...")
    count = converter.convert_to_file(output)
    click.echo(f"Wrote {count} records to {output}")


# ── Seed packs ─────────────────────────────────────────────────────────────


@admin_group.command("list-packs")
def list_packs_cmd() -> None:
    """List available seed packs from the registry."""
    from smartmemory.corpus.registry import PackRegistry

    registry = PackRegistry()
    click.echo("Fetching pack registry...")
    packs = registry.fetch()
    if not packs:
        click.echo("No seed packs available yet. Pack registry coming soon.")
        click.echo("Track progress: https://docs.smartmemory.ai/smartmemory/seed-packs")
        return
    click.echo(f"\n{'Name':<25} {'Version':<10} {'Size':<8} {'Domain':<15} Description")
    click.echo("-" * 80)
    for p in packs:
        click.echo(
            f"{p.name:<25} {p.version:<10} {p.size_mb:<8.1f} {p.domain:<15} {p.description}"
        )


@admin_group.command("install-pack")
@click.argument("name")
@click.option(
    "--source",
    default=None,
    type=click.Path(exists=True),
    help="Local pack directory (skip registry download)",
)
@click.option(
    "--mode",
    "install_mode",
    default="direct",
    type=click.Choice(["full", "direct"]),
    show_default=True,
)
@click.option("--skip-patterns", is_flag=True, help="Skip EntityRuler pattern import")
@click.option("--skip-entities", is_flag=True, help="Skip grounding entity import")
def install_pack_cmd(
    name: str,
    source: str | None,
    install_mode: str,
    skip_patterns: bool,
    skip_entities: bool,
) -> None:
    """Install a seed pack into SmartMemory."""
    from pathlib import Path
    from smartmemory.corpus.pack import InstalledPacks, SeedPack
    from smartmemory_app.storage import get_memory, _resolve_data_dir

    if source:
        pack_dir = source
    else:
        # Check bundled packs first
        bundled = (
            Path(__file__).parent.parent
            / "smart-memory-core"
            / "smartmemory"
            / "data"
            / "seed_packs"
            / name
        )
        if bundled.exists():
            pack_dir = str(bundled)
        else:
            from smartmemory.corpus.registry import PackRegistry

            registry = PackRegistry()
            registry.fetch()
            pack_info = registry.get(name)
            if not pack_info:
                click.echo(f"Pack '{name}' not found in registry.")
                raise SystemExit(1)
            pack_dir = registry.download(pack_info, str(_resolve_data_dir() / "packs"))

    pack = SeedPack(pack_dir)
    errors = pack.validate()
    if errors:
        click.echo(f"Invalid pack: {'; '.join(errors)}")
        raise SystemExit(1)

    sm = get_memory()
    installed = InstalledPacks(_resolve_data_dir())
    click.echo(f"Installing pack: {pack.manifest.name} v{pack.manifest.version}...")
    counts = pack.install(
        smart_memory=sm,
        installed_packs=installed,
        mode=install_mode,
        skip_patterns=skip_patterns,
        skip_entities=skip_entities,
    )
    click.echo(
        f"Installed: {counts['corpus']} memories, {counts['patterns']} patterns, {counts['entities']} entities"
    )


# ── Data management ─────────────────────────────────────────────────────────


@cli.command("clear")
@click.confirmation_option(prompt="This will delete all local memories. Are you sure?")
def clear_cmd() -> None:
    """Delete all local memories and reset the vector index."""
    result = _daemon_request("POST", "/memory/clear")
    if result is not None:
        click.echo(f"Cleared {result.get('cleared', '?')} files via daemon.")
        return

    # Daemon not running — clear files directly
    from smartmemory_app.storage import _resolve_data_dir, _shutdown

    _shutdown()

    data_path = _resolve_data_dir()
    if not data_path.exists():
        click.echo("No data directory found. Nothing to clear.")
        return

    removed = 0
    for pattern in [
        "*.db",
        "*.db-shm",
        "*.db-wal",
        "*.db-journal",
        "*.usearch",
        "*.json",
        "*.jsonl",
        "*.log",
        ".write.lock",
    ]:
        for f in data_path.glob(pattern):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                click.echo(f"Warning: could not remove {f.name}: {e}")

    click.echo(f"Cleared {removed} files from {data_path}")

    from smartmemory_app.setup import _seed_data_dir

    _seed_data_dir()
    click.echo("Re-seeded entity patterns.")


# ── Admin: reindex ─────────────────────────────────────────────────────────


@admin_group.command("reindex")
def reindex_cmd() -> None:
    """Re-embed all memories with the current embedding model."""
    from smartmemory_app.config import load_config

    cfg = load_config()
    if cfg.mode == "remote":
        raise click.ClickException("Reindex is only available in local mode.")
    result = _daemon_request("POST", "/memory/reindex")
    if result is not None:
        click.echo(
            f"Reindexed {result.get('reindexed', '?')} memories "
            f"({result.get('dims', '?')}d, {result.get('provider', '?')}, "
            f"{result.get('elapsed_s', '?')}s)"
        )
    else:
        raise click.ClickException(
            "Daemon is not running. Start it first: smartmemory start"
        )


@admin_group.command("reextract")
def reextract_cmd() -> None:
    """Re-run entity extraction on all memories to populate the knowledge graph.

    Use after upgrading to rebuild entity nodes and edges for memories that
    were stored before entity extraction was available on lite mode.
    """
    from smartmemory_app.config import load_config

    cfg = load_config()
    if cfg.mode == "remote":
        raise click.ClickException("Reextract is only available in local mode.")
    click.echo("Re-extracting entities from all memories...")
    result = _daemon_request("POST", "/memory/reextract", timeout=300)
    if result is not None:
        click.echo(
            f"Done: {result.get('extracted', 0)} memories processed, "
            f"{result.get('entities_created', 0)} new entity nodes, "
            f"{result.get('skipped', 0)} skipped, "
            f"{result.get('elapsed_s', '?')}s"
        )
    else:
        raise click.ClickException(
            "Daemon is not running. Start it first: smartmemory start"
        )


# ── Hidden/internal ─────────────────────────────────────────────────────────


@cli.command("server", hidden=True)
def server_cmd() -> None:
    """Start the SmartMemory MCP server (called by MCP clients, not users)."""
    click.echo(
        "MCP server is now 'smartmemory-mcp'. It's included in your installation."
    )
    click.echo("Run: smartmemory-mcp")


@cli.command("events-server", hidden=True)
@click.option("--port", default=9015, show_default=True, help="WebSocket port")
def events_server_cmd(port: int) -> None:
    """Run the lite WebSocket events server standalone (debugging only)."""
    from smartmemory_app.events_server import main

    main(port=port)


if __name__ == "__main__":
    cli()
