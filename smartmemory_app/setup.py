"""DIST-LITE-5 + DIST-SETUP-TUI-1: Setup — first-run questionnaire + Claude Code hook wiring.

`smartmemory setup` is the single entry point for both:
  - First-run mode/pipeline config (local vs remote, LLM provider, etc.)
  - Claude Code hook installation (~/.claude/hooks/ + settings.json)

Local mode: asks pipeline questions, writes config, wires Claude Code hooks.
  All local deps (smartmemory-core, spaCy, usearch, filelock) are included
  in the base `pip install smartmemory` — no extras needed.
Remote mode: validates API key against /auth/me, stores in OS keychain,
  writes config. No hook wiring (remote mode has no hook-invoked pipeline).

DIST-SETUP-TUI-1: When running interactively with textual installed,
  launches a Textual TUI with arrow-key selection. Falls back to click
  prompts when non-interactive, flags provided, or textual unavailable.
"""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
import sys
from dataclasses import dataclass
from typing import Callable
from xml.sax.saxutils import escape

import click

from smartmemory_app import console as _console
from smartmemory_app.install_check import (
    PROXY_ENV_VARS,
    SOCKS_PROXY_ERROR,
    check_installation,
)
from smartmemory_app.daemon import (
    _LAUNCHD_DAEMON_LABEL,
    _LAUNCHD_WORKER_LABEL,
)

from smartmemory_app.support_diagnostics import download_failure, log_download_start
from smartmemory_app.diagnostics import redact_credentials
from smartmemory_app.doctor import after_setup as _doctor_after_setup

logger = logging.getLogger(__name__)

_SETUP_INSTALLATION_ERROR = """Setup stopped because your SmartMemory version is old or incomplete.

Run these commands one at a time. The first command stops the old SmartMemory background process so it cannot restart during the upgrade.

smartmemory uninstall
pip install -U smartmemory
sm doctor
smartmemory setup

After `sm doctor`, look for `All checks passed.` That means the upgrade worked."""


# ── SetupResult — contract between TUI and business logic ─────────────────


@dataclass
class SetupResult:
    """Answers collected from the TUI (or click prompts). Matches SmartMemoryConfig fields."""

    mode: str = "local"
    llm_provider: str = "groq"
    llm_model: str = ""
    embedding_provider: str = "local"
    spacy_model: str = "en_core_web_sm"
    coreference: bool = False
    data_dir: str = "~/.smartmemory"
    daemon_warming: bool = False  # transient TUI outcome; not a config setting


HOOKS_SRC = Path(__file__).parent / "hooks"
SKILLS_SRC = Path(__file__).parent / "skills"
PLIST_TEMPLATE = Path(__file__).parent / "data" / "ai.smartmemory.daemon.plist"
WORKER_PLIST_TEMPLATE = Path(__file__).parent / "data" / "ai.smartmemory.worker.plist"
CLAUDE_DIR = Path.home() / ".claude"
HOOKS_DEST = CLAUDE_DIR / "hooks"
SETTINGS = CLAUDE_DIR / "settings.json"
DATA_DIR = Path.home() / ".smartmemory"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_NAME = f"{_LAUNCHD_DAEMON_LABEL}.plist"
WORKER_PLIST_NAME = f"{_LAUNCHD_WORKER_LABEL}.plist"

# Maps source filename (in package) → namespaced dest filename (in ~/.claude/hooks/)
_HOOK_FILE_MAP = {
    # DIST-AGENT-HOOKS-1: 6-phase lifecycle hooks
    "orient.sh": "smartmemory-orient.sh",
    "recall.sh": "smartmemory-recall.sh",
    "observe.sh": "smartmemory-observe.sh",
    "distill.sh": "smartmemory-distill.sh",
    "learn.sh": "smartmemory-learn.sh",
    "persist.sh": "smartmemory-persist.sh",
}
HOOK_NAMES = list(_HOOK_FILE_MAP.values())
SKILL_NAMES = ["remember.md", "search.md", "ingest.md", "orient.md"]

# Legacy entries to clean up during install (pre-namespacing)
_LEGACY_HOOK_REGISTRATIONS = {
    # Pre-DIST-AGENT-HOOKS-1 registrations to clean up
    "SessionStart": {"command": "bash", "args": [str(HOOKS_DEST / "session-start.sh")]},
    "Stop": {"command": "bash", "args": [str(HOOKS_DEST / "session-end.sh")]},
    "PostToolUseFailure": {
        "command": "bash",
        "args": [str(HOOKS_DEST / "post-tool-failure.sh")],
    },
    # Also clean old namespaced variants
    "_legacy_SessionStart": {
        "command": "bash",
        "args": [str(HOOKS_DEST / "smartmemory-session-start.sh")],
    },
    "_legacy_Stop": {
        "command": "bash",
        "args": [str(HOOKS_DEST / "smartmemory-session-end.sh")],
    },
    "_legacy_PostToolUseFailure": {
        "command": "bash",
        "args": [str(HOOKS_DEST / "smartmemory-post-tool-failure.sh")],
    },
}


def _resolve_hook_shell() -> str:
    """Validate Git Bash on Windows. Never select Windows' WSL bash launcher."""
    if sys.platform != "win32":
        return "bash"
    git = shutil.which("git")
    candidates = []
    if git:
        git_path = Path(git)
        candidates.extend(
            (git_path.parent.parent / "bin/bash.exe", git_path.parent / "bash.exe")
        )
        # Portable Git can place git.exe deeper than cmd/ or bin/.
        try:
            result = subprocess.run(
                [git, "--exec-path"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
                creationflags=0x08000000,
            )
            exec_path = Path(result.stdout.strip())
            candidates.append(exec_path.parent.parent.parent / "bin/bash.exe")
        except (OSError, subprocess.SubprocessError):
            pass
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            subprocess.run(
                [str(candidate), "--noprofile", "--norc", "-c", "exit 0"],
                capture_output=True,
                check=True,
                timeout=5,
                creationflags=0x08000000,
            )
            return str(candidate).replace("\\", "/")
        except (OSError, subprocess.SubprocessError):
            continue
    raise click.ClickException(
        "Cannot install Windows lifecycle hooks: Git Bash is unavailable or failed validation. "
        "Install Git for Windows, then rerun smartmemory setup."
    )


def _hook_command(filename: str, shell: str | None = None) -> str:
    """Quote the installed script and select a verified Windows shell."""
    path = str(HOOKS_DEST / filename)
    shell = shell or _resolve_hook_shell()
    if sys.platform == "win32":
        path = path.replace("\\", "/")
        path = path.replace("$", "\\$").replace("`", "\\`").replace('"', '\\"')
        shell = shell.replace("\\", "/")
        shell = shell.replace("$", "\\$").replace("`", "\\`").replace('"', '\\"')
        executable = "bash" if shell == "bash" else '"' + shell + '"'
        return f'{executable} "{path}"'
    return f"bash {path}"


def _installed_hook_name(command: str) -> str | None:
    """Recognize only bash commands targeting our known installed hook files."""
    if not isinstance(command, str):
        return None
    match = re.fullmatch(
        r'\s*(?:bash|"[^"\n]+[/\\]bash\.exe")\s+(.+?)\s*', command, re.IGNORECASE
    )
    if not match:
        return None
    path = match[1]
    if path.startswith("'") and path.endswith("'"):
        try:
            (path,) = shlex.split(path)
        except ValueError:
            return None
    elif path.startswith('"') and path.endswith('"'):
        path = path[1:-1].replace("\\$", "$").replace("\\`", "`").replace('\\"', '"')
    path = path.replace("\\", "/")
    names = HOOK_NAMES + [
        "session-start.sh",
        "session-end.sh",
        "post-tool-failure.sh",
        "smartmemory-session-start.sh",
        "smartmemory-session-end.sh",
        "smartmemory-post-tool-failure.sh",
    ]
    for name in names:
        if path == str(HOOKS_DEST / name).replace("\\", "/"):
            return name
    return None


# HOOK-DEADLINE: Claude Code kill line (seconds) for the two hooks that block
# the user. The CLI's own deadline (SMARTMEMORY_HOOK_DEADLINE, default 8 s)
# fires first and exits cleanly; this only bounds interpreter start-up on top.
# Written only when absent, so a user-chosen timeout always survives setup.
HOOK_TIMEOUTS = {
    "SessionStart": ("smartmemory-orient.sh", 15),
    "UserPromptSubmit": ("smartmemory-recall.sh", 15),
}


def _get_hook_registrations(shell: str | None = None) -> dict:
    """Build hook registration entries using current Claude Code hooks format.

    Each event maps to a registration dict with matcher + hooks array.
    Paths point to the COPIED destination (~/.claude/hooks/), not the package
    source, so registrations are stable across package upgrades.
    """
    shell = shell or _resolve_hook_shell()
    return {
        "SessionStart": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-orient.sh", shell),
                    "timeout": HOOK_TIMEOUTS["SessionStart"][1],
                }
            ],
        },
        "UserPromptSubmit": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-recall.sh", shell),
                    "timeout": HOOK_TIMEOUTS["UserPromptSubmit"][1],
                }
            ],
        },
        "PostToolUse": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-observe.sh", shell),
                }
            ],
        },
        "Stop": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-distill.sh", shell),
                }
            ],
        },
        "PostToolUseFailure": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-learn.sh", shell),
                }
            ],
        },
        "SessionEnd": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command("smartmemory-persist.sh", shell),
                }
            ],
        },
    }


# Module-level alias for external callers that need the structure at import time.
# NOTE: frozen at import — prefer _get_hook_registrations() inside functions.
HOOK_REGISTRATIONS = _get_hook_registrations(shell="bash")


# ── Setup command ─────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["local", "remote"]),
    default=None,
    help="Skip questionnaire: set mode directly.",
)
@click.option(
    "--api-key",
    "api_key",
    default=None,
    help="Remote API key (stored in OS keychain). Required for --mode remote.",
)
@click.option(
    "--for",
    "for_tool",
    default=None,
    help="Tool config (e.g. 'cursor'). Works in both modes.",
)
@click.option(
    "--api-url", help="Remote service base URL (overrides SMARTMEMORY_API_URL)."
)
@click.option(
    "--restart",
    is_flag=True,
    default=False,
    help="Start over: forget the saved progress of an unfinished run.",
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Accept the default for every question (local mode unless --mode says otherwise).",
)
@_doctor_after_setup  # SETUP-DOCTOR-1: show what the CLI will use, stop on a FAIL
def setup(
    mode: str | None,
    api_key: str | None,
    for_tool: str | None,
    api_url: str | None = None,
    restart: bool = False,
    yes: bool = False,
) -> None:
    """First-run questionnaire: configure local or remote mode.

    Three self-contained branches: each handles config, post-config, AND daemon
    start. No shared post-branch code to avoid double-start bugs.
    """
    # SETUP-RESUMABLE-1: every step is checked after it runs and its result saved
    # (setup_run.py). A re-run after an unfinished run resumes at the first step
    # whose effect is gone. --restart starts over.
    installation = check_installation()
    if not installation.ok:
        raise click.ClickException(_SETUP_INSTALLATION_ERROR)
    if not installation.socks_support_ok:
        click.echo(f"Warning: {SOCKS_PROXY_ERROR}", err=True)

    from smartmemory_app.config import config_path

    # DIST-UPDATE-HINT-1: whether this is a first install, read before any branch
    # writes a config. The TUI branch owns its own completion screen, so the
    # first-run line is echoed here for it; the click branch echoes its own.
    first_run = not config_path().exists()
    if first_run:
        from smartmemory_app.crash_reporter import enabled

        if enabled():
            click.echo(
                "Automatic anonymous crash reports are on. Disable: SMARTMEMORY_CRASH_REPORTS=0"
            )
    from smartmemory_app import setup_state
    from smartmemory_app.setup_run import SetupRun

    if restart:
        setup_state.clear()
    else:
        saved = SetupRun.resumable(mode=mode)
        if saved is not None:
            _resume_setup(saved, api_key=api_key, yes=yes)
            if for_tool:
                _setup_tool_config(for_tool)
            return

    if yes and mode is None:
        mode = "local"
    if (
        yes
        and mode == "remote"
        and not (api_key or os.environ.get("SMARTMEMORY_API_KEY"))
    ):
        raise click.ClickException(
            "Remote setup with --yes needs an API key.\n"
            "Fix: run sm setup --mode remote --yes --api-key KEY"
        )

    # Branch 1: Flags provided — use click flow directly
    # _setup_click() already handles daemon start for local mode internally
    if mode is not None:
        _setup_click(mode, api_key, api_url, **({"yes": True} if yes else {}))
        if for_tool:
            _setup_tool_config(for_tool)
        return

    # Branch 2: Interactive TUI (if available)
    if _can_run_tui():
        try:
            from smartmemory_app.setup_tui import run_setup_tui

            result = run_setup_tui()
            if result is None:
                click.echo("Setup cancelled.")
                return
            if result.mode == "remote":
                # TUI only selected mode — hand off to click-based remote setup
                _setup_remote(api_key, api_url)
                click.echo("\nSetup complete.")
                try:
                    from smartmemory_app.launch_metrics import emit as _lm_emit

                    _lm_emit("setup.complete", {"mode": "remote"})
                except Exception:
                    pass
            else:
                # TUI ProgressScreen already ran config + hooks + daemon
                props = {"mode": "local"}
                if result.daemon_warming:
                    props["warming"] = True
                try:
                    from smartmemory_app.launch_metrics import emit as _lm_emit

                    _lm_emit("setup.complete", props)
                except Exception:
                    pass
                if result.daemon_warming:
                    click.echo(_SETUP_WARMING_WARNING)
                _verify_after_tui()
                if not result.daemon_warming and first_run:
                    click.echo("All done. Run 'sm tour' if this is your first time.")
            if for_tool:
                _setup_tool_config(for_tool)
            return
        except click.ClickException:
            raise
        except Exception as e:
            logger.warning("TUI setup failed, using text prompts", exc_info=True)
            click.echo(
                redact_credentials(f"TUI unavailable ({e}), using text prompts.\n")
            )

    # Branch 3: Click fallback
    _setup_click(None, api_key, api_url)
    if for_tool:
        _setup_tool_config(for_tool)


def _setup_click(
    mode: str | None,
    api_key: str | None,
    api_url: str | None = None,
    *,
    yes: bool = False,
) -> None:
    """Click-based setup flow (non-TUI). Self-contained: config + post-config."""
    if mode is None:
        click.echo("Welcome to SmartMemory.\n")
        click.echo("Where do you want to store memories?")
        click.echo("  1. Local:  on this machine, no account needed")
        click.echo("  2. Remote: SmartMemory hosted service (api.smartmemory.ai)")
        choice = click.prompt(">", type=click.Choice(["1", "2"]), default="1")
        mode = "local" if choice == "1" else "remote"

    if mode == "remote":
        _setup_remote(api_key, api_url)
        click.echo("\nSetup complete.")
        try:
            from smartmemory_app.launch_metrics import emit as _lm_emit

            _lm_emit("setup.complete", {"mode": "remote"})
        except Exception:
            pass
    else:
        from smartmemory_app.setup_steps import StepTracker

        tracker = StepTracker()
        first_run = _setup_local(tracker, **({"yes": True} if yes else {}))
        daemon_status = _finish_local_run(tracker)
        if daemon_status and daemon_status.get("status") == "ok":
            if first_run:
                click.echo("All done. Run 'sm tour' if this is your first time.")
            else:
                click.echo("Done. SmartMemory is ready.")
        from smartmemory_app.config import (
            NO_LOCAL_MODEL_OFF_LINE,
            load_config,
            local_llm_without_model,
        )

        if local_llm_without_model(load_config()):
            click.echo(NO_LOCAL_MODEL_OFF_LINE)
        props = {"mode": "local"}
        if daemon_status.get("status") == "warming":
            props["warming"] = True
        try:
            from smartmemory_app.launch_metrics import emit as _lm_emit

            _lm_emit("setup.complete", props)
        except Exception:
            pass


def _finish_local_run(tracker) -> dict:
    """Start SmartMemory and run the checks: the steps after _setup_local, same counter."""
    from smartmemory_app import setup_state
    from smartmemory_app.setup_run import SetupRun

    state = setup_state.load()
    if state is None or state.choices.get("mode") != "local" or state.complete:
        # _setup_local could not save its progress: start SmartMemory as before,
        # but say that the checks were skipped.
        logger.warning(
            "Setup progress was not saved, so the mode and save-and-recall checks "
            "did not run. Run: sm doctor"
        )
        with tracker.step("SmartMemory started", live=False):
            return _start_daemon_local() or {}
    run = SetupRun(state, tracker=tracker, check_restart=False)
    run.run(run.index_of("daemon"))
    return run.daemon_status


def _resume_setup(state, *, api_key: str | None = None, yes: bool = False) -> None:
    """Continue an unfinished run with its saved choices (no questions asked again)."""
    from smartmemory_app.setup_run import SetupRun

    if (
        yes
        and state.choices.get("mode") == "remote"
        and not (api_key or os.environ.get("SMARTMEMORY_API_KEY"))
    ):
        from smartmemory_app.config import load_config

        if not load_config(apply_env=False).api_key_set:
            raise click.ClickException(
                "Remote setup with --yes needs an API key.\n"
                "Fix: run sm setup --mode remote --yes --api-key KEY"
            )
    run = SetupRun(state, api_key=api_key)
    run.resume()
    mode = state.choices.get("mode", "local")
    if mode == "local":
        if run.daemon_status.get("status") == "warming":
            click.echo(_SETUP_WARMING_WARNING)
        else:
            click.echo("Done. SmartMemory is ready.")
    else:
        click.echo("\nSetup complete.")
    try:
        from smartmemory_app.launch_metrics import emit as _lm_emit

        _lm_emit("setup.complete", {"mode": mode, "resumed": True})
    except Exception:
        pass


def _verify_after_tui() -> None:
    """Run the checks after the setup screen closes (it ran the install steps)."""
    from smartmemory_app import setup_state
    from smartmemory_app.setup_run import SetupRun

    state = setup_state.load()
    if state is None or state.choices.get("mode") != "local":
        logger.warning(
            "Setup progress was not saved, so the mode and save-and-recall checks "
            "did not run. Run: sm doctor"
        )
        return
    run = SetupRun(state, check_restart=False)
    start = run.first_unrecorded()
    if start < run.index_of("mode"):
        total = len(run.plan)
        raise click.ClickException(
            f"Setup did not finish: step {start + 1} of {total} "
            f"({run.plan[start].title}) did not complete.\n"
            "Fix: run sm setup again. It resumes at that step."
        )
    click.echo("")
    run.run(start)


def _can_run_tui() -> bool:
    """Check if we can launch a Textual TUI."""
    if sys.platform == "win32" and os.environ.get("SMARTMEMORY_FORCE_TUI") != "1":
        logger.debug("Skipping setup TUI on Windows, using text prompts")
        return False
    if not _console.interactive_terminal():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("CI"):
        return False
    try:
        import textual  # noqa: F401

        return True
    except ImportError:
        return False


def _start_daemon_local(
    on_log: Callable[[str], None] | None = None,
) -> dict:
    """Start daemon automatically in local mode.

    On macOS: let launchd own the process (RunAtLoad starts it immediately).
    On other platforms: start manually via start_daemon().
    Never do both — that causes a duplicate-bind crash loop.
    """
    from smartmemory_app.daemon import _retire_legacy_workers, _upgrade_worker_agent

    try:
        _upgrade_worker_agent()
        _retire_legacy_workers()
        launchd_ok = _install_launchd_plist()
        if sys.platform == "win32":
            message = (
                "Warning: Windows login auto-start and automatic crash recovery are unavailable. "
                "Run sm start after login or a crash."
            )
            logger.warning(message)
            (on_log or click.echo)(message)
        if on_log is None:
            action = (
                "Waiting for SmartMemory to start..."
                if launchd_ok
                else "Starting SmartMemory..."
            )
            click.echo(f"\n{action}")
        from smartmemory_app.daemon import start_daemon

        if on_log is not None:
            status = start_daemon(on_log=on_log, allow_warming_on_timeout=True)
        else:
            from smartmemory_app.progress import startup_progress

            with startup_progress("Starting SmartMemory", emit=click.echo) as emit:
                status = start_daemon(on_log=emit, allow_warming_on_timeout=True)
    except Exception as exc:
        raise _daemon_setup_error(str(exc) or exc.__class__.__name__) from exc

    if status is None:
        raise _daemon_setup_error("No health response was received after startup.")
    if status.get("status") not in {"ok", "warming"}:
        reason = status.get("degraded_reason") or (
            f"Daemon health status: {status.get('status', 'unknown')}."
        )
        raise _daemon_setup_error(reason, status)

    if status.get("status") == "warming":
        (on_log or click.echo)(_SETUP_WARMING_WARNING)
    elif on_log is None:
        from smartmemory_app.daemon import _port

        click.echo(
            f"SmartMemory is running (daemon running at http://127.0.0.1:{_port()})."
        )
    if status.get("reextract_offer"):
        (on_log or click.echo)(status["reextract_offer"])
    return status


_SETUP_WARMING_WARNING = (
    "Warning: SmartMemory daemon is still warming up. Setup is configured, "
    "but SmartMemory is not ready yet.\n"
    "Check: sm status. Additional worker startup is deferred, once warmup finishes, run sm start --wait."
)


def _daemon_setup_error(
    reason: str, status: dict | None = None
) -> click.ClickException:
    """Name the failed setup step, its one-line fix, and the daemon's log location."""
    from smartmemory_app.daemon import _data_dir

    problem, fix = reason, None
    try:
        from smartmemory_app.daemon_diagnosis import collect_facts, diagnose

        found = diagnose(collect_facts(status))
        # A healthy daemon contradicts the failure, so keep the raw reason alone.
        if not found.ok:
            problem, fix = found.detail, found.fix
    except Exception:
        logger.warning("Daemon failure diagnosis unavailable", exc_info=True)
    lines = ["Setup failed: SmartMemory daemon did not start or become ready."]
    lines.append(f"Problem: {problem}")
    if fix:
        lines.append(f"Fix: {fix}")
    if problem != reason:
        lines.append(f"Detail: {reason}")
    lines.append(f"Log: {_data_dir() / 'daemon.log'}")
    lines.append("Next step: run sm doctor, then sm start --wait.")
    return click.ClickException("\n".join(lines))


def _apply_setup_result(
    result: SetupResult,
    on_step: Callable[[str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> None:
    """Apply a SetupResult from TUI. Saves config + runs post-config steps.

    Args:
        result: Collected answers from the TUI.
        on_step: Optional callback called with step name after each step completes.
                 Used by ProgressScreen for per-step UI updates.
        on_log: Optional sink for newline-only model download progress lines.
    """
    from smartmemory_app.setup_run import SetupRun

    data_dir = str(Path(result.data_dir).expanduser())
    choices = {
        "mode": result.mode,
        "coreference": result.coreference,
        "llm_provider": result.llm_provider,
        "llm_model": result.llm_model,
        "embedding_provider": result.embedding_provider,
        "spacy_model": result.spacy_model,
        "data_dir": data_dir,
        "seed_dir": data_dir,
    }
    # SETUP-RESUMABLE-1: the same steps and checks as the text flow, saved after
    # each one. The screen draws progress (quiet), and starts SmartMemory itself.
    run = SetupRun.fresh(
        choices, on_step=on_step, on_log=on_log, quiet=True, check_restart=False
    )
    run.run(stop_before="daemon")


# ── Mode setup helpers ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProfileLookup:
    """One shell-profile lookup. ``error`` is set when the profile exists but could
    not be read, so a caller can tell "unreadable" from "not exported there"."""

    profile: str  # display form, e.g. ~/.zshrc
    value: str = ""
    error: str = ""  # exception class name when unreadable


def _profile_lookup(name: str) -> ProfileLookup:
    """Read ``export NAME=...`` from the user's shell profile (SETUP-DOCTOR-1)."""
    shell = os.environ.get("SHELL", "/bin/zsh")
    if "zsh" in shell:
        rel = ".zshrc"
    elif "bash" in shell:
        rel = ".bashrc"
    else:
        rel = ".profile"
    profile = Path.home() / rel
    shown = f"~/{rel}"
    try:
        text = profile.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProfileLookup(shown)
    except Exception as exc:
        return ProfileLookup(shown, error=type(exc).__name__)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"export {name}="):
            # Extract value — handles export KEY="value" and export KEY=value
            val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return ProfileLookup(shown, value=val)
    return ProfileLookup(shown)


def _read_env_from_profile(name: str) -> str:
    """Try to read an export value from the user's shell profile ("" when absent or unreadable)."""
    return _profile_lookup(name).value


def _masked_key_hint(key: str) -> str:
    """What a prompt may show of a stored key: its last four characters, never a prefix."""
    return "..." + key[-4:] if len(key) > 12 else "***"


def _persist_env_var(name: str, value: str) -> None:
    """Persist through Windows credentials or a UTF-8 shell profile.

    Also sets os.environ so this process and its children pick up the key.
    """
    os.environ[name] = value
    if sys.platform == "win32":
        try:
            import keyring

            keyring.set_password("smartmemory", name, value)
            click.echo("  Stored in Windows credential store for the daemon.")
        except Exception as exc:
            logger.warning(
                "Credential persistence failed, key is only available in this setup process: %s",
                redact_credentials(str(exc), (value,)),
            )
            click.echo(
                "  Warning: credential storage failed. The key is only set for this setup session."
            )
        click.echo(
            f'  For PowerShell CLI sessions, set $env:{name} = "<your API key>".\n'
            f'  To persist it for future logins: [Environment]::SetEnvironmentVariable("{name}", "<your API key>", "User")'
        )
        return
    shell = os.environ.get("SHELL", "/bin/zsh")
    if "zsh" in shell:
        profile = Path.home() / ".zshrc"
    elif "bash" in shell:
        profile = Path.home() / ".bashrc"
    else:
        profile = Path.home() / ".profile"

    export_line = f'export {name}="{value}"'

    try:
        existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
        lines = existing.splitlines()

        # Replace existing line or append
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"export {name}="):
                lines[i] = export_line
                replaced = True
                break

        if replaced:
            profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with open(profile, "a", encoding="utf-8") as f:
                f.write(f"\n{export_line}\n")

        click.echo(f"  Written to {profile}")
    except Exception as e:
        click.echo(
            f"  Warning: could not write to {profile}: "
            f"{redact_credentials(str(e), (value,))}"
        )
        # Never print the key: it lands in terminal scrollback and CI logs.
        click.echo(f'  Add this line to {profile} yourself: export {name}="<your key>"')

    # Set in current process so daemon picks it up
    os.environ[name] = value


# Shown wherever the local model choice is made (click prompt and TUI model screen).
NO_LOCAL_MODEL_NOTE = (
    "No local model chosen: SmartMemory will not use your local model for extraction "
    "until you add one. Add it later with: sm config llm_model <model name>"
)


def _prompt_local_model(provider: str) -> str:
    """Ask which local model to use. The default is none, stored as an empty name."""
    click.echo(f"\nWhich {provider} model should SmartMemory use? Default: none.")
    click.echo(f"  {NO_LOCAL_MODEL_NOTE}")
    choice = click.prompt("Local model name", default="none").strip()
    return "" if choice.lower() == "none" else choice


# SETUP-RESUMABLE-1: what ``sm setup --yes`` answers to every local question.
LOCAL_DEFAULTS = {
    "mode": "local",
    "coreference": False,
    "llm_provider": "groq",
    "llm_model": "",
    "embedding_provider": "local",
    "spacy_model": "en_core_web_sm",
    "data_dir": "~/.smartmemory",
}


def _setup_local(tracker=None, *, yes: bool = False) -> bool:
    """Ask pipeline questions, write config, wire Claude Code hooks.

    ``tracker`` is the run's ``StepTracker`` (the daemon step and the checks after it
    run in ``_finish_local_run``). ``yes`` takes every default with no prompt.

    All local deps (smartmemory-core, spaCy, usearch, filelock) are already
    installed as part of `pip install smartmemory` — no extra install step needed.
    """
    from smartmemory_app.config import config_path
    from smartmemory_app.daemon import is_running
    from smartmemory_app.setup_run import SetupRun
    from smartmemory_app.setup_steps import StepTracker

    # DIST-UPDATE-HINT-1: read before save_config writes one.
    was_first_run = not config_path().exists()

    choices = dict(LOCAL_DEFAULTS) if yes else _ask_local_choices()
    choices["restart"] = is_running(require_healthy=False)
    run = SetupRun.fresh(choices, tracker=tracker or StepTracker())
    click.echo("")
    # Stops before starting SmartMemory. A live daemon is stopped first so the
    # lifecycle owner restarts it with the new config; the runner asks again after
    # the downloads, since a daemon can start or stop while they run.
    run.run(stop_before="daemon")
    return was_first_run


def _ask_local_choices() -> dict:
    """The local-mode questions. Returns the choices (an LLM key goes to its store)."""
    from smartmemory_app.config import LOCAL_MODEL_PROVIDERS

    coref = click.confirm(
        "\nEnable coreference resolution? "
        "(downloads ~500MB fastcoref model on first run)",
        default=False,
    )
    click.echo("\nSmartMemory needs an LLM to extract entities and relationships.")
    click.echo("  groq         free tier, fast inference (recommended)")
    click.echo("  claude-agent Claude Agent SDK, OAuth (no API key)")
    click.echo("  anthropic    Anthropic API (ANTHROPIC_API_KEY)")
    click.echo("  openai       OpenAI API (OPENAI_API_KEY)")
    click.echo("  gemini       Google Gemini API (GEMINI_API_KEY)")
    click.echo("  ollama       free, local (llama3.1, mistral)")
    click.echo("  lmstudio     local, OpenAI-compatible endpoint")
    click.echo("  none         EntityRuler only (very limited)")
    llm = click.prompt(
        "LLM provider",
        default="groq",
    )

    # Prompt for API key if the provider needs one
    _LLM_KEY_ENVVAR = {
        "groq": "GROQ_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    llm_model = ""
    if llm in LOCAL_MODEL_PROVIDERS:
        llm_model = _prompt_local_model(llm)
    key_envvar = _LLM_KEY_ENVVAR.get(llm)
    if key_envvar:
        # Check env, then keychain, then shell profile
        existing = os.environ.get(key_envvar, "").strip()
        if not existing:
            try:
                import keyring

                existing = (
                    keyring.get_password("smartmemory", key_envvar) or ""
                ).strip()
                if existing:
                    os.environ[key_envvar] = existing  # load into current process
            except Exception:
                pass
        if not existing:
            # Try reading from shell profile as last resort
            existing = _read_env_from_profile(key_envvar)
            if existing:
                os.environ[key_envvar] = existing
        if existing:
            api_key = click.prompt(
                f"\n{key_envvar} ({_masked_key_hint(existing)}). Press Enter to keep, or paste new key",
                default="",
                hide_input=True,
            )
        else:
            api_key = click.prompt(
                f"\n{key_envvar} not set. Enter your API key (Enter to skip)",
                default="",
                hide_input=True,
            )
        if api_key.strip():
            _persist_env_var(key_envvar, api_key.strip())
            # Windows persistence and shell guidance are handled above.
            if sys.platform != "win32":
                try:
                    import keyring

                    keyring.set_password("smartmemory", key_envvar, api_key.strip())
                    click.echo("  Also stored in OS keychain.")
                except Exception as exc:
                    logger.warning(
                        "OS credential store unavailable, using shell profile: %s",
                        redact_credentials(str(exc), (api_key.strip(),)),
                    )

    embedding = click.prompt(
        "Embedding provider? (local / openai / ollama)",
        default="local",
    )

    click.echo("\nspaCy model (used for entity extraction):")
    click.echo("  sm  15MB, fast, good for most use cases")
    click.echo("  md  45MB, better NER accuracy")
    click.echo("  lg  590MB, best accuracy, slower to download")
    spacy_size = click.prompt(
        "Which size?",
        type=click.Choice(["sm", "md", "lg"], case_sensitive=False),
        default="sm",
    )
    spacy_model = f"en_core_web_{spacy_size}"

    data_dir = click.prompt("Default data directory", default="~/.smartmemory")
    return {
        "mode": "local",
        "coreference": coref,
        "llm_provider": llm,
        "llm_model": llm_model,
        "embedding_provider": embedding,
        "spacy_model": spacy_model,
        "data_dir": data_dir,
    }


def _remote_api_url(api_url: str | None) -> str:
    """The remote base URL, refused when it carries credentials, a query or a fragment."""
    from urllib.parse import urlsplit

    from smartmemory_app.config import load_config

    api_url = (api_url or load_config().api_url).rstrip("/")
    parsed = urlsplit(api_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise click.ClickException(
            "API URL must be an HTTP(S) base URL without credentials, query or fragment."
        )
    return api_url


def _setup_remote(api_key: str | None, api_url: str | None = None) -> None:
    """Validate API key, store in OS keychain, write remote config, then check it.

    SETUP-RESUMABLE-1: the steps are ``setup_steps.remote_setup_plan`` (login, hooks,
    the mode check and a save-and-recall round trip), run and saved by setup_run.
    The key itself is never saved in the progress file.
    """
    from smartmemory_app.setup_run import SetupRun

    api_url = _remote_api_url(api_url)
    choices = {"mode": "remote", "api_url": api_url}
    run = SetupRun.fresh(choices, api_key=api_key)
    run.run()
    click.echo("Lifecycle hooks installed. Auto-recall and observation active.")


def _remote_login(api_key: str | None, api_url: str | None = None) -> str:
    """Check the API key against /auth/me, keep it in the OS keychain, write remote config.

    Returns the account's default team id.
    """
    import httpx
    from smartmemory_app.config import SmartMemoryConfig, save_config, set_api_key

    api_url = _remote_api_url(api_url)
    api_key = api_key or os.environ.get("SMARTMEMORY_API_KEY")
    if not api_key:
        api_key = click.prompt(
            "SmartMemory API key (stored in OS keychain, not config file)",
            hide_input=True,
        )

    click.echo("Validating API key...")
    try:
        r = httpx.get(
            f"{api_url}/auth/me",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        user = r.json()
        team_id = user.get("default_team_id") or ""
        click.echo(f"Authenticated as {user.get('email')}. Team: {team_id}")
    except Exception as e:
        raise click.ClickException(
            redact_credentials(f"Remote authentication failed: {e}", (api_key,))
        ) from e

    set_api_key(api_key)  # persist to OS keychain (warns if unavailable, never raises)
    cfg = SmartMemoryConfig(
        mode="remote", api_url=api_url, api_key_set=True, team_id=team_id
    )
    save_config(cfg)
    return team_id


def _setup_tool_config(tool: str) -> None:
    """Emit tool-specific MCP config snippet."""
    click.echo(f"\nTool config for {tool}:")
    click.echo(
        '  Add to your MCP config: {"command": "smartmemory", "args": ["server"]}'
    )


# ── Uninstall command ─────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--keep-data",
    is_flag=True,
    default=False,
    help="Keep ~/.smartmemory/ data directory (memories, patterns). Config is still removed.",
)
def uninstall(keep_data: bool) -> None:
    """Remove SmartMemory hooks, skills, launchd plist, config, and optionally data."""
    # Stop daemon before removing anything
    try:
        from smartmemory_app.daemon import stop_daemon

        stop_daemon()
    except Exception:
        pass
    _uninstall_launchd_plist()
    _deregister_hooks()
    _remove_hooks()
    _remove_skills()
    _remove_config_file()
    if not keep_data:
        _remove_data_dir()
    click.echo("SmartMemory removed. Restart Claude Code to deactivate hooks.")


# ── Install helpers ───────────────────────────────────────────────────────────


def _ensure_embedding_model(provider: str, *, missing: bool = False) -> None:
    """Fetch the configured local embedder's files and any transitive remote code.

    Args:
        provider: Configured embedding provider; only ``local`` has files to fetch.
        missing: The caller already knows the files are absent (``sm add`` first
            run), so the notice says "Downloading" instead of "Preparing".
    """
    if provider != "local":
        return
    from smartmemory.errors import MissingModelError
    from smartmemory.tools.factory import _require_embedding_model
    from smartmemory.plugins.embedding import DEFAULT_LOCAL_MODEL, EmbeddingService
    from smartmemory.utils import hf_models

    model = "configured embedding model"
    previous = os.environ.get("SMARTMEMORY_EMBEDDING_PROVIDER")
    previous_download = os.environ.get(hf_models.ALLOW_DOWNLOAD_ENV)
    os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = provider
    try:
        model = EmbeddingService().local_model_name()
        log_download_start(logger, model, EmbeddingService().backend_name)
        note = _download_note(model)
        if missing:
            click.echo(f"Downloading local embedding model {model!r} ({note})...")
        else:
            click.echo(
                f"Preparing local embedding model {model!r} "
                f"(downloads missing files, {note})..."
            )
        _require_embedding_model(allow_download=True)
        service = EmbeddingService()
        # Pinned torch models use trust_remote_code at inference. A snapshot alone
        # omits external modeling repos; reuse the core loader to cache those now.
        # MiniLM needs no remote code and stays a fast file-only setup check.
        if hf_models.canonical_id(model) != DEFAULT_LOCAL_MODEL:
            os.environ[hf_models.ALLOW_DOWNLOAD_ENV] = "true"
            try:
                hf_models.load_sentence_transformer(
                    model,
                    device=service.backend_name.removeprefix("torch/"),
                )
            except Exception as exc:
                raise MissingModelError(
                    f"Embedding model {model!r} could not be prepared. Run sm setup. Details: {exc}"
                ) from exc
        from smartmemory_app.warm import warm_models

        try:
            warm_models(reranker=False, strict=True, repair_hint=None)
        except Exception as exc:
            # Setup cannot tell the user to run setup; name the cause and a way out.
            raise RuntimeError(
                f"{str(exc).rstrip('. ')}. {_warm_failure_hint(service)}"
            ) from exc
        if missing:
            click.echo(f"Downloaded local embedding model {model!r}.")
        else:
            click.echo(f"Local embedding model {model!r} ready.")
    except Exception as exc:
        raise click.ClickException(download_failure(logger, model, exc)) from exc
    finally:
        if previous_download is None:
            os.environ.pop(hf_models.ALLOW_DOWNLOAD_ENV, None)
        else:
            os.environ[hf_models.ALLOW_DOWNLOAD_ENV] = previous_download
        if previous is None:
            os.environ.pop("SMARTMEMORY_EMBEDDING_PROVIDER", None)
        else:
            os.environ["SMARTMEMORY_EMBEDDING_PROVIDER"] = previous


def _warm_failure_hint(service) -> str:
    """What to try after the embedder downloaded but would not load (CLI-SETUP-CIRCULAR-EMBED-1)."""
    try:
        backend = service.backend_name or ""
    except Exception:
        backend = ""
    if backend.startswith("torch/"):
        return (
            "The torch runtime could not load the model. To use the ONNX runtime for this run, "
            "set SMARTMEMORY_EMBEDDING_BACKEND=onnx and run sm setup again, or repair the torch install."
        )
    return "Run sm doctor for details."


def _ensure_lazy_models() -> None:
    """Prefetch default-on torch consumers, including when embeddings use ONNX."""
    from smartmemory.utils import hf_models

    # MiniLM is shared by domain/type classifiers, hybrid retrieval and clustering.
    # These consume torch weights even when the primary embedder uses ONNX.
    models = (
        "sentence-transformers/all-MiniLM-L6-v2",
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
    )
    for model in models:
        try:
            log_download_start(logger, model, "torch")
            hf_models.resolve_local_path(model, allow_download=True, backend="torch")
        except Exception as exc:
            message = download_failure(logger, model, exc)
            click.echo(f"{message} Its runtime feature may be unavailable.", err=True)


# LITE-FIRSTRUN-SPACY-1: sizes for the one-line download notices (setup and sm add).
_MODEL_SIZES = {
    "en_core_web_sm": "about 15 MB",
    "en_core_web_md": "about 40 MB",
    "en_core_web_lg": "about 560 MB",
    "sentence-transformers/all-MiniLM-L6-v2": "about 100 MB",
}


def _download_note(model: str) -> str:
    from smartmemory.utils.hf_models import canonical_id

    size = _MODEL_SIZES.get(model) or _MODEL_SIZES.get(canonical_id(model))
    return f"{size}, one time only" if size else "one time only"


def _ensure_spacy(
    model: str = "en_core_web_sm", *, replay_on_failure: bool = True
) -> None:
    """Prepare the selected model and the default used by background workers.

    Args:
        model: The configured spaCy model; ``en_core_web_sm`` is always added.
        replay_on_failure: Show core's captured installer output when it fails
            (setup). ``sm add`` passes False to keep its failure to one line; the
            output then goes to the debug log.
    """
    import contextlib
    import io

    import spacy

    from smartmemory.errors import MissingModelError
    from smartmemory.tools.factory import _ensure_spacy_model

    try:
        for required_model in dict.fromkeys((model, "en_core_web_sm")):
            log_download_start(logger, required_model, "spacy")
            downloading = not spacy.util.is_package(required_model)
            if downloading:
                click.echo(
                    f"Downloading spaCy language model {required_model!r} "
                    f"({_download_note(required_model)})..."
                )
            # Core's own progress lines would repeat ours; keep them for the debug log.
            chatter = io.StringIO()
            try:
                with contextlib.redirect_stdout(chatter):
                    _ensure_spacy_model(required_model)
            except (Exception, SystemExit) as exc:
                # On failure the installer's own output is the diagnosis.
                if chatter.getvalue().strip():
                    if replay_on_failure:
                        click.echo(
                            redact_credentials(chatter.getvalue().rstrip()), err=True
                        )
                    else:
                        logger.debug(
                            "spaCy installer output: %s", chatter.getvalue().strip()
                        )
                if isinstance(exc, MissingModelError):
                    raise
                raise MissingModelError(
                    f"Could not install spaCy model {required_model!r}: "
                    f"{type(exc).__name__}: {exc}. "
                    "Rerun sm setup after checking network access."
                ) from exc
            if chatter.getvalue().strip():
                logger.debug("spaCy installer output: %s", chatter.getvalue().strip())
            if downloading:
                click.echo(f"Downloaded spaCy language model {required_model!r}.")
    except MissingModelError as exc:
        raise click.ClickException(
            download_failure(logger, required_model, exc)
        ) from exc


def _copy_hooks() -> None:
    dest = CLAUDE_DIR / "hooks"
    dest.mkdir(parents=True, exist_ok=True)
    for src_name, dest_name in _HOOK_FILE_MAP.items():
        src = HOOKS_SRC / src_name
        if src.exists():
            shutil.copy2(src, dest / dest_name)


def _copy_skills() -> None:
    dest = CLAUDE_DIR / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    for src in SKILLS_SRC.glob("*.md"):
        target = dest / src.name
        if not target.exists():
            shutil.copy2(src, target)


def _register_hooks() -> None:
    shell = _resolve_hook_shell()
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    hooks = cfg.setdefault("hooks", {})
    changed = False

    # Repair our commands in place, including mixed registrations. Foreign
    # commands, matchers, timeouts and other settings survive unchanged.
    for event, registrations in list(hooks.items()):
        existing = registrations if isinstance(registrations, list) else [registrations]
        repaired = []
        for registration in existing:
            entry = registration
            owned = False
            shared = False
            if isinstance(entry, dict):
                if entry.get("command") == "bash" and len(entry.get("args", [])) == 1:
                    name = _installed_hook_name("bash " + str(entry["args"][0]))
                    if name and name not in HOOK_NAMES:
                        continue
                if isinstance(entry.get("hooks"), list):
                    commands = []
                    for hook in entry["hooks"]:
                        name = (
                            _installed_hook_name(hook.get("command", ""))
                            if isinstance(hook, dict) and hook.get("type") == "command"
                            else None
                        )
                        if name in HOOK_NAMES:
                            owned = True
                            hook = {**hook, "command": _hook_command(name, shell)}
                            default = HOOK_TIMEOUTS.get(event)
                            if default and default[0] == name and "timeout" not in hook:
                                hook["timeout"] = default[1]
                        elif name:
                            continue  # our obsolete filenames only
                        else:
                            shared = True
                        commands.append(hook)
                    if not commands and entry["hooks"]:
                        continue
                    entry = {**entry, "hooks": commands}
            if not owned or shared or entry not in repaired:
                repaired.append(entry)
        if repaired != existing:
            hooks[event] = repaired
            changed = True

    # Add missing default registrations, without duplicating repaired entries
    # that carry user-selected timeouts or share a registration with other tools.
    for event, entry in _get_hook_registrations(shell).items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = [existing]
        command = entry["hooks"][0]["command"]
        present = any(
            isinstance(h, dict)
            and h.get("matcher", "") == entry["matcher"]
            and any(
                isinstance(hook, dict) and hook.get("command") == command
                for hook in h.get("hooks", [])
            )
            for h in existing
        )
        if not present:
            existing.append(entry)
            hooks[event] = existing
            changed = True

    if changed:
        SETTINGS.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _seed_data_dir(data_dir: str | None = None) -> None:
    """Seed entity patterns in the data directory.

    Args:
        data_dir: Explicit data directory path. When provided, expanduser() is applied.
                  When None, falls back to SMARTMEMORY_DATA_DIR env var or ~/.smartmemory.
    """
    if data_dir is not None:
        resolved = Path(data_dir).expanduser()
    else:
        raw = os.environ.get("SMARTMEMORY_DATA_DIR")
        resolved = Path(raw) if raw else DATA_DIR
    resolved.mkdir(parents=True, exist_ok=True)
    from smartmemory_app.patterns import JSONLPatternStore

    JSONLPatternStore(
        resolved
    )  # seeds entity_patterns.jsonl if absent (side effect of __init__)


def _daemon_can_read_key(name: str, value: str) -> bool:
    """True when the daemon's own key lookup (keychain, then shell profile) finds ``value``.

    A key that only lives in this shell's env is stored in the OS keychain, the same
    place ``sm setup`` puts a key you type. Never raises. The key is never logged.
    """
    try:
        import keyring

        if keyring.get_password("smartmemory", name) == value:
            return True
        keyring.set_password("smartmemory", name, value)
        # A store that accepts a write but does not hand it back (a null backend)
        # has not saved the key, so only the read-back counts.
        if keyring.get_password("smartmemory", name) == value:
            return True
        logger.warning("OS credential store did not return the %s it was given", name)
    except Exception as exc:
        logger.warning(
            "OS credential store unavailable for %s: %s",
            name,
            redact_credentials(str(exc), (value,)),
        )
    return _read_env_from_profile(name) == value


def _installed_plist_key(name: str) -> str:
    """The ``name`` value an installed daemon or worker plist holds ("" when none).

    Plists written before SETUP-PLIST-PLAINTEXT-KEY-1 carried the provider key, and
    for a key that only lived in the environment they were its only copy.
    """
    import plistlib

    for plist_name in (PLIST_NAME, WORKER_PLIST_NAME):
        try:
            data = plistlib.loads((LAUNCH_AGENTS_DIR / plist_name).read_bytes())
            value = data.get("EnvironmentVariables", {}).get(name, "")
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Schemes a proxy URL may carry. Anything before the last "@" of a proxy value is
# its login, and a prefix that is not one of these schemes is part of that login.
_PROXY_SCHEMES = re.compile(r"(?i)(?:https?|socks4a?|socks5h?)://")


def _plist_env_value(name: str, value: str) -> str:
    """The form of an env value that may be copied into a launchd plist.

    A plist is a plaintext file in LaunchAgents, so the copy rule comes from the
    VARIABLE, never from what its value looks like:

    - ``*_PROXY`` and ``ALL_PROXY`` (any case) hold one proxy URL. Its login is
      dropped whatever characters the password holds (``,``, ``/``, ``@``, ``:``,
      ``%``-escapes), with or without a scheme, and a WARNING names the variable.
    - ``NO_PROXY`` (any case) is a host list and ``HOME`` a path. Neither holds a
      login, so both are copied as they are.

    Any other name raises, so a new env-to-plist copy has to state its rule here.
    """
    upper = name.upper()
    if upper == "NO_PROXY" or name == "HOME":
        return value
    if upper.endswith("_PROXY"):
        return _proxy_without_login(name, value)
    raise ValueError(f"no launchd plist copy rule for {name}")


def _proxy_without_login(name: str, value: str) -> str:
    """``value`` parsed as one proxy URL, with everything before its last ``@`` dropped."""
    if "@" not in value:
        return value
    scheme = _PROXY_SCHEMES.match(value)
    stripped = (scheme.group(0) if scheme else "") + value.rsplit("@", 1)[1]
    logger.warning(
        "%s holds a proxy login (user and password). It was left out of the "
        "launchd plist so the login is not stored in a plaintext file",
        name,
    )
    click.echo(
        f"Warning: {name} holds a login. It was left out of the auto-start files, "
        "so the background service connects without it. If your proxy requires a "
        f"login, point {name} at a local forwarding proxy that needs none and "
        "re-run sm setup."
    )
    return stripped


def _install_launchd_plist() -> bool:
    """Install launchd plist for auto-start + crash recovery (macOS only).

    Substitutes runtime paths, configuration, and configured proxy variables
    into the template plist and writes to ~/Library/LaunchAgents/. Then loads
    the plist with launchctl so launchd starts managing the daemon immediately.

    Returns True if the plist was loaded successfully (caller should NOT also
    call start_daemon — launchd owns the process via RunAtLoad).
    Returns False on non-macOS, missing template, or launchctl failure.
    """
    from smartmemory_app.daemon import _run_command
    from smartmemory_app.diagnostics import redact_credentials

    import platform

    if platform.system() != "Darwin":
        return False  # launchd is macOS-only

    if not PLIST_TEMPLATE.exists():
        click.echo(
            "Warning: launchd plist template not found, skipping auto-start setup."
        )
        return False

    from smartmemory_app.config import load_config

    cfg = load_config()

    python_path = sys.executable
    bin_dir = str(Path(python_path).parent)
    # Use config as source of truth for data_dir — this is what the user chose
    # during setup (or the default). Env var SMARTMEMORY_DATA_DIR is a runtime
    # override that shouldn't be baked into a persistent plist.
    data_dir = str(Path(cfg.data_dir).expanduser())

    # The key never goes into the plist (a plaintext file in LaunchAgents). The
    # daemon and the worker read it from the OS keychain or shell profile at boot
    # (config.load_llm_keys_from_stores), so make sure one of those holds it.
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        try:
            import keyring

            groq_key = (
                keyring.get_password("smartmemory", "GROQ_API_KEY")
                or keyring.get_password("smartmemory", "groq_api_key")
                or ""
            )
        except Exception:
            pass
    # An installed plist written before SETUP-PLIST-PLAINTEXT-KEY-1 may hold the
    # daemon's only persistent copy of the key, and the rewrite below removes it.
    legacy_key = _installed_plist_key("GROQ_API_KEY")
    if not groq_key:
        groq_key = legacy_key
    key_readable = not groq_key or _daemon_can_read_key("GROQ_API_KEY", groq_key)
    # The invariant, checked right before the rewrite whatever the key's source
    # (this shell, the old plist, or a key kept by pressing Enter in sm setup):
    # never replace a plist that holds a key unless the daemon can read a key from
    # a persistent store afterwards. Otherwise leave both files byte-identical.
    if legacy_key and not key_readable:
        click.echo(
            "Warning: your Groq key could not be saved to the OS keychain, and the "
            "existing auto-start files hold the only copy the background service can "
            "read. The files were left as they are. Unlock the keychain or add "
            "GROQ_API_KEY to your shell profile, then run sm setup again."
        )
        return (LAUNCH_AGENTS_DIR / PLIST_NAME).exists()

    proxy_env_xml = "".join(
        f"\n        <key>{name}</key>\n        <string>{escape(value)}</string>"
        for name in (*PROXY_ENV_VARS, "NO_PROXY", "no_proxy")
        if (value := _plist_env_value(name, os.environ.get(name, "")))
    )

    replacements = {
        "{PYTHON_PATH}": python_path,
        "{HOME}": escape(
            _plist_env_value("HOME", os.environ.get("HOME") or str(Path.home()))
        ),
        "{DAEMON_PORT}": str(cfg.daemon_port),
        "{DATA_DIR}": data_dir,
        "{BIN_DIR}": bin_dir,
        "{PROXY_ENV_XML}": proxy_env_xml,
    }

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    all_ok = True

    # Install both daemon and worker plists
    templates = [
        (
            PLIST_TEMPLATE,
            PLIST_NAME,
            _LAUNCHD_DAEMON_LABEL,
            "daemon",
            "smartmemory_app.viewer_server",
            f"main(port={cfg.daemon_port}, open_browser=False)",
        ),
    ]
    if WORKER_PLIST_TEMPLATE.exists():
        templates.append(
            (
                WORKER_PLIST_TEMPLATE,
                WORKER_PLIST_NAME,
                _LAUNCHD_WORKER_LABEL,
                "worker",
                "smartmemory_app.worker_entry",
                "main()",
            )
        )

    for (
        template_path,
        plist_name,
        job_label,
        display_name,
        module,
        main_call,
    ) in templates:
        if not template_path.exists():
            click.echo(f"Warning: {display_name} plist template not found: skipping.")
            continue

        content = template_path.read_text()
        job_replacements = {
            **replacements,
            "{LAUNCHD_LABEL}": job_label,
            "{LAUNCHD_COMMAND}": _launchd_command(job_label, module, main_call),
        }
        for placeholder, value in job_replacements.items():
            content = content.replace(placeholder, value)

        plist_dest = LAUNCH_AGENTS_DIR / plist_name

        # Unload existing plist before overwriting (launchctl requires this)
        if plist_dest.exists():
            _run_command(
                ["launchctl", "unload", str(plist_dest)],
                capture_output=True,
            )

        plist_dest.write_text(content)

        result = _run_command(
            ["launchctl", "load", str(plist_dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.echo(f"Installed launchd plist: {plist_dest}")
        else:
            click.echo(
                f"Warning: launchctl load failed for {display_name}: "
                f"{redact_credentials(result.stderr.strip(), (groq_key,))}"
            )
            click.echo(f"Load manually: launchctl load {plist_dest}")
            all_ok = False

    if all_ok:
        click.echo("SmartMemory will auto-start on login and restart on crash.")
        if groq_key and key_readable:
            click.echo("Enrichment worker enabled (GROQ_API_KEY found).")
        elif groq_key:
            click.echo(
                "Warning: GROQ_API_KEY is set in this shell only, so the background "
                "service cannot read it. Run sm setup to store it in your OS keychain."
            )
        else:
            click.echo(
                "Warning: GROQ_API_KEY not found: enrichment worker won't extract relations."
            )
    return all_ok


def _launchd_command(label: str, module: str, main_call: str) -> str:
    """Build a standalone import guard that remains embedded in the installed plist."""
    return f'''import os
import subprocess
try:
    import smartmemory_app
except (ModuleNotFoundError, ImportError) as exc:
    if getattr(exc, "name", None) != "smartmemory_app":
        raise
    job_label = "{label}"
    message = f"SmartMemory was removed, background job {{job_label}} switched itself off."
    data_dir = os.environ.get("SMARTMEMORY_DATA_DIR", os.path.expanduser("~/.smartmemory"))
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "daemon.log"), "a", encoding="utf-8") as log_file:
            log_file.write(message + "\\n")
    except OSError:
        print(message, flush=True)
    subprocess.run(["launchctl", "bootout", f"gui/{{os.getuid()}}/{{job_label}}"], check=False)
else:
    from {module} import main
    {main_call}'''


def _uninstall_launchd_plist() -> None:
    """Unload and remove daemon + worker launchd plists (macOS only)."""
    import platform

    if platform.system() != "Darwin":
        return

    for plist_name, label in [(PLIST_NAME, "daemon"), (WORKER_PLIST_NAME, "worker")]:
        plist_dest = LAUNCH_AGENTS_DIR / plist_name
        if not plist_dest.exists():
            continue
        subprocess.run(
            ["launchctl", "unload", str(plist_dest)],
            capture_output=True,
        )
        plist_dest.unlink(missing_ok=True)
        click.echo(f"Removed {label} launchd plist.")

    click.echo("SmartMemory will no longer auto-start.")


# ── Uninstall helpers ─────────────────────────────────────────────────────────


def _deregister_hooks() -> None:
    """Remove all SmartMemory hook entries from settings.json (current + legacy)."""
    if not SETTINGS.exists():
        return
    cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
    hooks = cfg.get("hooks", {})
    changed = False

    all_entries = {**_get_hook_registrations(), **_LEGACY_HOOK_REGISTRATIONS}
    for event, entry in all_entries.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = [existing]
        filtered = [h for h in existing if h != entry]
        # Also catch any entry referencing smartmemory hook files
        filtered = [
            h
            for h in filtered
            if not (
                isinstance(h, dict)
                and any(
                    "smartmemory-" in str(v)
                    for v in (
                        h.get("args", [])
                        + [
                            hh.get("command", "")
                            for hh in h.get("hooks", [])
                            if isinstance(hh, dict)
                        ]
                    )
                )
            )
        ]
        if len(filtered) != len(existing):
            hooks[event] = filtered
            changed = True

    if changed:
        SETTINGS.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _remove_hooks() -> None:
    for name in HOOK_NAMES:
        path = HOOKS_DEST / name
        if path.exists():
            path.unlink()
    # Clean up legacy (non-namespaced) files only if they're SmartMemory-only
    for src_name in _HOOK_FILE_MAP:
        path = HOOKS_DEST / src_name
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if "smartmemory_app" in content and content.count("\n") < 15:
                path.unlink()


def _remove_skills() -> None:
    skills_dest = CLAUDE_DIR / "skills"
    for name in SKILL_NAMES:
        path = skills_dest / name
        if path.exists():
            path.unlink()


def _remove_config_file() -> None:
    from smartmemory_app.config import config_path

    path = config_path()
    if path.exists():
        path.unlink()
        click.echo(f"Removed config file: {path}")


def _remove_data_dir() -> None:
    raw = os.environ.get("SMARTMEMORY_DATA_DIR")
    data_dir = Path(raw) if raw else DATA_DIR
    if data_dir.exists():
        shutil.rmtree(data_dir)
        click.echo(f"Removed data directory: {data_dir}")


if __name__ == "__main__":
    setup()
