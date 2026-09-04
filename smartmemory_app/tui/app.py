"""DIST-LITE-10: `sm explore` — a Textual graph browser over the lite daemon.

Not a graph picture. A terminal cannot lay out a force-directed graph legibly
past a few dozen nodes, and Tier-2 enrichment routinely yields hundreds, so the
app browses: one focused node, its adjacency list grouped by relation type, a
walk with history, search, ask, and a live tail of the daemon's SSE frames.

Keys: up/down move · enter walk · backspace back · / search · ? ask · q quit.
"""

from __future__ import annotations

import logging
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input

from smartmemory_app.tui.client import (
    ExploreClient,
    ExploreUnavailable,
    FocusView,
    frame_item_ids,
)
from smartmemory_app.tui.panes import (
    Breadcrumb,
    FocusPane,
    LivePane,
    RelationsPane,
    ResultsPane,
)

log = logging.getLogger(__name__)

HINT = (
    "[bold]sm explore[/bold]\n\n"
    "Press [bold]/[/bold] to search for a memory, [bold]?[/bold] to ask a question.\n"
    "[dim]up/down move · enter walk · backspace back · q quit[/dim]"
)


class ExploreApp(App):
    """Terminal graph browser. `client` is injected so tests can fake the daemon."""

    CSS = """
    Screen { layout: vertical; }
    #breadcrumb { height: 1; padding: 0 1; background: $panel; }
    #body { height: 1fr; }
    #left { width: 2fr; }
    #right { width: 1fr; border-left: solid $panel; }
    #focus { height: auto; max-height: 40%; padding: 1; border-bottom: solid $panel; }
    #focus.flash { background: $accent 20%; }
    #relations { height: 1fr; }
    #results { height: 40%; border-top: solid $panel; }
    #live { height: 1fr; padding: 0 1; }
    #query { display: none; }
    #query.visible { display: block; }
    .group-header { background: $panel; }
    """

    # The query box is hidden until `/` or `?` opens it; without this the app's
    # default auto-focus lands on it and swallows every key binding.
    AUTO_FOCUS = "#relations"

    BINDINGS = [
        Binding("backspace", "back", "Back"),
        Binding("slash", "search", "Search"),
        Binding("question_mark", "ask", "Ask"),
        Binding("escape", "cancel_input", "Cancel", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        client: ExploreClient,
        start_id: Optional[str] = None,
        *,
        live: bool = True,
    ) -> None:
        super().__init__()
        self.client = client
        self.start_id = start_id
        self.live = live
        self.focus_view: Optional[FocusView] = None
        self.history: list[str] = []
        self.trail: list[str] = []
        self._input_mode = "search"
        # Tests wait on this instead of App.workers.wait_for_complete(), which
        # would block forever on the never-completing live tail.
        self.io_pending = 0

    # ── layout ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Breadcrumb(id="breadcrumb")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield FocusPane(HINT, id="focus")
                yield RelationsPane(id="relations")
                yield ResultsPane(id="results")
            with Vertical(id="right"):
                yield LivePane(id="live", markup=True, wrap=True)
        yield Input(placeholder="search…", id="query")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).can_focus = False
        self.query_one(Breadcrumb).set_path(self.trail)
        if self.live:
            self.tail_progress()
        if self.start_id:
            self.load_focus(self.start_id, push=False)

    # ── focus ───────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="focus")
    def load_focus(self, item_id: str, push: bool = True) -> None:
        """Read node + adjacency off the UI thread, then apply on it."""
        self.io_pending += 1
        try:
            view = self.client.focus(item_id)
        except ExploreUnavailable as exc:
            log.warning("explore: focus %s failed: %s", item_id, exc)
            self.call_from_thread(self._apply_error, str(exc))
            return
        finally:
            self.io_pending -= 1
        self.call_from_thread(self._apply_focus, view, push)

    def _apply_focus(self, view: FocusView, push: bool) -> None:
        if push and self.focus_view is not None:
            self.history.append(self.focus_view.item_id)
        self.focus_view = view
        if push or not self.trail:
            self.trail.append(view.label)
        else:
            self.trail[-1] = view.label
        self.query_one(FocusPane).show(view)
        self.query_one(RelationsPane).set_view(view)
        self.query_one(Breadcrumb).set_path(self.trail)
        self.query_one(RelationsPane).focus()

    def _apply_error(self, message: str) -> None:
        self.query_one(FocusPane).show_message(f"[red]{message}[/red]")
        self.query_one(LivePane).add_note(message)

    def action_back(self) -> None:
        if not self.history:
            self.query_one(LivePane).add_note("already at the start of the walk")
            return
        previous = self.history.pop()
        if self.trail:
            self.trail.pop()
        self.focus_view = None
        self.load_focus(previous, push=False)

    # ── walk ────────────────────────────────────────────────────────────

    def on_list_view_selected(self, event) -> None:
        pane = event.list_view
        target: Optional[str] = None
        if isinstance(pane, RelationsPane):
            row = getattr(event.item, "row", None)
            target = row.focus_id if row is not None else None
        else:
            target = getattr(event.item, "focus_id", None)
        if target:
            self.load_focus(target)

    # ── search / ask ────────────────────────────────────────────────────

    def action_search(self) -> None:
        self._open_input("search", "search memories…")

    def action_ask(self) -> None:
        self._open_input("ask", "ask a question…")

    def _open_input(self, mode: str, placeholder: str) -> None:
        self._input_mode = mode
        query = self.query_one(Input)
        query.placeholder = placeholder
        query.value = ""
        query.add_class("visible")
        query.can_focus = True
        query.focus()

    def action_cancel_input(self) -> None:
        query = self.query_one(Input)
        query.remove_class("visible")
        query.can_focus = False
        self.query_one(RelationsPane).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.action_cancel_input()
        if not text:
            return
        if self._input_mode == "ask":
            self.run_ask(text)
        else:
            self.run_search(text)

    @work(thread=True, group="query")
    def run_search(self, query: str) -> None:
        self.io_pending += 1
        try:
            hits = self.client.search(query, top_k=10)
        except ExploreUnavailable as exc:
            log.warning("explore: search failed: %s", exc)
            self.call_from_thread(self._apply_results_error, str(exc))
            return
        finally:
            self.io_pending -= 1
        self.call_from_thread(self._apply_search, query, hits)

    @work(thread=True, group="query")
    def run_ask(self, question: str) -> None:
        self.io_pending += 1
        try:
            response = self.client.ask(question)
        except ExploreUnavailable as exc:
            log.warning("explore: ask failed: %s", exc)
            self.call_from_thread(self._apply_results_error, str(exc))
            return
        finally:
            self.io_pending -= 1
        self.call_from_thread(self._apply_ask, question, response)

    def _apply_search(self, query: str, hits: list[dict]) -> None:
        results = self.query_one(ResultsPane)
        results.set_search(query, hits)
        results.focus()

    def _apply_ask(self, question: str, response: dict) -> None:
        results = self.query_one(ResultsPane)
        results.set_ask(question, response)
        results.focus()

    def _apply_results_error(self, message: str) -> None:
        self.query_one(ResultsPane).set_message(f"[red]{message}[/red]")
        self.query_one(LivePane).add_note(message)

    # ── live tail ───────────────────────────────────────────────────────

    @work(group="live")
    async def tail_progress(self) -> None:
        """One line per SSE frame; frames touching the focused item flash."""
        live = self.query_one(LivePane)
        live.add_note("live: connecting to /memory/progress/stream")
        try:
            async for frame in self.client.stream_progress():
                self.on_frame(frame)
        except ExploreUnavailable as exc:
            log.warning("explore: live tail unavailable: %s", exc)
            live.add_note(f"live tail unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001 - the tail must never kill the app
            log.warning("explore: live tail stopped: %s", exc)
            live.add_note(f"live tail stopped: {exc}")

    def on_frame(self, frame: dict) -> None:
        focused = self.focus_view.item_id if self.focus_view else None
        flash = bool(focused and focused in frame_item_ids(frame))
        self.query_one(LivePane).add_frame(frame, flash=flash)
        if flash:
            pane = self.query_one(FocusPane)
            pane.add_class("flash")
            self.set_timer(0.6, lambda: pane.remove_class("flash"))
