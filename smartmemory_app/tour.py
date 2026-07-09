"""DIST-TOUR-1: guided onboarding tour for `sm tour`.

The tour deliberately drives the same local daemon HTTP API as normal usage:
`POST /memory/ingest`, `POST /memory/search`, and `GET /memory/recall`.
The browser viewer observes the existing live graph event path; this module
does not emit graph events itself.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import httpx
import tiktoken
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

SEARCH_QUERY = "which database did we pick"
RECALL_QUERY = "what should I remember when this project starts"

DEFAULT_TOUR_FACTS: tuple[str, ...] = (
    "Project Atlas picked Postgres as the application database because reporting queries need joins.",
    "Deploys go through GitHub Actions; never deploy manually from a laptop.",
    "Staging rate-limits uploads at 100 requests per minute, so batch the uploader.",
    "SmartMemory local mode stores data under SMARTMEMORY_DATA_DIR in an isolated SQLite graph.",
    "The graph viewer listens to /memory/progress/stream and updates as memories are added.",
    "Use sm recall at session start to restore the relevant project context.",
    "Architecture decisions should be captured immediately with sm add while the reasoning is fresh.",
    "The support runbook says to restart the daemon with sm restart after changing local providers.",
)

logger = logging.getLogger(__name__)

EventCallback = Callable[["TourEvent"], None]


@dataclass(frozen=True)
class TokenReceipt:
    full_context_tokens: int
    recall_tokens: int
    full_context_text: str
    recall_text: str


@dataclass(frozen=True)
class TourEvent:
    step: int
    title: str
    body: str
    command: str = ""


@dataclass(frozen=True)
class TourRunResult:
    data_dir: Path
    port: int
    viewer_url: str
    seeded_count: int
    search_text: str
    recall_text: str
    receipt: TokenReceipt
    imported_claude: bool
    store_was_empty: bool
    store_populated: bool
    imported_claude_search_text: str = ""
    code_stub: bool = False
    success: bool = True
    degraded_reason: str = ""


class TourClient(Protocol):
    def ingest(self, content: str, memory_type: str = "semantic") -> dict: ...

    def search(self, query: str, top_k: int = 5) -> dict: ...

    def recall(
        self, cwd: str | None = None, top_k: int = 8, query: str | None = None
    ) -> dict: ...

    def close(self) -> None: ...


@lru_cache(maxsize=1)
def _cl100k_encoder() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens with the cl100k_base encoder used by the baseline receipt."""
    return len(_cl100k_encoder().encode(text))


def build_claude_md_equivalent(facts: Sequence[str]) -> str:
    """Render the seeded facts as the CLAUDE.md-style context being avoided."""
    lines = [
        "# Project Memory",
        "",
        "These are the project facts this assistant would otherwise reread every turn:",
        "",
    ]
    lines.extend(f"- {fact}" for fact in facts)
    return "\n".join(lines).strip() + "\n"


def compute_token_receipt(
    facts: Sequence[str],
    recall_payload: str | dict | list,
) -> TokenReceipt:
    """Compute the tour receipt from real text, not pre-baked constants."""
    full_context = build_claude_md_equivalent(facts)
    if isinstance(recall_payload, str):
        recall_text = recall_payload
    else:
        recall_text = json.dumps(recall_payload, sort_keys=True)
    return TokenReceipt(
        full_context_tokens=count_tokens(full_context),
        recall_tokens=count_tokens(recall_text),
        full_context_text=full_context,
        recall_text=recall_text,
    )


class TourHttpClient:
    """Small daemon HTTP client for the tour arc."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            trust_env=False,
        )

    def ingest(self, content: str, memory_type: str = "semantic") -> dict:
        response = self._client.post(
            "/memory/ingest",
            json={
                "content": content,
                "memory_type": memory_type,
                "context": {"origin": "cli:add", "memory_type": memory_type},
                "properties": {"tour": "DIST-TOUR-1"},
            },
        )
        response.raise_for_status()
        return response.json()

    def search(self, query: str, top_k: int = 5) -> dict:
        response = self._client.post(
            "/memory/search",
            json={"query": query, "top_k": top_k},
        )
        response.raise_for_status()
        return response.json()

    def recall(
        self, cwd: str | None = None, top_k: int = 8, query: str | None = None
    ) -> dict:
        response = self._client.get(
            "/memory/recall",
            params={
                "cwd": cwd or "",
                "top_k": top_k,
                "query": query or "",
                "include_snapshot": "true",
                "strict": "false",
            },
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


class TourArcDriver:
    """Runs the offline-safe default tour arc against an already-running daemon."""

    def __init__(
        self,
        client: TourClient,
        facts: Sequence[str] = DEFAULT_TOUR_FACTS,
        cwd: str | Path | None = None,
        pace_seconds: float = 0.45,
    ) -> None:
        self.client = client
        self.facts = tuple(facts)
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.pace_seconds = pace_seconds

    def run_default_arc(
        self,
        *,
        include_claude_import: bool = True,
        emit: EventCallback | None = None,
    ) -> TourRunResult:
        seeded_ids: list[str] = []
        self._emit(
            emit,
            1,
            "Seed project facts",
            "Adding the pinned facts one by one. The viewer should grow as the daemon stores them.",
            'POST /memory/ingest {"content": "...", "memory_type": "semantic"}',
        )
        for fact in self.facts:
            result = self.client.ingest(fact, memory_type="semantic")
            seeded_ids.append(str(result.get("item_id", "?")))
            self._emit(
                emit,
                1,
                "Seed project facts",
                f"Added {len(seeded_ids)}/{len(self.facts)}: {fact}",
                'POST /memory/ingest {"content": "...", "memory_type": "semantic"}',
            )
            if self.pace_seconds:
                time.sleep(self.pace_seconds)

        search_response = self.client.search(SEARCH_QUERY, top_k=5)
        search_text = _format_search_response(search_response)
        self._emit(
            emit,
            2,
            "Semantic search",
            search_text,
            f'POST /memory/search {{"query": "{SEARCH_QUERY}", "top_k": 5}}',
        )

        recall_response = self.client.recall(
            cwd=str(self.cwd),
            top_k=8,
            query=RECALL_QUERY,
        )
        recall_text = _extract_recall_text(recall_response)
        receipt = compute_token_receipt(self.facts, recall_text)

        degraded_reason = _tour_degraded_reason(
            facts=self.facts,
            search_response=search_response,
            recall_text=recall_text,
        )
        if degraded_reason:
            logger.warning("SmartMemory tour degraded: %s", degraded_reason)
            self._emit(
                emit,
                3,
                "DEGRADED: seeded content was not returned",
                (
                    f"DEGRADED: {degraded_reason}\n"
                    "The tour stops before showing the token receipt because "
                    "the daemon did not return the seeded content."
                ),
                f'POST /memory/search; GET /memory/recall?query="{RECALL_QUERY}"',
            )
            return TourRunResult(
                data_dir=Path(),
                port=0,
                viewer_url="",
                seeded_count=len(seeded_ids),
                search_text=search_text,
                recall_text=recall_text,
                receipt=receipt,
                imported_claude=False,
                store_was_empty=False,
                store_populated=False,
                success=False,
                degraded_reason=degraded_reason,
            )

        self._emit(
            emit,
            3,
            "Token receipt",
            (
                f"CLAUDE.md-equivalent: {receipt.full_context_tokens} tokens\n"
                f"Recall payload: {receipt.recall_tokens} tokens"
            ),
            "tiktoken cl100k_base",
        )
        self._emit(
            emit,
            4,
            "Cross-session recall",
            recall_text or "(recall returned no context)",
            f'GET /memory/recall?top_k=8&query="{RECALL_QUERY}"',
        )

        imported_claude = False
        imported_search_text = ""
        claude_path = self.cwd / "CLAUDE.md"
        if include_claude_import and claude_path.exists() and claude_path.is_file():
            content = claude_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                self.client.ingest(content, memory_type="semantic")
                imported_claude = True
                imported_search_text = _format_search_response(
                    self.client.search("project instructions", top_k=3)
                )
                self._emit(
                    emit,
                    5,
                    "Import your CLAUDE.md",
                    imported_search_text,
                    "sm add --all - < ./CLAUDE.md",
                )
        if not imported_claude:
            self._emit(
                emit,
                5,
                "Import your CLAUDE.md",
                "No ./CLAUDE.md found in this directory, so the import step was skipped.",
                "sm add --all - < ./CLAUDE.md",
            )

        cheat_sheet = "\n".join(
            [
                'sm add "We chose Postgres for reporting queries"',
                'sm search "which database did we pick"',
                "sm recall --query 'startup context'",
                "sm add - < notes.txt",
                "sm add --all - < CLAUDE.md",
                "sm viewer",
            ]
        )
        self._emit(
            emit,
            6,
            "Try it on your project",
            cheat_sheet,
            "sm tour",
        )
        return TourRunResult(
            data_dir=Path(),
            port=0,
            viewer_url="",
            seeded_count=len(seeded_ids),
            search_text=search_text,
            recall_text=recall_text,
            receipt=receipt,
            imported_claude=imported_claude,
            imported_claude_search_text=imported_search_text,
            store_was_empty=False,
            store_populated=False,
        )

    @staticmethod
    def _emit(
        emit: EventCallback | None,
        step: int,
        title: str,
        body: str,
        command: str = "",
    ) -> None:
        if emit is not None:
            emit(TourEvent(step=step, title=title, body=body, command=command))


class TourDaemon:
    """Isolated viewer daemon spawned directly through viewer_server.main()."""

    def __init__(
        self,
        port: int | None = None,
        keep: bool = False,
        data_dir: str | Path | None = None,
        health_timeout: float = 75.0,
    ) -> None:
        self.port = port
        self.keep = keep
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.health_timeout = health_timeout
        self.process: subprocess.Popen | None = None
        self.store_was_empty = False
        self.store_populated = False
        self.base_url = ""

    def start(self) -> "TourDaemon":
        requested_port = self.port
        attempts = 3 if requested_port is None else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if requested_port is None:
                self.port = _free_port()
            else:
                self.port = requested_port
                if not _is_port_available(self.port):
                    raise RuntimeError(
                        f"port {self.port} is in use; omit --port to auto-select "
                        "a free one"
                    )
            try:
                return self._start_selected_port()
            except BaseException as exc:
                last_error = exc
                self.stop()
                if (
                    requested_port is None
                    and attempt < attempts - 1
                    and _looks_like_auto_port_race(exc)
                ):
                    self.data_dir = None
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("tour daemon did not start")

    def _start_selected_port(self) -> "TourDaemon":
        if self.port is None:
            raise RuntimeError("tour daemon port was not selected")
        if self.data_dir is None:
            self.data_dir = Path(tempfile.mkdtemp(prefix="smartmemory-tour-"))
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_was_empty = not any(self.data_dir.iterdir())
        self.base_url = f"http://127.0.0.1:{self.port}"

        env = {
            **os.environ,
            "SMARTMEMORY_MODE": "local",
            "SMARTMEMORY_DATA_DIR": str(self.data_dir),
            "SMARTMEMORY_DAEMON_PORT": str(self.port),
            "SMARTMEMORY_EMBEDDING_PROVIDER": "local",
        }
        code = (
            "from smartmemory_app.viewer_server import main; "
            f"main(port={self.port!r}, open_browser=False)"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            self._wait_until_healthy()
            self._assert_reached_empty_store()
        except BaseException:
            self.stop()
            raise
        return self

    def mark_populated(self) -> None:
        self.store_populated = self._has_store_files()

    def stop(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.mark_populated()
        finally:
            self.process = None
            if self.data_dir is not None and not self.keep:
                shutil.rmtree(self.data_dir, ignore_errors=True)

    def _wait_until_healthy(self) -> None:
        deadline = time.monotonic() + self.health_timeout
        last_error: Exception | None = None
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError(
                        f"tour daemon exited early with code {self.process.returncode}"
                    )
                try:
                    response = client.get(f"{self.base_url}/health")
                    if response.status_code == 200:
                        return
                except Exception as exc:
                    last_error = exc
                time.sleep(0.25)
        detail = f": {last_error}" if last_error else ""
        raise TimeoutError(f"tour daemon did not become healthy{detail}")

    def _assert_reached_empty_store(self) -> None:
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            response = client.get(f"{self.base_url}/memory/graph/full")
            response.raise_for_status()
            payload = response.json()
        node_count = _graph_node_count(payload)
        if node_count != 0:
            raise RuntimeError(
                "tour daemon identity check failed: reached a non-empty memory "
                "graph; aborting before ingest to avoid writing to another daemon"
            )

    def _has_store_files(self) -> bool:
        if self.data_dir is None or not self.data_dir.exists():
            return False
        return any(self.data_dir.rglob("*"))


class TourSessionRunner:
    """Owns daemon lifecycle, viewer launch, and the default arc."""

    def __init__(
        self,
        *,
        keep: bool = False,
        code: bool = False,
        port: int | None = None,
        no_viewer: bool = False,
        cwd: str | Path | None = None,
        daemon_factory: Callable[[int | None, bool], Any] | None = None,
        client_factory: Callable[[str], TourClient] | None = None,
        facts: Sequence[str] = DEFAULT_TOUR_FACTS,
        pace_seconds: float = 0.45,
        daemon_handle: Any | None = None,
    ) -> None:
        self.keep = keep
        self.code = code
        self.port = port
        self.no_viewer = no_viewer
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.daemon_factory = daemon_factory or (
            lambda p, k: TourDaemon(port=p, keep=k)
        )
        self.client_factory = client_factory or TourHttpClient
        self.facts = tuple(facts)
        self.pace_seconds = pace_seconds
        self.daemon_handle = daemon_handle

    def start_daemon_for_app(self) -> Any | None:
        if self.code:
            return None
        if self.daemon_handle is not None:
            return self.daemon_handle
        daemon = self.daemon_factory(self.port, self.keep)
        self.daemon_handle = daemon.start()
        return self.daemon_handle

    def run(self, emit: EventCallback | None = None) -> TourRunResult:
        if self.code:
            return self._run_code_stub(emit)

        owns_daemon = self.daemon_handle is None
        if owns_daemon:
            daemon = self.daemon_factory(self.port, self.keep)
            handle = daemon.start()
        else:
            daemon = self.daemon_handle
            handle = self.daemon_handle
        client: TourClient | None = None
        handle_port = int(getattr(handle, "port", self.port or 0))
        base_url = getattr(handle, "base_url", "") or f"http://127.0.0.1:{handle_port}"
        viewer_url = f"http://localhost:{handle_port}"
        result: TourRunResult | None = None
        try:
            if emit is not None:
                emit(
                    TourEvent(
                        step=0,
                        title="Opening your graph viewer",
                        body=(
                            "Drag the browser next to this terminal, then watch the "
                            "isolated graph grow as the tour runs."
                        ),
                        command=viewer_url,
                    )
                )
            if not self.no_viewer:
                webbrowser.open(viewer_url)
            client = self.client_factory(base_url)
            driver = TourArcDriver(
                client=client,
                facts=self.facts,
                cwd=self.cwd,
                pace_seconds=self.pace_seconds,
            )
            result = driver.run_default_arc(include_claude_import=True, emit=emit)
            if hasattr(handle, "mark_populated"):
                handle.mark_populated()
            data_dir = Path(getattr(handle, "data_dir", Path()))
            store_was_empty = bool(getattr(handle, "store_was_empty", False))
            store_populated = bool(getattr(handle, "store_populated", False))
            if data_dir.exists():
                store_populated = store_populated or any(data_dir.rglob("*"))
            result = replace(
                result,
                data_dir=data_dir,
                port=handle_port,
                viewer_url=viewer_url,
                store_was_empty=store_was_empty,
                store_populated=store_populated,
            )
        finally:
            if client is not None:
                client.close()
            if owns_daemon:
                _stop_daemon(daemon, handle)
        if result is None:
            raise RuntimeError("tour did not produce a result")
        return result

    def _run_code_stub(self, emit: EventCallback | None) -> TourRunResult:
        body = (
            "The code-intelligence branch is coming soon. The default tour is "
            "offline-safe and complete; run `sm tour` without --code to watch it."
        )
        if emit is not None:
            emit(TourEvent(step=0, title="Code tour coming soon", body=body))
        receipt = compute_token_receipt(["Code tour coming soon"], body)
        return TourRunResult(
            data_dir=Path(),
            port=self.port or 0,
            viewer_url="",
            seeded_count=0,
            search_text="",
            recall_text=body,
            receipt=receipt,
            imported_claude=False,
            store_was_empty=True,
            store_populated=False,
            code_stub=True,
        )


class TourScreen(Screen):
    BINDINGS = [
        Binding("enter", "next", "Start / Next"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Center():
            with Vertical(id="tour-box"):
                yield Static("", id="tour-title")
                yield Static("", id="tour-body")
                yield Static("", id="tour-command")
                with VerticalScroll(id="tour-scroll"):
                    yield Static("", id="tour-output")
        yield Footer()

    def on_mount(self) -> None:
        self.app._tour_screen = self
        self.app.show_intro()

    def action_next(self) -> None:
        self.app.action_next()

    def action_quit(self) -> None:
        self.app.exit(None)


class TourApp(App):
    TITLE = "SmartMemory Tour"
    CSS = """
    #tour-box {
        width: 88;
        max-width: 96;
        height: 34;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #tour-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #tour-body {
        margin-bottom: 1;
    }
    #tour-command {
        color: $accent;
        margin-bottom: 1;
    }
    #tour-scroll {
        height: 1fr;
    }
    """

    def __init__(self, runner: TourSessionRunner) -> None:
        super().__init__()
        self.runner = runner
        self.current_step = 0
        self.last_output = ""
        self.event_history: list[TourEvent] = []
        self._tour_screen: TourScreen | None = None
        self._started = False
        self._finished = False

    def on_mount(self) -> None:
        self.push_screen(TourScreen())

    def show_intro(self) -> None:
        if getattr(self.runner, "code", False):
            self._show_event(
                TourEvent(
                    step=0,
                    title="Code tour coming soon",
                    body=(
                        "Press Enter to see the placeholder. The complete offline "
                        "tour runs with `sm tour` without --code."
                    ),
                    command="sm tour --code",
                )
            )
            return
        self._show_event(
            TourEvent(
                step=0,
                title="Opening your graph viewer",
                body=(
                    "Press Enter to start the isolated tour. If the viewer opens, "
                    "drag it next to this terminal and watch it grow."
                ),
                command="sm tour",
            )
        )

    def action_next(self) -> None:
        if self._finished:
            self.exit(getattr(self, "_result", None))
            return
        if not self._started:
            self._started = True
            self._show_event(
                TourEvent(
                    step=0,
                    title="Starting isolated tour store",
                    body="Launching the local-only daemon for this tour.",
                    command="smartmemory_app.viewer_server.main(open_browser=False)",
                )
            )
            self._run_tour()

    @work(thread=True)
    def _run_tour(self) -> None:
        def emit(event: TourEvent) -> None:
            self.call_from_thread(self._show_event, event)

        try:
            result = self.runner.run(emit=emit)
            self.call_from_thread(self._finish, result)
        except BaseException as exc:
            self.call_from_thread(
                self._show_event,
                TourEvent(
                    step=self.current_step,
                    title="Tour failed",
                    body=str(exc) or exc.__class__.__name__,
                    command="",
                ),
            )
            self.call_from_thread(self._mark_finished)
            self.call_from_thread(self.exit, None)

    def _finish(self, result: TourRunResult) -> None:
        self._result = result
        self._show_event(
            TourEvent(
                step=6 if result.success else 3,
                title="Tour complete" if result.success else "Tour degraded",
                body=_completion_body(result, keep=getattr(self.runner, "keep", None)),
                command="Press Enter to exit.",
            )
        )
        self._mark_finished()

    def _mark_finished(self) -> None:
        self._finished = True

    def _show_event(self, event: TourEvent) -> None:
        self.current_step = event.step
        self.last_output = event.body
        self.event_history.append(event)
        screen = self._tour_screen
        if screen is None:
            return
        screen.query_one("#tour-title", Static).update(
            f"[bold]Step {event.step}: {event.title}[/bold]"
        )
        screen.query_one("#tour-body", Static).update(event.body)
        screen.query_one("#tour-command", Static).update(event.command)
        screen.query_one("#tour-output", Static).update(event.body)


def run_tour(
    keep: bool = False,
    code: bool = False,
    port: int | None = None,
    no_viewer: bool = False,
) -> TourRunResult | None:
    """Launch the guided tour TUI."""
    runner = TourSessionRunner(
        keep=keep,
        code=code,
        port=port,
        no_viewer=no_viewer,
    )
    daemon_handle: Any | None = None
    try:
        daemon_handle = runner.start_daemon_for_app()
        app = TourApp(runner)
        return app.run()
    finally:
        if daemon_handle is not None:
            _stop_daemon(daemon_handle, daemon_handle)


def _format_search_response(response: dict | list) -> str:
    items = response.get("items", []) if isinstance(response, dict) else response
    if not items:
        return "No results."
    lines = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        memory_type = item.get("memory_type", "?")
        item_id = str(item.get("item_id", "?"))[:8]
        content = str(item.get("content", "")).strip()
        lines.append(f"[{memory_type}] {item_id} {content}")
    return "\n".join(lines) if lines else "No results."


def _extract_recall_text(response: dict | str) -> str:
    if isinstance(response, str):
        return response
    context = response.get("context", "")
    return str(context)


def _tour_degraded_reason(
    *,
    facts: Sequence[str],
    search_response: dict | list,
    recall_text: str,
) -> str:
    reasons: list[str] = []
    postgres_fact = next((fact for fact in facts if "Postgres" in fact), "")
    if not postgres_fact or not _search_contains_fact(search_response, postgres_fact):
        reasons.append("search did not return the seeded Postgres fact")
    if not recall_text.strip() or not any(fact in recall_text for fact in facts):
        reasons.append("recall did not return non-empty seeded content")
    return "; ".join(reasons)


def _search_contains_fact(response: dict | list, fact: str) -> bool:
    items = response.get("items", []) if isinstance(response, dict) else response
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and fact in str(item.get("content", "")):
            return True
    return False


def _graph_node_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise RuntimeError(
            "tour daemon identity check failed: /memory/graph/full returned "
            "an unexpected payload"
        )
    count = int(payload.get("node_count", 0) or 0)
    nodes = payload.get("nodes", [])
    if isinstance(nodes, list):
        count = max(count, len(nodes))
    return count


def _is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _looks_like_auto_port_race(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "exited early" in str(exc)


def _stop_daemon(daemon: Any, handle: Any) -> None:
    stop = getattr(handle, "stop", None)
    if callable(stop):
        stop()
        return
    stop = getattr(daemon, "stop", None)
    if callable(stop):
        stop()


def _completion_body(result: TourRunResult, keep: bool | None = None) -> str:
    if keep is True:
        store_status = f"Store kept at {result.data_dir}"
    elif keep is False and result.data_dir.exists():
        store_status = "Temporary store will be removed on exit."
    elif result.data_dir.exists():
        store_status = f"Store kept at {result.data_dir}"
    else:
        store_status = "Temporary store removed."
    if not result.success:
        return (
            f"DEGRADED: {result.degraded_reason}\n"
            f"Seeded {result.seeded_count} memories. {store_status}"
        )
    return f"Seeded {result.seeded_count} memories. {store_status}"
