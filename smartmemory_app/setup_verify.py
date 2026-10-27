"""SETUP-RESUMABLE-1: the check behind every setup step.

``verify_step(key, choices)`` answers "is this step's effect still there?" for each
step of ``setup_steps.setup_plan``. ``setup_run`` calls it right after a step runs,
and again on a re-run to find the first step whose effect is gone.

Where a check already exists it is called, not re-derived:
  daemon     doctor._check_daemon (the "Local daemon" row of sm doctor)
  auth       doctor.check_credentials (the "API key" row)
  mode       config.load_config, the resolver every sm command uses, with
             doctor's env-source labels to name an inherited SMARTMEMORY_MODE
  roundtrip  the real sm add, sm get and sm search commands, in process, so the
             test memory takes the CLI's own route (daemon, hosted API, or the
             in-process fallback) and scope (scope_header.resolve_scope)

No check prints an env value: a setting that came from an env variable is shown
as ``$NAME``.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click

log = logging.getLogger(__name__)

# Shown after the mode check (CLI-LOCAL-MODE-1): which command changes the mode.
MODE_HELP = (
    "To change the mode later, run: sm config mode local (or remote). "
    "That is saved in the config file. An exported SMARTMEMORY_MODE overrides it "
    "for that shell only."
)

ROUND_TRIP_PREFIX = "SmartMemory setup check"
SEARCH_ATTEMPTS = 5
SEARCH_PAUSE_SECONDS = 2.0


@dataclass(frozen=True)
class Check:
    ok: bool
    detail: str = ""
    fix: str = ""


# ── helpers ─────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _env(**values: str):
    """Set meta switches for one nested call, then restore them."""
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _doctor_context():
    from smartmemory_app import doctor

    return doctor.Context(from_process=True)


def _row_check(row, *, accept_warn: bool = False) -> Check:
    from smartmemory_app.doctor import PASS, WARN

    ok = row.status == PASS or (accept_warn and row.status == WARN)
    return Check(ok, row.detail, "" if ok else row.fix)


# ── install steps ───────────────────────────────────────────────────────────

_LOCAL_FIELDS = (
    "llm_provider",
    "llm_model",
    "embedding_provider",
    "spacy_model",
    "coreference",
    "data_dir",
)


def verify_config(choices: dict) -> Check:
    """The config FILE holds what setup chose (env overrides are the mode check's job)."""
    from smartmemory_app.config import config_path, load_config

    path = config_path()
    if not path.exists():
        return Check(False, f"no config file at {path}", "Run: sm setup")
    cfg = load_config(apply_env=False)
    fields = ("mode",) + (_LOCAL_FIELDS if choices.get("mode") == "local" else ())
    if choices.get("mode") == "remote":
        fields += ("api_url",)
    for name in fields:
        if name not in choices:
            continue
        have, want = getattr(cfg, name), choices[name]
        if name == "api_url":
            have, want = str(have).rstrip("/"), str(want).rstrip("/")
        if have != want:
            return Check(
                False,
                f"{path} has {name} = {have!r}, setup chose {want!r}",
                "Run: sm setup --restart",
            )
    return Check(True, f"{path} holds the chosen settings")


def verify_spacy(choices: dict) -> Check:
    import spacy

    model = choices.get("spacy_model") or "en_core_web_sm"
    for name in dict.fromkeys((model, "en_core_web_sm")):
        if not spacy.util.is_package(name):
            return Check(
                False,
                f"spaCy model {name!r} is not installed",
                "Check your network connection, then run: sm setup",
            )
    return Check(True, f"spaCy model {model!r} installed")


def verify_embedding(choices: dict) -> Check:
    if choices.get("embedding_provider", "local") != "local":
        return Check(True, "no local embedding model needed")
    from smartmemory.tools.factory import _require_embedding_model

    with _env(SMARTMEMORY_EMBEDDING_PROVIDER="local"):
        try:
            _require_embedding_model(allow_download=False)
        except Exception as exc:
            return Check(
                False,
                f"local embedding model files are missing ({type(exc).__name__})",
                "Check your network connection, then run: sm setup",
            )
    return Check(True, "local embedding model files present")


LAZY_MODELS = (
    "sentence-transformers/all-MiniLM-L6-v2",
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
)


def verify_extra_models(choices: dict) -> Check:
    from smartmemory.utils import hf_models

    missing = []
    for model in LAZY_MODELS:
        try:
            hf_models.resolve_local_path(model, allow_download=False, backend="torch")
        except Exception:
            missing.append(model)
    if missing:
        return Check(
            False,
            "helper models not downloaded: " + ", ".join(missing),
            "Check your network connection, then run: sm setup",
        )
    return Check(True, "helper models present")


def verify_hooks(choices: dict) -> Check:
    from smartmemory_app import setup

    dest = setup.CLAUDE_DIR / "hooks"
    for src_name, dest_name in setup._HOOK_FILE_MAP.items():
        src = setup.HOOKS_SRC / src_name
        if not src.exists():
            continue
        target = dest / dest_name
        if not target.exists() or target.read_bytes() != src.read_bytes():
            return Check(
                False,
                f"hook {target} is missing or out of date",
                "Run: sm setup",
            )
    return Check(True, f"hooks installed in {dest}")


def verify_skills(choices: dict) -> Check:
    from smartmemory_app import setup

    dest = setup.CLAUDE_DIR / "skills"
    for src in setup.SKILLS_SRC.glob("*.md"):
        if not (dest / src.name).exists():
            return Check(False, f"skill {dest / src.name} is missing", "Run: sm setup")
    return Check(True, f"skills installed in {dest}")


def verify_register(choices: dict) -> Check:
    from smartmemory_app import setup

    path = setup.SETTINGS
    try:
        shell = setup._resolve_hook_shell()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        return Check(
            False,
            f"{path} could not be read ({type(exc).__name__})",
            "Fix or remove the file, then run: sm setup",
        )
    hooks = data.get("hooks") if isinstance(data, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    for event, entry in setup._get_hook_registrations(shell).items():
        command = entry["hooks"][0]["command"]
        registered = hooks.get(event, [])
        registered = registered if isinstance(registered, list) else [registered]
        if not any(
            isinstance(h, dict)
            and any(
                isinstance(hook, dict) and hook.get("command") == command
                for hook in h.get("hooks", [])
            )
            for h in registered
        ):
            return Check(
                False, f"{path} does not register the {event} hook", "Run: sm setup"
            )
    return Check(True, f"hooks registered in {path}")


def verify_seed(choices: dict) -> Check:
    from smartmemory_app.storage import _resolve_data_dir

    seed_dir = choices.get("seed_dir")
    folder = _resolve_data_dir(str(Path(seed_dir).expanduser()) if seed_dir else None)
    patterns = folder / "entity_patterns.jsonl"
    if not patterns.exists():
        return Check(
            False,
            f"{patterns} is missing",
            "Check that the data folder is writable, then run: sm setup",
        )
    return Check(True, f"patterns seeded in {folder}")


def verify_restart(choices: dict) -> Check:
    # A stop leaves nothing lasting to re-check: the daemon step right after it
    # starts SmartMemory again, and that step's check covers the result.
    return Check(True, "the running SmartMemory was stopped for the new settings")


def verify_daemon(choices: dict) -> Check:
    from smartmemory_app import doctor

    ctx = _doctor_context()
    row = doctor._check_daemon(ctx)
    warming = False
    if row.status == doctor.WARN:  # a daemon still warming up counts as started
        try:
            info = ctx.cached("daemon", ctx.daemon_status)
        except Exception:
            info = None
        warming = isinstance(info, dict) and info.get("status") == "warming"
    return _row_check(row, accept_warn=warming)


def verify_auth(choices: dict) -> Check:
    from smartmemory_app import doctor
    from smartmemory_app.config import load_config

    if load_config(apply_env=False).mode != "remote":
        return Check(
            False,
            "the config file is not in remote mode",
            "Run: sm login",
        )
    return _row_check(doctor.check_credentials(_doctor_context()))


# ── mode check (CLI-LOCAL-MODE-1) ───────────────────────────────────────────


def check_mode(choices: dict) -> Check:
    """The mode the CLI resolves (load_config, as every sm command) equals the chosen one."""
    from smartmemory_app import doctor
    from smartmemory_app.config import config_path, load_config

    chosen = choices.get("mode")
    ctx = _doctor_context()
    env_mode = os.environ.get("SMARTMEMORY_MODE")
    label = (
        (
            doctor._decided_env_label(ctx, "SMARTMEMORY_MODE")
            or doctor._env_source(ctx, "SMARTMEMORY_MODE")
        )
        if env_mode
        else ""
    )
    try:
        resolved = load_config().mode
    except ValueError:
        return Check(
            False,
            f"{label} has an invalid value, so every sm command stops with an error",
            "Unset SMARTMEMORY_MODE where it is exported, then run: sm setup",
        )
    if resolved == chosen:
        detail = f"the CLI runs in {resolved} mode, as chosen"
        if env_mode:
            detail += f" ({label} agrees)"
        return Check(True, detail)
    if env_mode:
        return Check(
            False,
            f"setup chose {chosen} mode, but the CLI runs in {resolved} mode because of {label}",
            "Unset SMARTMEMORY_MODE where it is exported, or run: sm env and choose "
            "ignore for it. Then run: sm setup",
        )
    return Check(
        False,
        f"setup chose {chosen} mode, but the CLI reads {resolved or 'no'} mode from {config_path()}",
        f"Run: sm config mode {chosen}",
    )


# ── save and recall round trip ──────────────────────────────────────────────


class _RoundTripError(Exception):
    def __init__(self, detail: str, fix: str = "") -> None:
        super().__init__(detail)
        self.detail, self.fix = detail, fix


def _run_command(
    command: click.Command, name: str, args: list[str], *, quiet: bool = False
) -> str:
    """Run one sm command in this process and return what it printed on stdout.

    The setup run already asked about inherited variables, so the command does not
    ask again. ``sm add`` keeps its scope header (it names where the memory goes);
    the read-back commands run quiet so the header is printed once.
    """
    out = io.StringIO()
    switches = {"SMARTMEMORY_ENV_CHECK": "0"}
    if quiet:
        switches["SMARTMEMORY_QUIET"] = "1"
    with _env(**switches), contextlib.redirect_stdout(out):
        try:
            command.main(args=args, prog_name=f"sm {name}", standalone_mode=False)
        except click.ClickException as exc:
            raise _RoundTripError(
                f"sm {name} failed: {exc.format_message()}", "Run: sm doctor"
            ) from exc
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise _RoundTripError(
                    f"sm {name} exited with code {exc.code}", "Run: sm doctor"
                ) from exc
        except Exception as exc:
            raise _RoundTripError(
                f"sm {name} failed ({type(exc).__name__})", "Run: sm doctor"
            ) from exc
    return out.getvalue()


def _json_tail(text: str):
    """The JSON document that ends a command's output (sm get and sm search --json)."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if not text[end:].strip():
            return value
    raise ValueError("no JSON in command output")


def _stamped(item: dict, name: str) -> Optional[str]:
    """``name`` from a memory item, flat (daemon, hosted) or under metadata (in-process)."""
    for container in (item, item.get("metadata"), item.get("properties")):
        if isinstance(container, dict) and container.get(name):
            return str(container[name])
    return None


@dataclass(frozen=True)
class Scope:
    mode: str
    tenant: str
    workspace: Optional[str]
    label: str  # the one line setup prints, env values shown as $NAME

    def describe(self) -> str:
        return self.label


def _local_scope(cfg, body: dict) -> Scope:
    from smartmemory_app import scope_header

    scope = scope_header.resolve_scope(
        cfg, path="/memory/ingest", kwargs={"json": dict(body)}, command="add"
    )
    shown = scope.workspace_id or "untagged"
    if scope.workspace_id and scope.workspace_id == os.environ.get(
        scope_header.ENV_WORKSPACE
    ):
        shown = f"${scope_header.ENV_WORKSPACE}"
    label = (
        f"mode local, tenant none (local store), workspace {shown} "
        f"({scope.workspace_source})"
    )
    return Scope("local", "none", scope.workspace_id, label)


def _remote_scope(cfg) -> Scope:
    from smartmemory_app.storage import _get_remote_memory

    mem = _get_remote_memory(cfg)
    workspace = getattr(mem, "_team_id", "") or None
    tenant = getattr(mem, "web_tenant_id", "") or "unknown"
    shown = workspace or "none"
    if workspace and workspace == os.environ.get("SMARTMEMORY_TEAM_ID"):
        shown, source = "$SMARTMEMORY_TEAM_ID", "SMARTMEMORY_TEAM_ID env var"
    elif cfg.team_id:
        source = "config team_id"
    else:
        source = "account default from /auth/me"
    label = f"mode remote, tenant {tenant}, workspace {shown} ({source})"
    return Scope("remote", tenant, workspace, label)


def _delete(item_id: str) -> None:
    """Remove the test memory by the CLI's route (no sm delete command exists)."""
    from smartmemory_app import cli

    with _env(SMARTMEMORY_QUIET="1", SMARTMEMORY_ENV_CHECK="0"):
        result = cli._memory_request("DELETE", f"/memory/{item_id}")
        if result is None:  # local daemon down: the in-process store, like sm get
            from smartmemory_app.storage import delete

            if not delete(item_id):
                raise LookupError(item_id)


def round_trip(choices: dict, *, sleep: Callable[[float], None] = time.sleep) -> Check:
    """Save a test memory with sm add, read it back with sm get and find it with sm search.

    Passes only when the item read back carries the workspace (and, when the
    service reports one, the tenant) that this check reports. The test memory is
    deleted afterwards; a failed delete is a WARNING naming the item id.
    """
    from smartmemory_app import cli
    from smartmemory_app.config import load_config

    cfg = load_config()
    token = uuid.uuid4().hex[:12]
    content = f"{ROUND_TRIP_PREFIX} {token}"
    body = {
        "content": content,
        "memory_type": "episodic",
        "context": {"origin": "cli:add"},
    }
    scope = _local_scope(cfg, body) if cfg.mode != "remote" else None
    item_id = None
    try:
        added = _run_command(cli.add_cmd, "add", [content]).strip().splitlines()
        item_id = added[-1].strip() if added else ""
        if not item_id or item_id.startswith("Held") or item_id == "?":
            raise _RoundTripError(
                f"sm add returned no memory id ({item_id or 'empty output'})",
                "Run: sm doctor",
            )
        if scope is None:
            scope = _remote_scope(cfg)
        item = _json_tail(_run_command(cli.get_cmd, "get", [item_id], quiet=True))
        if not isinstance(item, dict):
            raise _RoundTripError("sm get returned no memory", "Run: sm doctor")
        stamped = _stamped(item, "workspace_id")
        if cfg.mode == "remote":
            if stamped and scope.workspace and stamped != scope.workspace:
                raise _RoundTripError(
                    f"the test memory landed in workspace {stamped}, but the CLI sends {scope.workspace}",
                    "Run: sm doctor and compare the Tenant and workspace row",
                )
            tenant = _stamped(item, "tenant_id")
            if tenant and scope.tenant != "unknown" and tenant != scope.tenant:
                raise _RoundTripError(
                    f"the test memory landed in tenant {tenant}, but the API key belongs to tenant {scope.tenant}",
                    "Run: sm doctor and compare the Tenant and workspace row",
                )
        elif stamped != scope.workspace:
            raise _RoundTripError(
                f"the test memory landed in workspace {stamped or 'untagged'}, "
                f"but the CLI reports {scope.workspace or 'untagged'}",
                "Run: sm restart from this shell so the daemon and the CLI use the same settings",
            )
        found = False
        for attempt in range(SEARCH_ATTEMPTS):
            rows = _json_tail(
                _run_command(
                    cli.search_cmd,
                    "search",
                    [content, "--json", "--top-k", "20"],
                    quiet=True,
                )
            )
            rows = rows.get("items", []) if isinstance(rows, dict) else rows
            if any(
                isinstance(r, dict) and r.get("item_id") == item_id for r in rows or []
            ):
                found = True
                break
            if attempt + 1 < SEARCH_ATTEMPTS:
                sleep(SEARCH_PAUSE_SECONDS)
        if not found:
            raise _RoundTripError(
                "the test memory was saved but sm search did not find it",
                "Run: sm doctor. If every row passes, run: sm report",
            )
    except _RoundTripError as exc:
        where = f" Checked against: {scope.describe()}." if scope else ""
        return Check(False, f"{exc.detail}.{where}", exc.fix)
    except ValueError as exc:
        return Check(
            False,
            f"a command printed output setup could not read ({exc})",
            "Run: sm report",
        )
    finally:
        if item_id and not item_id.startswith("Held"):
            try:
                _delete(item_id)
            except Exception as exc:
                log.warning(
                    "Setup could not delete its test memory %s (%s)",
                    item_id,
                    type(exc).__name__,
                )
                click.echo(
                    f"Note: setup left its test memory {item_id} in the store.",
                    err=True,
                )
    return Check(True, f"saved, read back and found a test memory in {scope.label}")


# ── dispatch ────────────────────────────────────────────────────────────────

_VERIFIERS: dict[str, Callable[[dict], Check]] = {
    "config": verify_config,
    "spacy": verify_spacy,
    "embedding": verify_embedding,
    "extra_models": verify_extra_models,
    "hooks": verify_hooks,
    "skills": verify_skills,
    "register": verify_register,
    "seed": verify_seed,
    "restart": verify_restart,
    "daemon": verify_daemon,
    "auth": verify_auth,
    "mode": check_mode,
    "roundtrip": round_trip,
}


def _verify_step(key: str, choices: dict) -> Check:
    check = _VERIFIERS.get(key)
    if check is None:
        return Check(False, f"no check for setup step {key!r}", "Run: sm report")
    try:
        return check(choices)
    except Exception as exc:
        log.warning("Setup check %s could not run: %s", key, type(exc).__name__)
        return Check(
            False,
            f"the check itself failed ({type(exc).__name__})",
            "Run: sm setup again. If it repeats, run: sm report",
        )


# The seam setup_run calls. Unit suites that fake the install steps replace it
# (tests/conftest.py), the way doctor.run_checks is replaced.
verify_step = _verify_step
