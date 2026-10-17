"""DIST-SETUP-TUI-1: Textual TUI for `smartmemory setup`.

Arrow-key selection for mode, LLM provider, embedding provider. Live model
discovery for ollama/lmstudio. Summary screen with edit/toggle. Progress
screen with per-step checklist + daemon spinner.

Entry point: run_setup_tui() -> SetupResult | None
"""

import os
import sys
import time

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    OptionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from smartmemory_app.setup import NO_LOCAL_MODEL_NOTE, SetupResult
from smartmemory_app.setup_steps import (
    PlanStep,
    estimate_phrase,
    format_elapsed,
    local_setup_plan,
)


# ── Provider definitions ──────────────────────────────────────────────────

LLM_PROVIDERS = [
    ("groq", "Free tier, fast inference (recommended)"),
    ("claude-agent", "Claude Agent SDK, OAuth (no API key)"),
    ("openai", "OpenAI API"),
    ("anthropic", "Anthropic API"),
    ("gemini", "Google Gemini API"),
    ("ollama", "Local, free (llama3.1, mistral)"),
    ("lmstudio", "Local, OpenAI-compatible endpoint"),
    ("none", "EntityRuler only (very limited)"),
]

EMBEDDING_PROVIDERS = [
    ("local", "Built-in (no API key needed)"),
    ("openai", "OpenAI embeddings (1536-dim)"),
    ("ollama", "Local Ollama embeddings"),
]

_KEY_VARS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

LOCAL_MODEL_PROVIDERS = {"ollama", "lmstudio"}
NO_MODEL_ID = "__none__"
NO_MODEL_LABEL = "none (default)  decide later"


def _detect_keys() -> str:
    """Build a status line showing detected API keys."""
    parts = []
    for provider, var in _KEY_VARS.items():
        found = bool(os.environ.get(var))
        icon = "[green]✓[/]" if found else "[dim]✗[/]"
        parts.append(f"{var} {icon}")
    return "  ".join(parts)


# ── Screens ───────────────────────────────────────────────────────────────


class WelcomeScreen(Screen):
    BINDINGS = [Binding("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="welcome-box"):
                yield Static("\n[bold]Welcome to SmartMemory[/bold]\n", id="title")
                yield Static("Where do you want to store memories?\n")
                yield OptionList(
                    Option("Local: on this machine, no account needed", id="local"),
                    Option("Remote: SmartMemory hosted service", id="remote"),
                    id="mode-list",
                )
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "remote":
            self.app.exit(SetupResult(mode="remote"))
        else:
            self.app.push_screen(LLMScreen())

    def action_quit(self) -> None:
        self.app.exit(None)


class LLMScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="llm-box"):
                yield Static(
                    "\n[bold]LLM Provider[/bold]\n\n"
                    "SmartMemory uses an LLM to extract entities\n"
                    "and relationships from your text.\n"
                )
                options = [
                    Option(f"{pid:<14s} {desc}", id=pid) for pid, desc in LLM_PROVIDERS
                ]
                yield OptionList(*options, id="llm-list")
                yield Static(f"\nKeys: {_detect_keys()}\n", id="key-badges")
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        provider = str(event.option.id)
        self.app._result.llm_provider = provider
        if provider in LOCAL_MODEL_PROVIDERS:
            self.app.push_screen(ModelScreen(provider))
        else:
            self.app.push_screen(EmbeddingScreen())

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(None)


class ModelScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, provider: str) -> None:
        super().__init__()
        self._provider = provider

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="model-box"):
                yield Static("\n[bold]Model Selection[/bold]\n")
                yield Static(
                    f"Discovering models on {self._provider}...\n", id="status"
                )
                yield LoadingIndicator(id="loader")
                yield OptionList(id="model-list")
                yield Static(f"\n{NO_LOCAL_MODEL_NOTE}", id="no-model-note")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#model-list", OptionList).display = False
        self._discover()

    @work(exclusive=True, group="discover")
    async def _discover(self) -> None:
        models = await _discover_models(self._provider)
        loader = self.query_one("#loader", LoadingIndicator)
        status = self.query_one("#status", Static)
        model_list = self.query_one("#model-list", OptionList)

        loader.display = False
        # The default is none, so a found list never forces a pick and an
        # unreachable provider still leaves a way forward.
        model_list.clear_options()
        model_list.add_option(Option(NO_MODEL_LABEL, id=NO_MODEL_ID))
        if models:
            status.update(f"Found {len(models)} models:\n")
            for m in models:
                name = m["name"]
                size = m.get("size", "")
                label = f"{name:<30s} {size}" if size else name
                model_list.add_option(Option(label, id=name))
        else:
            status.update(f"[yellow]Could not reach {self._provider}[/yellow]\n")
        model_list.display = True
        model_list.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = str(event.option.id)
        self.app._result.llm_model = "" if chosen == NO_MODEL_ID else chosen
        self.app.push_screen(EmbeddingScreen())

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(None)


class EmbeddingScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="embed-box"):
                yield Static("\n[bold]Embedding Provider[/bold]\n")
                has_openai = bool(os.environ.get("OPENAI_API_KEY"))
                if has_openai:
                    yield Static(
                        "[green]OPENAI_API_KEY detected[/green], defaulting to openai\n"
                    )
                options = [
                    Option(f"{pid:<10s} {desc}", id=pid)
                    for pid, desc in EMBEDDING_PROVIDERS
                ]
                ol = OptionList(*options, id="embed-list")
                yield ol
        yield Footer()

    def on_mount(self) -> None:
        # Highlight openai if key detected
        if os.environ.get("OPENAI_API_KEY"):
            ol = self.query_one("#embed-list", OptionList)
            ol.highlighted = 1  # openai is index 1

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.app._result.embedding_provider = str(event.option.id)
        self.app.push_screen(SummaryScreen())

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(None)


class SummaryScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        r = self.app._result
        with Center():
            with Vertical(id="summary-box"):
                yield Static("\n[bold]Configuration Summary[/bold]\n")
                yield Static(f"  Mode:        {r.mode}")
                yield Static(
                    f"  LLM:         {r.llm_provider}"
                    + (f" ({r.llm_model})" if r.llm_model else "")
                    + (
                        " (no local model)"
                        if r.llm_provider in LOCAL_MODEL_PROVIDERS and not r.llm_model
                        else ""
                    )
                )
                yield Static(f"  Embeddings:  {r.embedding_provider}")
                yield Static("")
                yield Static("  Data directory:")
                yield Input(value=r.data_dir, id="data-dir-input")
                yield Static("")
                with Vertical(id="coref-row"):
                    yield Static(
                        "  Coreference resolution (~500MB model):", id="coref-label"
                    )
                    yield Switch(value=r.coreference, id="coref-switch")
                yield Static("")
                with Center():
                    yield Button("Confirm", id="confirm-btn", variant="success")
                    yield Button("Back", id="back-btn", variant="default")
                    yield Button("Cancel", id="cancel-btn", variant="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            r = self.app._result
            r.data_dir = self.query_one("#data-dir-input", Input).value
            r.coreference = self.query_one("#coref-switch", Switch).value
            self.app.push_screen(ProgressScreen())
        elif event.button.id == "back-btn":
            self.app.pop_screen()
        elif event.button.id == "cancel-btn":
            self.app.exit(None)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(None)


class ProgressScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        # CLI-SETUP-UX-1: the same numbered plan the plain CLI prints, estimates included.
        self._plan = local_setup_plan(self.app._result.embedding_provider)
        with Center():
            with Vertical(id="progress-box"):
                yield Static("\n[bold]Setting up SmartMemory...[/bold]\n")
                for index, step in enumerate(self._plan, start=1):
                    yield Static(
                        self._row(
                            "●" if index == 1 else "○",
                            index,
                            step,
                            estimate_phrase(step),
                        ),
                        id=f"step-{step.key}",
                    )
                yield LoadingIndicator(id="daemon-spinner")
                yield Static("", id="daemon-detail")
                yield Static("", id="final-status")
        yield Footer()

    def _row(self, symbol: str, index: int, step: PlanStep, suffix: str) -> str:
        """One checklist row. The counter is escaped so markup never reads it as a tag."""
        return (
            f"{symbol} {escape(f'[{index}/{len(self._plan)}]')} {step.name} ({suffix})"
        )

    def _step_at(self, name: str) -> tuple[int, PlanStep] | None:
        for index, step in enumerate(getattr(self, "_plan", ()), start=1):
            if step.name == name:
                return index, step
        return None

    def on_mount(self) -> None:
        self._setup_finished = False
        self._daemon_started_at: float | None = None
        self._step_started = time.monotonic()
        self.query_one("#daemon-spinner", LoadingIndicator).display = False
        self.set_interval(1, self._update_daemon_elapsed)
        self._run_setup()

    def _mark_step_done(self, name: str) -> None:
        found = self._step_at(name)
        if found is None:
            return
        index, step = found
        took = format_elapsed(time.monotonic() - self._step_started)
        self._step_started = time.monotonic()
        self.query_one(f"#step-{step.key}", Static).update(
            self._row("[green]✓[/green]", index, step, f"took {took}")
        )
        if index < len(self._plan):
            nxt = self._plan[index]
            self.query_one(f"#step-{nxt.key}", Static).update(
                self._row("●", index + 1, nxt, estimate_phrase(nxt))
            )

    def _set_final_status(self, status_text: str) -> None:
        self.query_one("#final-status", Static).update(status_text)
        self._setup_finished = True

    def _begin_daemon_wait(self) -> None:
        self._daemon_started_at = time.monotonic()
        self.query_one("#daemon-spinner", LoadingIndicator).display = True
        self._update_daemon_elapsed()

    def _finish_daemon_wait(self) -> None:
        self._daemon_started_at = None
        self.query_one("#daemon-spinner", LoadingIndicator).display = False

    def _update_daemon_elapsed(self) -> None:
        started = getattr(self, "_daemon_started_at", None)
        if started is None:
            return
        elapsed = int(time.monotonic() - started)
        found = self._step_at("SmartMemory started")
        if found is None:
            return
        index, step = found
        self.query_one("#step-daemon", Static).update(
            self._row("●", index, step, f"{elapsed}s, {estimate_phrase(step)}")
        )

    def _show_daemon_log(self, line: str) -> None:
        """Place the latest complete daemon progress line under the spinner."""
        self.app.call_from_thread(
            self.query_one("#daemon-detail", Static).update,
            line,
        )

    @work(thread=True)
    def _run_setup(self) -> None:
        def on_step(name: str) -> None:
            self.app.call_from_thread(self._mark_step_done, name)

        try:
            from smartmemory_app.setup import _apply_setup_result, _start_daemon_local

            _apply_setup_result(
                self.app._result, on_step=on_step, on_log=self._show_daemon_log
            )

            self.app.call_from_thread(self._begin_daemon_wait)
            try:
                daemon_status = _start_daemon_local(on_log=self._show_daemon_log)
            finally:
                self.app.call_from_thread(self._finish_daemon_wait)

            if daemon_status and daemon_status.get("status") == "ok":
                self.app.call_from_thread(self._mark_step_done, "SmartMemory started")
                status_text = '\n[bold green]SmartMemory is ready![/bold green]\n\nTry: [cyan]smartmemory add "hello world"[/cyan]\n\nPress any key to exit.'
            elif daemon_status and daemon_status.get("status") == "warming":
                self.app._result.daemon_warming = True
                self.app.call_from_thread(
                    self.query_one("#step-daemon", Static).update,
                    "[yellow]⚠[/yellow] SmartMemory is still warming up",
                )
                status_text = (
                    "\n[bold yellow]Setup is configured, but SmartMemory is still warming up.[/bold yellow]"
                    "\nCheck: [cyan]sm status[/cyan]. Additional worker startup is deferred. Once warmup finishes, run [cyan]sm start --wait[/cyan]."
                    "\n\nPress any key to exit."
                )
            elif daemon_status and daemon_status.get("status") == "degraded":
                reason = (
                    daemon_status.get("degraded_reason") or "No reason was reported."
                )
                self.app.call_from_thread(
                    self.query_one("#step-daemon", Static).update,
                    "[yellow]⚠[/yellow] SmartMemory needs attention",
                )
                status_text = (
                    "\n[bold yellow]Setup finished, but SmartMemory needs attention.[/bold yellow]"
                    f"\n{reason}\n\nRun: [cyan]sm doctor[/cyan]\n\nPress any key to exit."
                )
            else:
                self.app.call_from_thread(
                    self.query_one("#step-daemon", Static).update,
                    "[red]✗[/red] SmartMemory did not start",
                )
                status_text = "\n[bold red]Setup could not start SmartMemory.[/bold red]\nRun: [cyan]sm doctor[/cyan]\n\nPress any key to exit."

            self.app.call_from_thread(self._set_final_status, status_text)
        except BaseException as e:
            import logging
            from smartmemory_app.diagnostics import redact_credentials

            from smartmemory_app.runtime_diagnostics import record_cli_crash
            import sys

            if isinstance(e, Exception):
                record_cli_crash(e, sys.argv[1:])
            logging.getLogger(__name__).warning(
                "Setup TUI worker failed", exc_info=True
            )
            message = redact_credentials(
                " ".join(str(e).split()) or e.__class__.__name__
            )
            try:
                import click
                from smartmemory_app.bug_report import debug_log_path

                if not isinstance(e, click.ClickException):
                    message = redact_credentials(
                        f"{type(e).__name__}: {message[:500]}\nLog: {debug_log_path()}\n"
                        "Run `smartmemory report --zip` and email the file to support@smartmemory.ai."
                    )
            except Exception:
                pass
            self.app._setup_error = message
            self.app.call_from_thread(
                self._set_final_status,
                f"\n[bold red]Setup failed: {message}[/bold red]\n\nPress any key to exit.",
            )

    def on_key(self) -> None:
        if getattr(self, "_setup_finished", False):
            self.app.exit(self.app._result)


# ── Model discovery ───────────────────────────────────────────────────────


async def _discover_models(provider: str) -> list[dict]:
    """Fetch available models from local/API provider."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            if provider == "ollama":
                r = await client.get("http://localhost:11434/api/tags")
                r.raise_for_status()
                models = r.json().get("models", [])
                return [
                    {"name": m["name"], "size": _format_size(m.get("size", 0))}
                    for m in models
                ]
            elif provider == "lmstudio":
                r = await client.get("http://localhost:1234/v1/models")
                r.raise_for_status()
                return [{"name": m["id"]} for m in r.json().get("data", [])]
    except Exception:
        pass
    return []


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable (e.g., 4.7 GB)."""
    if size_bytes <= 0:
        return ""
    gb = size_bytes / (1024**3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = size_bytes / (1024**2)
    return f"{mb:.0f} MB"


# ── App ───────────────────────────────────────────────────────────────────


class SetupApp(App):
    TITLE = "SmartMemory Setup"
    CSS = """
    #welcome-box, #llm-box, #model-box, #embed-box, #summary-box, #progress-box {
        width: 60;
        max-width: 80;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #title {
        text-align: center;
    }
    OptionList {
        height: auto;
        max-height: 12;
    }
    #data-dir-input {
        width: 40;
    }
    #coref-row {
        height: 3;
        layout: horizontal;
    }
    #coref-label {
        width: 1fr;
    }
    Button {
        margin: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._result = SetupResult()
        self._setup_error: str | None = None

    def on_mount(self) -> None:
        self.push_screen(WelcomeScreen())


# ── Entry point ───────────────────────────────────────────────────────────


def run_setup_tui() -> SetupResult | None:
    """Launch the Textual TUI. Returns SetupResult on success, None on cancel."""
    app = SetupApp()
    try:
        result = app.run()
    finally:
        # Restore terminal reporting before click prompts, including on crashes.
        try:
            if sys.stdout.isatty():
                sys.stdout.write(
                    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?1004l"
                )
                sys.stdout.flush()
        except Exception:
            pass
    if app._setup_error is not None:
        import click

        raise click.ClickException(app._setup_error)
    return result
