"""`sm code` subgroup — code repository indexing.

Wraps local or remote ``ingest_code`` and emits structured progress to stdout.
Remote mode parses the checkout locally and uploads entities and relations to
the configured hosted workspace without opening a local memory store.

Progress streaming
------------------
``ingest_code`` is a synchronous bulk operation; we stream progress around it
rather than inside it (the indexer logs its own debug lines but doesn't
publish per-file events). The CLI prints structured JSON-ish lines marking
phase boundaries so callers can pipe-parse:

    [code:index] phase=start repo=<repo> path=<path> langs=<...>
    [code:index] phase=done repo=<repo> files=<n> entities=<n> edges=<n> embeddings=<n> elapsed_s=<s>

Errors are printed prefixed with ``[code:index] error:``.

Origin
------
Every entity created by ``CodeIndexer.to_properties()`` already sets
``origin = "code:index"`` (Tier 1 user content per
``smartmemory/origin_policy.py``); this command does not need to set it
explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import click

log = logging.getLogger(__name__)


# Default exclusions for "real" repos. Kept tight on purpose — tree-sitter
# can choke on minified vendor bundles, and node_modules / venvs blow up the
# parser pass.
_DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "tmp",
}


def _detect_repo_name(path: Path) -> str:
    """Best-effort repo identifier — folder name, falling back to git remote."""
    name = path.resolve().name
    if name and name not in {"", ".", "/"}:
        return name
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url.rsplit("/", 1)[-1].removesuffix(".git") or "repo"
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return "repo"


@click.group("code")
def code_group() -> None:
    """Code repository indexing commands."""


@code_group.command("index")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--repo", default=None, help="Repo identifier (defaults to folder name).")
@click.option(
    "--language",
    "--languages",
    "languages",
    multiple=True,
    type=click.Choice(["python", "typescript"]),
    help="Narrow indexing to these languages. Repeatable. Defaults to every supported "
    "language present (python, typescript/javascript).",
)
@click.option(
    "--exclude",
    "extra_excludes",
    multiple=True,
    help="Additional directory names to exclude (repeatable).",
)
@click.option(
    "--commit-hash",
    default=None,
    help="Override git commit SHA stamped on every entity. "
    "Defaults to auto-detect via `git rev-parse HEAD`. "
    "Pass empty string to suppress auto-detection.",
)
def code_index_cmd(
    path: str,
    repo: str | None,
    languages: tuple[str, ...],
    extra_excludes: tuple[str, ...],
    commit_hash: str | None,
) -> None:
    """Index a code repository into the knowledge graph.

    Parses every supported language present (Python and TypeScript/JavaScript,
    including .mjs/.cjs/.mts/.cts), writes code entities + relationships as graph
    nodes/edges, and generates vector embeddings. Every node is tagged with
    origin=code:index (Tier 1). --language narrows the set.

    \b
    Examples:
        sm code index .
        sm code index ~/repos/myapp --repo myapp --language python
    """
    repo_path = Path(path).resolve()
    repo_id = repo or _detect_repo_name(repo_path)
    # None = core's default: every supported language present (CODE-INDEXER-HARDEN-1 F32).
    lang_list = list(languages) if languages else None
    lang_label = ",".join(lang_list) if lang_list else "all"
    excludes = sorted(_DEFAULT_EXCLUDES | set(extra_excludes))

    click.echo(
        f"[code:index] phase=start repo={repo_id} path={repo_path} "
        f"langs={lang_label} excludes={len(excludes)}"
    )

    try:
        from smartmemory_app.launch_metrics import emit as _lm_emit

        _lm_emit("index.start", {"repo": repo_id, "languages": lang_list or ["all"]})
    except Exception:
        pass

    # Storage selects local Lite or the hosted adapter. Both parse in this
    # process, and the remote adapter uploads only the parsed contract fields.
    try:
        from smartmemory_app.storage import get_memory
    except Exception as exc:  # pragma: no cover — import failure is environmental
        raise click.ClickException(f"Could not import storage layer: {exc}")

    try:
        memory = get_memory()
    except Exception as exc:
        raise click.ClickException(
            f"SmartMemory is not configured: {exc}. Run: sm init"
        )

    if not hasattr(memory, "ingest_code"):
        log.warning("Code indexing refused: active backend lacks ingest_code")
        raise click.ClickException("Active backend does not support code indexing.")

    started = time.time()
    try:
        result = memory.ingest_code(
            directory=str(repo_path),
            repo=repo_id,
            commit_hash=commit_hash,
            exclude_dirs=excludes,
            languages=lang_list,
        )
    except Exception as exc:
        if hasattr(exc, "result"):
            _report_diagnostics(exc.result)
        click.echo(f"[code:index] error: {exc}", err=True)
        log.exception("code index failed")
        raise SystemExit(1)
    elapsed = time.time() - started
    _report_diagnostics(result)

    click.echo(
        f"[code:index] phase=done repo={repo_id} "
        f"files={result.files_parsed} skipped={result.files_skipped} "
        f"entities={result.entities_created} edges={result.edges_created} "
        f"embeddings={result.embeddings_generated} "
        f"elapsed_s={result.elapsed_seconds or round(elapsed, 2)}"
    )

    try:
        from smartmemory_app.launch_metrics import emit as _lm_emit

        _lm_emit(
            "index.complete",
            {
                "repo": repo_id,
                "files": result.files_parsed,
                "entities": result.entities_created,
                "edges": result.edges_created,
                "elapsed_s": float(result.elapsed_seconds or round(elapsed, 2)),
            },
        )
    except Exception:
        pass

    if result.errors:
        # Surface (but do not fail on) parse errors — IndexResult treats them
        # as soft warnings. Cap the dump so a busted vendor dir does not flood
        # stdout. NEVER swallow silently — the rule of no_silent_fallbacks.
        cap = 20
        click.echo(
            f"[code:index] warning: {len(result.errors)} parse error(s); showing first {min(cap, len(result.errors))}",
            err=True,
        )
        for line in result.errors[:cap]:
            click.echo(f"  - {line}", err=True)
    if not result.replaced:
        raise SystemExit(1)


def _report_diagnostics(result) -> None:
    """Report contracted counts and publication separately from transport acceptance."""
    sites = getattr(result, "call_sites", "unknown")
    site_summary = (
        " ".join(
            f"{key}={sites[key]}"
            for key in ("extracted", "unresolved", "name_only", "exact")
        )
        if isinstance(sites, dict)
        else "unknown"
    )
    click.echo(f"[code:index] source_call_sites {site_summary}")
    click.echo(
        f"[code:index] resolved_call_edges={getattr(result, 'resolved_call_edges', 'unknown')} (not recall)"
    )
    click.echo(
        f"[code:index] clean={result.files_clean} partial={result.files_partial} failed={result.files_failed} "
        f"acceptance={result.acceptance} staging={result.staging} publication={result.publication}"
    )
    click.echo(
        "[code:index] G16 complete-generation publication is not proven by 1.x acceptance."
    )
    for diagnostic in result.diagnostics:
        if diagnostic["status"] != "clean":
            click.echo(
                f"  {diagnostic['file_path']}: {diagnostic['status']} coverage={diagnostic['coverage']}",
                err=True,
            )
            for span in diagnostic.get("spans", [])[:20]:
                click.echo(f"    {span}", err=True)


def _is_git_ignored(root: Path, target: Path) -> bool:
    """True when git reports ``target`` as ignored in the checkout at ``root`` (False when git is unavailable)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", "--no-index", str(target)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def _write_atomic(out: Path, snapshot: dict) -> int:
    """Write ``snapshot`` to ``out`` through a temp file in the same directory, then ``os.replace``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{out.name}.", suffix=".tmp", dir=str(out.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                snapshot, stream, sort_keys=True, ensure_ascii=False, allow_nan=False
            )
            stream.write("\n")
        os.replace(temp_name, out)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return out.stat().st_size


@code_group.command("bundle")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--repo", required=True, help="Repo identifier stamped on the snapshot.")
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Snapshot file to write.",
)
@click.option(
    "--language",
    "--languages",
    "languages",
    multiple=True,
    type=click.Choice(["python", "typescript"]),
    help="Narrow to these languages. Repeatable. Defaults to every supported language present.",
)
@click.option(
    "--exclude",
    "extra_excludes",
    multiple=True,
    help="Additional directory names to exclude (repeatable).",
)
@click.option(
    "--allow-partial",
    is_flag=True,
    default=False,
    help="Write a snapshot even when some files failed to parse (complete=false, failed_paths lists them).",
)
@click.option(
    "--fields",
    type=click.Choice(["full", "minimal"]),
    default="full",
    show_default=True,
    help=(
        "minimal drops framework_evidence and clean per-entity parse diagnostics. "
        "The forge/stratum code-graph consumer (STRAT-CODEGRAPH-1) reads minimal by default and full is opt-in for it."
    ),
)
def code_bundle_cmd(
    path: str,
    repo: str,
    out_path: str,
    languages: tuple[str, ...],
    extra_excludes: tuple[str, ...],
    allow_partial: bool,
    fields: str,
) -> None:
    """Write a store-free local snapshot of a checkout (no backend, no upload, no embeddings).

    \b
    Examples:
        smartmemory code bundle . --repo myapp --out myapp.snapshot.json
        smartmemory code bundle . --repo myapp --out snap.json --allow-partial
    """
    from smartmemory.code.indexer import CodeIndexer
    from smartmemory.code.models import CodePreparationError

    root = Path(path).resolve()
    out = Path(out_path).resolve()
    excludes = sorted(_DEFAULT_EXCLUDES | set(extra_excludes))
    started = time.time()
    indexer = CodeIndexer(
        graph=None, repo=repo, repo_root=str(root), exclude_dirs=set(excludes)
    )
    try:
        snapshot, _result = indexer.prepare_snapshot(
            list(languages) if languages else None,
            allow_partial=allow_partial,
            fields=fields,
        )
    except (CodePreparationError, ValueError) as exc:
        click.echo(f"[code:bundle] error: {exc}", err=True)
        log.warning("code bundle refused for %s: %s", root, exc)
        raise SystemExit(1)

    try:
        out.relative_to(root)
    except ValueError:
        inside = False
    else:
        inside = True
    if inside and not _is_git_ignored(root, out):
        log.warning(
            "code bundle --out %s is inside the checkout and not git-ignored", out
        )
        click.echo(
            f"[code:bundle] warning: --out is inside the checkout and not git-ignored: {out}. "
            "The next snapshot will see it as an uncommitted change.",
            err=True,
        )

    try:
        size = _write_atomic(out, snapshot)
    except OSError as exc:
        click.echo(f"[code:bundle] error: could not write {out}: {exc}", err=True)
        raise SystemExit(1)
    click.echo(
        f"[code:bundle] repo={repo} entities={len(snapshot['entities'])} relations={len(snapshot['relations'])} "
        f"complete={str(snapshot['complete']).lower()} commit_hash={snapshot['commit_hash']} "
        f"bytes={size} elapsed_s={round(time.time() - started, 2)} out={out}"
    )
    if not snapshot["complete"]:
        failed = ", ".join(item["path"] for item in snapshot["failed_paths"])
        click.echo(
            f"[code:bundle] warning: incomplete snapshot, failed paths: {failed}",
            err=True,
        )


@code_group.command("effects")
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--repo", default=None, help="Repository identifier.")
@click.option(
    "--checkpoint-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Caller-owned per-source parse cache.",
)
def code_effects_cmd(path: str, repo: str | None, checkpoint_dir: str | None) -> None:
    """Print deterministic Python effects JSON without opening a memory store."""
    import json

    from smartmemory.code.effects import scan_effects

    root = Path(path).resolve()
    try:
        result = scan_effects(root, repo or root.name, checkpoint_dir=checkpoint_dir)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
