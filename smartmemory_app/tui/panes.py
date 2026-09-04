"""DIST-LITE-10: the panes `sm explore` composes.

A terminal cannot draw a force-directed graph legibly, so this is a browser and
not a picture: a focus pane, an adjacency list grouped by relation type, an
ask/search results list, and a live tail of the daemon's SSE frames.
"""

from __future__ import annotations

from typing import Optional

from textual.widgets import Label, ListItem, ListView, RichLog, Static

from smartmemory_app.tui.client import FocusView, RelationRow, frame_line


class Breadcrumb(Static):
    """The walk so far, oldest first. Backspace pops the last hop."""

    def set_path(self, labels: list[str]) -> None:
        if not labels:
            self.update("[dim]no focus[/dim]")
            return
        trail = " › ".join(labels[-6:])
        prefix = "… › " if len(labels) > 6 else ""
        self.update(f"[bold]{prefix}{trail}[/bold]")


class FocusPane(Static):
    """The focused node: label, ids, type, and the stored text."""

    def show(self, view: FocusView) -> None:
        body = view.content.strip() or "[dim](no content)[/dim]"
        self.update(
            f"[bold]{view.label}[/bold]\n"
            f"[dim]{view.item_id}  ·  {view.node_category}/{view.memory_type}  ·  "
            f"{len(view.rows)} relations[/dim]\n\n"
            f"{body}"
        )

    def show_message(self, message: str) -> None:
        self.update(message)


class RelationRowItem(ListItem):
    """A selectable adjacency row. `row` is None for a relation-type header."""

    def __init__(self, text: str, row: Optional[RelationRow] = None) -> None:
        super().__init__(Label(text))
        self.text = text
        self.row = row
        if row is None:
            self.add_class("group-header")


class RelationsPane(ListView):
    """One-hop adjacency, grouped by relation type. Enter walks the selection."""

    def set_view(self, view: FocusView) -> None:
        self.clear()
        groups = view.grouped()
        if not groups:
            self.append(
                RelationRowItem("[dim](no relations — infra edges are filtered)[/dim]")
            )
            return
        for relation_type, rows in groups:
            self.append(RelationRowItem(f"[bold]{relation_type}[/bold] ({len(rows)})"))
            for row in rows:
                self.append(RelationRowItem(f"  {row.render()}", row))
        # Land the cursor on the first walkable row, not the type header, so a
        # single Enter walks and up/down read as "move between relations".
        self.index = self._first_selectable()

    def _first_selectable(self) -> int:
        for index, child in enumerate(self.children):
            if getattr(child, "row", None) is not None:
                return index
        return 0

    def selected_row(self) -> Optional[RelationRow]:
        item = self.highlighted_child
        return getattr(item, "row", None) if item is not None else None


class ResultRowItem(ListItem):
    """A search hit, or an ask evidence/relation row. `focus_id` is what Enter walks to."""

    def __init__(self, text: str, focus_id: Optional[str] = None) -> None:
        super().__init__(Label(text))
        self.text = text
        self.focus_id = focus_id


class ResultsPane(ListView):
    """Search results and ask output. Every row with an id refocuses on Enter."""

    def set_search(self, query: str, hits: list[dict]) -> None:
        self.clear()
        self.append(ResultRowItem(f"[bold]search:[/bold] {query} ({len(hits)})"))
        for hit in hits:
            item_id = str(hit.get("item_id") or hit.get("id") or "")
            content = " ".join(str(hit.get("content") or "").split())[:100]
            self.append(ResultRowItem(f"  {content or item_id}", item_id or None))
        self._land_cursor()

    def set_ask(self, question: str, response: dict) -> None:
        """Render an ask response per DIST-LITE-9 ask-contract.json."""
        self.clear()
        self.append(ResultRowItem(f"[bold]ask:[/bold] {question}"))
        self.append(ResultRowItem(f"  [bold]{response.get('answer', '')}[/bold]"))
        reasoning = str(response.get("reasoning") or "").strip()
        if reasoning:
            self.append(ResultRowItem(f"  [dim]{reasoning}[/dim]"))
        evidence = [e for e in (response.get("evidence") or []) if isinstance(e, dict)]
        if evidence:
            self.append(ResultRowItem("  [bold]evidence[/bold]"))
        for item in evidence:
            item_id = str(item.get("item_id") or "")
            content = " ".join(str(item.get("content") or "").split())[:90]
            self.append(ResultRowItem(f"    {content or item_id}", item_id or None))
        relations = [
            r for r in (response.get("relations") or []) if isinstance(r, dict)
        ]
        if relations:
            self.append(ResultRowItem("  [bold]relations[/bold]"))
        for relation in relations:
            # ask-contract.json: source_id/target_id are what a host focuses on;
            # the labels alone cannot address a graph element.
            source_id = str(relation.get("source_id") or "")
            self.append(
                ResultRowItem(
                    f"    {relation.get('source', '?')} --{relation.get('type', '?')}--> "
                    f"{relation.get('target', '?')}",
                    source_id or None,
                )
            )
        self._land_cursor()

    def set_message(self, message: str) -> None:
        self.clear()
        self.append(ResultRowItem(message))

    def _land_cursor(self) -> None:
        """Cursor starts on the first row that can actually be walked."""
        for index, child in enumerate(self.children):
            if getattr(child, "focus_id", None):
                self.index = index
                return
        self.index = 0

    def selected_focus_id(self) -> Optional[str]:
        item = self.highlighted_child
        return getattr(item, "focus_id", None) if item is not None else None


class LivePane(RichLog):
    """Tail of GET /memory/progress/stream — one line per frame."""

    def add_frame(self, frame: dict, *, flash: bool) -> None:
        line = frame_line(frame)
        self.write(f"[reverse]{line}[/reverse]" if flash else line)

    def add_note(self, note: str) -> None:
        self.write(f"[dim]{note}[/dim]")
