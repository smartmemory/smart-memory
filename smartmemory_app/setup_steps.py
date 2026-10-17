"""CLI-SETUP-UX-1: numbered setup steps with measured time estimates.

Every setup step prints ``[n/N] <step> ... (about <estimate>)`` before it runs and a
done or failed line with the elapsed time after. Output is plain lines: no control
characters reach a pipe, CI log or launchd log. On a terminal, long steps also show
the existing Rich spinner from ``progress.startup_progress``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Callable, ContextManager, Iterator

import click

# Measured 2026-10-17 by the measure script in the CLI-SETUP-UX-1 unit folder
# (units/CLI-SETUP-UX-1/measure.py, raw rows in measure-output.txt): a clean venv
# (Python 3.12, smartmemory 2.0 wrapper with core 1.5.26 from PyPI), an empty tmp HOME
# (no spaCy model, no Hugging Face cache) and a real network, on a Mac under a 1-minute
# load of 87, so a quiet machine is usually faster. Seconds per step:
#   spacy 70.6, embedding 95.8, extra models 302.2, and the small local steps
#   (config, hooks, skills, register, seed) under 0.1 each, measured one by one.
# A step with no row here prints no estimate, only its real elapsed time afterwards:
#   "restart" (stop_daemon touches launchd and live workers) and "daemon"
#   (_start_daemon_local installs launchd and the worker agent), neither of which is
#   safe to run against a throwaway HOME. Do not add a number for them without a row.
# Re-measure with that script when a model or the dependency set changes.
STEP_ESTIMATE_SECONDS = {
    "config": 1,
    "spacy": 71,
    "embedding": 96,
    "extra_models": 302,
    "hooks": 1,
    "skills": 1,
    "register": 1,
    "seed": 1,
}


@dataclass(frozen=True)
class PlanStep:
    """One numbered setup step. ``name`` is the done-name ``_apply_setup_result`` reports."""

    key: str
    name: str
    title: str

    @property
    def estimate_seconds(self) -> int | None:
        """Measured seconds, or None when the step has no measurement."""
        return STEP_ESTIMATE_SECONDS.get(self.key)


def local_setup_plan(
    embedding_provider: str, *, restart: bool = False
) -> list[PlanStep]:
    """The ordered steps of a local setup. ``restart`` adds the stop of a live daemon."""
    steps = [
        PlanStep("config", "Config written", "Saving your settings"),
        PlanStep("spacy", "spaCy model ready", "Downloading the language model"),
    ]
    if embedding_provider == "local":
        steps.append(
            PlanStep(
                "embedding", "Embedding model ready", "Downloading the embedding model"
            )
        )
    steps += [
        PlanStep(
            "extra_models",
            "Helper models ready",
            "Downloading the search helper models",
        ),
        PlanStep("hooks", "Hooks installed", "Installing Claude Code hooks"),
        PlanStep("skills", "Skills installed", "Installing Claude Code skills"),
        PlanStep("register", "Hooks registered", "Registering the hooks"),
        PlanStep("seed", "Patterns seeded", "Seeding starter patterns"),
    ]
    if restart:
        steps.append(
            PlanStep(
                "restart", "SmartMemory stopped", "Stopping the running SmartMemory"
            )
        )
    steps.append(PlanStep("daemon", "SmartMemory started", "Starting SmartMemory"))
    return steps


def format_estimate(seconds: float) -> str:
    """Round a measured duration to a phrase that follows "about", never to false precision."""
    if seconds <= 5:
        return "5 seconds"
    if seconds < 10:
        return "10 seconds"
    if seconds < 90:
        return f"{round(seconds / 10) * 10} seconds"
    minutes = max(2, round(seconds / 60))
    return f"{minutes} minutes"


def format_elapsed(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    if whole < 60:
        return f"{whole}s"
    return f"{whole // 60}m {whole % 60:02d}s"


def estimate_phrase(step: PlanStep) -> str:
    """ "about 2 minutes" for a measured step, an empty string for one with no measurement."""
    seconds = step.estimate_seconds
    return "" if seconds is None else f"about {format_estimate(seconds)}"


def model_download_progress(
    on_log: Callable[[str], None] | None,
) -> ContextManager[None]:
    """Newline-only model download progress while a step runs, or a no-op without a sink."""
    if on_log is None:
        return nullcontext()
    from smartmemory_app.hf_progress import discrete_huggingface_progress

    return discrete_huggingface_progress(on_log)


class StepTracker:
    """Print the numbered counter for each step of one setup run."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._plan: list[PlanStep] = []

    def set_plan(self, plan: list[PlanStep]) -> None:
        self._plan = list(plan)

    def _locate(self, name: str) -> tuple[int, PlanStep]:
        for index, step in enumerate(self._plan, start=1):
            if step.name == name:
                return index, step
        # A caller step that is not in the plan still gets a counter, so none is silent.
        step = PlanStep("daemon", name, name)
        self._plan.append(step)
        return len(self._plan), step

    @contextmanager
    def step(self, name: str, *, live: bool = True) -> Iterator[Callable[[str], None]]:
        """Run one step. Yields a log callback for streamed detail lines.

        ``live=False`` skips the spinner for a body that draws its own (the daemon start).
        """
        index, step = self._locate(name)
        label = f"[{index}/{len(self._plan)}] {step.title}"
        phrase = estimate_phrase(step)
        click.echo(f"{label} ... ({phrase})" if phrase else f"{label} ...")
        started = self._clock()
        try:
            if live:
                from smartmemory_app.progress import startup_progress

                with startup_progress(label, emit=click.echo) as on_log:
                    yield on_log
            else:
                yield lambda line: click.echo(f"  {line}")
        except BaseException:
            click.echo(
                f"{label} failed after {format_elapsed(self._clock() - started)}",
                err=True,
            )
            raise
        click.echo(f"{label} done ({format_elapsed(self._clock() - started)})")
