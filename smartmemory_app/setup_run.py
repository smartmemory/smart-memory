"""SETUP-RESUMABLE-1: run the setup steps as a resumable, checked sequence.

The step list is ``setup_steps.setup_plan`` (one list for every setup path). For
each step this module runs the step, then its check (``setup_verify.verify_step``),
and saves the result in ``setup_state`` before moving on:

  pending   not reached yet
  done      ran without an error, check not passed yet
  verified  its check passed (the effect is really there)
  failed    the step or its check failed. ``reason`` says why.

Resume rule. ``sm setup`` after an unfinished run (any step not verified) reuses the
saved choices instead of asking again, then walks the steps in order: a step counts
as finished only when it was recorded done or verified AND its check passes now. The
first step that fails that test is where the run restarts, and every step after it
runs again too (a later step can depend on an earlier one, for example the daemon on
the config). ``sm setup --restart`` drops the saved progress and asks again. A run
whose every step is verified is complete, so the next ``sm setup`` is a new setup.

Steps run by the functions ``smartmemory_app.setup`` already has (looked up on the
module at call time, so a test that patches ``setup._ensure_spacy`` patches this).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import click

from smartmemory_app import setup_state, setup_verify
from smartmemory_app.setup_state import (
    DONE,
    FAILED,
    OPTIONAL_KEYS,
    VERIFIED,
    SetupState,
)
from smartmemory_app.setup_steps import PlanStep, StepTracker, setup_plan

log = logging.getLogger(__name__)


# One plain fix line per step when its body raises (a failed check brings its own).
RUN_FIX = {
    "config": "Check that the config folder is writable, then run: sm setup",
    "spacy": "Check your network connection, then run: sm setup (it resumes at this step)",
    "embedding": "Check your network connection, then run: sm setup (it resumes at this step)",
    "extra_models": "Check your network connection, then run: sm setup",
    "hooks": "Check that ~/.claude is writable, then run: sm setup",
    "skills": "Check that ~/.claude is writable, then run: sm setup",
    "register": "Check that ~/.claude/settings.json is valid JSON, then run: sm setup",
    "seed": "Check that the data folder is writable, then run: sm setup",
    "restart": "Run: sm stop, then run: sm setup",
    "daemon": "Run: sm doctor, then run: sm start --wait",
    "auth": "Run: sm login",
    "mode": "Run: sm doctor",
    "roundtrip": "Run: sm doctor",
}

# Steps whose body draws its own progress (or is instant): no spinner around them.
_NOT_LIVE = frozenset(
    {"config", "hooks", "skills", "register", "seed", "daemon", "mode", "auth"}
)


def plan_for(choices: dict) -> list[PlanStep]:
    return setup_plan(
        choices.get("mode", "local"),
        choices.get("embedding_provider", "local"),
        restart=bool(choices.get("restart")),
    )


def _cfg_from(choices: dict):
    from smartmemory_app.config import SmartMemoryConfig

    return SmartMemoryConfig(
        mode="local",
        coreference=bool(choices.get("coreference", False)),
        llm_provider=choices.get("llm_provider", "none"),
        llm_model=choices.get("llm_model", ""),
        embedding_provider=choices.get("embedding_provider", "local"),
        spacy_model=choices.get("spacy_model", "en_core_web_sm"),
        data_dir=choices.get("data_dir", "~/.smartmemory"),
    )


class SetupRun:
    """One setup run over the shared step list, its state saved after every step."""

    def __init__(
        self,
        state: SetupState,
        *,
        tracker: Optional[StepTracker] = None,
        on_step: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        quiet: bool = False,
        api_key: Optional[str] = None,
        check_restart: bool = True,
    ) -> None:
        self.state = state
        self.plan = plan_for(state.choices)
        self.tracker = tracker or StepTracker()
        self.tracker.set_plan(self.plan)
        self.on_step = on_step
        self.on_log = on_log
        self.quiet = quiet  # TUI: the screen draws progress, so no counter lines
        self.api_key = api_key  # remote: used for this run only, never saved
        self.daemon_status: dict = {}
        # Local click runs ask again before the daemon step whether one is running
        # (it can start or stop during the downloads). The TUI never stopped one.
        self._restart_pending = check_restart
        self._tracker_log: Optional[Callable[[str], None]] = None

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def fresh(cls, choices: dict, **kwargs) -> "SetupRun":
        state = SetupState.new(choices, [s.key for s in plan_for(choices)])
        setup_state.save(state)
        return cls(state, **kwargs)

    @classmethod
    def resumable(cls, *, mode: Optional[str] = None) -> Optional[SetupState]:
        """The saved state of an unfinished run, or None.

        ``mode`` is the --mode flag: a saved run in the other mode is not resumed.
        """
        state = setup_state.load()
        if state is None or state.complete:
            return None
        if mode is not None and state.choices.get("mode") != mode:
            return None
        return state

    # ── bookkeeping ───────────────────────────────────────────────────────

    def _mark(self, key: str, status: str, reason: str = "") -> None:
        self.state.mark(key, status, reason)
        try:
            setup_state.save(self.state)
        except Exception as exc:  # progress is a convenience, never a reason to stop
            log.warning(
                "Could not save setup progress (%s). A re-run starts over.",
                type(exc).__name__,
            )

    def _set_restart(self, restart: bool) -> None:
        """Add or drop the stop step when the daemon state changed during the downloads."""
        if bool(self.state.choices.get("restart")) == restart:
            return
        self.state.choices["restart"] = restart
        self.plan = plan_for(self.state.choices)
        self.tracker.set_plan(self.plan)
        keys = [s.key for s in self.plan]
        records = {r.key: r for r in self.state.steps}
        self.state.steps = [records.get(k) or setup_state.StepRecord(k) for k in keys]
        setup_state.save(self.state)

    def index_of(self, key: str) -> int:
        for index, step in enumerate(self.plan):
            if step.key == key:
                return index
        return len(self.plan)

    def first_unrecorded(self) -> int:
        """Index of the first step not recorded done or verified (no checks re-run)."""
        for index, step in enumerate(self.plan):
            if step.key in OPTIONAL_KEYS:
                continue
            if self.state.status(step.key) not in (DONE, VERIFIED):
                return index
        return len(self.plan)

    def first_unfinished(self) -> int:
        """Index of the first step to run again (resume rule in the module docstring)."""
        for index, step in enumerate(self.plan):
            if step.key in OPTIONAL_KEYS:
                continue
            if self.state.status(step.key) not in (DONE, VERIFIED):
                return index
            check = setup_verify.verify_step(step.key, self.state.choices)
            if not check.ok:
                log.warning(
                    "Setup step %s was finished but its check fails now (%s). Running it again.",
                    step.key,
                    check.detail,
                )
                return index
            if self.state.status(step.key) == DONE:
                self._mark(step.key, VERIFIED)  # its check passed just now
        return len(self.plan)

    # ── running ───────────────────────────────────────────────────────────

    def _body(self, step: PlanStep) -> None:
        from smartmemory_app import setup
        from smartmemory_app.setup_steps import model_download_progress

        key, choices = step.key, self.state.choices
        if key == "config":
            from smartmemory_app.config import save_config

            save_config(_cfg_from(choices))
        elif key == "spacy":
            setup._ensure_spacy(choices.get("spacy_model", "en_core_web_sm"))
        elif key == "embedding":
            with model_download_progress(self._log):
                setup._ensure_embedding_model(choices.get("embedding_provider"))
        elif key == "extra_models":
            with model_download_progress(self._log):
                setup._ensure_lazy_models()
        elif key == "hooks":
            setup._copy_hooks()
        elif key == "skills":
            setup._copy_skills()
        elif key == "register":
            setup._register_hooks()
        elif key == "seed":
            seed_dir = choices.get("seed_dir")
            setup._seed_data_dir(seed_dir) if seed_dir else setup._seed_data_dir()
        elif key == "restart":
            from smartmemory_app.daemon import stop_daemon

            stop_daemon()
            time.sleep(2)
        elif key == "daemon":
            status = (
                setup._start_daemon_local(on_log=self.on_log)
                if self.on_log
                else setup._start_daemon_local()
            )
            self.daemon_status = status or {}
        elif key == "auth":
            setup._remote_login(self.api_key, choices.get("api_url"))
        # "mode" and "roundtrip" have no body: their check is the step.

    @property
    def _log(self):
        return self.on_log or self._tracker_log

    def _run_one(self, step: PlanStep) -> None:
        if self.quiet:
            self._body(step)
            return
        with self.tracker.step(step.name, live=step.key not in _NOT_LIVE) as log_line:
            self._tracker_log = log_line
            try:
                self._body(step)
            finally:
                self._tracker_log = None

    def _before_restart(self) -> None:
        """Ask again after the downloads whether a daemon runs (it can start or stop)."""
        self._restart_pending = False
        if self.state.choices.get("mode") != "local":
            return
        if self.state.status("restart") in (DONE, VERIFIED):
            return  # stopped already in this run: keep the step and its counter
        from smartmemory_app.daemon import is_running

        self._set_restart(is_running(require_healthy=False))

    def run(self, start: int = 0, *, stop_before: Optional[str] = None) -> None:
        """Run steps from ``start``. ``stop_before`` ends before that step (the TUI's daemon)."""
        index = start
        while index < len(self.plan):
            step = self.plan[index]
            if self._restart_pending and step.key in ("restart", "daemon"):
                self._before_restart()
                step = self.plan[index]
            if stop_before and step.key == stop_before:
                return
            self.state.mark(step.key, "pending")
            try:
                self._run_one(step)
            except BaseException as exc:
                reason = (
                    exc.format_message()
                    if isinstance(exc, click.ClickException)
                    else f"{type(exc).__name__}: {exc}"
                )
                self._mark(step.key, FAILED, reason)
                # A setup error that already names its fix (the daemon's) keeps it alone.
                if (
                    isinstance(exc, Exception)
                    and not self.quiet
                    and "Fix:" not in reason
                    and "Next step:" not in reason
                ):
                    click.echo(
                        f"Fix: {RUN_FIX.get(step.key, 'Run: sm doctor')}", err=True
                    )
                raise
            self._mark(step.key, DONE)
            check = setup_verify.verify_step(step.key, self.state.choices)
            if check.ok:
                self._mark(step.key, VERIFIED)
                if step.key == "roundtrip":
                    self.state.verified_scope = check.detail
                    setup_state.save(self.state)
                if not self.quiet and step.key in ("mode", "roundtrip"):
                    click.echo(f"  {check.detail}")
                if not self.quiet and step.key == "mode":
                    click.echo(f"  {setup_verify.MODE_HELP}")
            else:
                self._mark(step.key, FAILED, check.detail)
                if step.key in OPTIONAL_KEYS:
                    if self.quiet and self.on_log:  # the TUI shows it in its log
                        self.on_log(f"Warning: {check.detail}. Fix: {check.fix}")
                    else:
                        click.echo(f"Warning: {check.detail}", err=True)
                        click.echo(f"Fix: {check.fix}", err=True)
                else:
                    raise click.ClickException(
                        f"Setup stopped at step {index + 1} of {len(self.plan)}: "
                        f"{step.title}.\nProblem: {check.detail}\nFix: {check.fix}\n"
                        "Then run sm setup again. It resumes at this step."
                    )
            if self.on_step:
                self.on_step(step.name)
            index += 1

    def resume(self) -> None:
        """Run from the first unfinished step, saying which steps are kept."""
        start = self.first_unfinished()
        total = len(self.plan)
        if start >= total:
            click.echo("Every setup step is already done and checked.")
            return
        click.echo(
            f"Resuming the setup started {self.state.started_at} at step "
            f"{start + 1} of {total}. Start over instead: sm setup --restart"
        )
        for index, step in enumerate(self.plan[:start], start=1):
            click.echo(f"[{index}/{total}] {step.name} (already done, checked)")
        self.run(start)


def mark_step(key: str, status: str, reason: str = "") -> None:
    """Record one step run outside a SetupRun (the TUI's daemon start)."""
    state = setup_state.load()
    if state is None:
        log.warning(
            "No setup progress file to record step %s in. A re-run of sm setup starts over.",
            key,
        )
        return
    state.mark(key, status, reason)
    setup_state.save(state)
