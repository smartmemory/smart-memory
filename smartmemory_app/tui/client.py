"""DIST-LITE-10: thin httpx wrapper over the lite daemon, for `sm explore`.

No new backend. Every read is an endpoint that already ships on :9014:

    GET  /health                     daemon reachability + mode
    POST /memory/search              search box (`/`)
    GET  /memory/{id}                focus node
    GET  /memory/{id}/neighbors      one-hop edges (DIST-OBSIDIAN-LITE-1 shape)
    POST /memory/ask                 ask box (`?`), DIST-LITE-9 ask-contract.json
    GET  /memory/progress/stream     live tail, PLAT-PUSH-SSE-1 frame contract

Relation rows mirror `sm ask`: structural edges (GROUNDED_IN, CONTAINS_ENTITY,
MENTIONED_IN, HAS_VERSION) describe how memory is wired rather than what it
claims, so they are not shown as relations. Because a memory item's own edges
are almost entirely structural, the semantic edges reachable from it are the
ones hanging off its entities — exactly what `local_api._ask_relations` walks.
This client walks the same hop and marks those rows `via`, so filtering the
infra edges does not strand the walk on a memory node.

Read shapes are pinned in
smart-memory-docs/docs/features/DIST-LITE-10/explore-contract.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

log = logging.getLogger(__name__)

# Same set local_api._STRUCTURAL_EDGE_TYPES excludes from `sm ask` relations.
STRUCTURAL_EDGE_TYPES = frozenset(
    {"GROUNDED_IN", "CONTAINS_ENTITY", "MENTIONED_IN", "HAS_VERSION"}
)

# Guard rails on the entity hop: a hub entity can carry hundreds of edges and a
# terminal pane cannot show them. Bounded, and the bound is logged when hit.
MAX_ENTITY_HOPS = 24
MAX_RELATION_ROWS = 200


class ExploreUnavailable(RuntimeError):
    """The daemon is not reachable or not healthy. Carries a user-facing message."""


@dataclass(frozen=True)
class RelationRow:
    """One selectable adjacency row. `focus_id` is what Enter walks to."""

    type: str
    source: str
    target: str
    source_id: str
    target_id: str
    direction: str  # "outgoing" | "incoming" | "via"
    focus_id: str

    def render(self) -> str:
        marker = {"outgoing": "->", "incoming": "<-", "via": "~"}.get(
            self.direction, "-"
        )
        return f"{marker} {self.source} --{self.type}--> {self.target}"


@dataclass(frozen=True)
class FocusView:
    """A focused node plus its relation rows, grouped by relation type."""

    item_id: str
    label: str
    content: str
    memory_type: str
    node_category: str
    rows: tuple[RelationRow, ...] = ()
    raw: dict = field(default_factory=dict)

    def grouped(self) -> list[tuple[str, list[RelationRow]]]:
        """Rows grouped by relation type, types in first-seen order."""
        groups: dict[str, list[RelationRow]] = {}
        for row in self.rows:
            groups.setdefault(row.type, []).append(row)
        return list(groups.items())


def node_label(node: dict, fallback: str = "") -> str:
    """Human label for a graph node. Mirrors local_api._node_label ordering."""
    if not isinstance(node, dict):
        return fallback
    for key in ("label", "name", "content"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= 80 else text[:77] + "..."
    return fallback


class ExploreClient:
    """Sync reads plus one async SSE tail. `transport` is for tests only."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=False,
            transport=transport,
        )
        self._label_cache: dict[str, str] = {}

    # ── plumbing ────────────────────────────────────────────────────────

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ExploreClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ExploreUnavailable(
                f"SmartMemory daemon at {self.base_url} is not reachable ({exc})."
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise ExploreUnavailable(f"{method} {path} failed: {detail}")
        return response.json()

    # ── reads ───────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Raise ExploreUnavailable unless the daemon answers /health with status ok."""
        payload = self._request("GET", "/health")
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ExploreUnavailable(
                f"SmartMemory daemon at {self.base_url} answered but is not healthy: {payload!r}"
            )
        return payload

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        payload = self._request(
            "POST", "/memory/search", json={"query": query, "top_k": top_k}
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    def get_node(self, item_id: str) -> dict:
        node = self._request("GET", f"/memory/{item_id}")
        if not isinstance(node, dict):
            raise ExploreUnavailable(f"Node {item_id} returned an unexpected shape.")
        return node

    def neighbors(self, item_id: str) -> dict:
        payload = self._request("GET", f"/memory/{item_id}/neighbors")
        return payload if isinstance(payload, dict) else {"neighbors": [], "edges": []}

    def ask(self, question: str, limit: int = 5) -> dict:
        payload = self._request(
            "POST", "/memory/ask", json={"question": question, "limit": limit}
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("answer"), str):
            raise ExploreUnavailable("Daemon returned an invalid ask response.")
        return payload

    # ── labels ──────────────────────────────────────────────────────────

    def label_for(self, item_id: str) -> str:
        """Cached label lookup. A node that cannot be read degrades to its id, loudly."""
        if item_id in self._label_cache:
            return self._label_cache[item_id]
        try:
            label = node_label(self.get_node(item_id), fallback=item_id)
        except ExploreUnavailable as exc:
            log.warning(
                "explore: label lookup failed for %s (%s) — showing the raw id instead",
                item_id,
                exc,
            )
            label = item_id
        self._label_cache[item_id] = label
        return label

    # ── the composed read the TUI actually uses ─────────────────────────

    def focus(self, item_id: str) -> FocusView:
        """Node + relation rows: direct semantic edges, then the entity hop."""
        node = self.get_node(item_id)
        payload = self.neighbors(item_id)
        edges = [e for e in (payload.get("edges") or []) if isinstance(e, dict)]
        neighbors = [n for n in (payload.get("neighbors") or []) if isinstance(n, dict)]

        rows: list[RelationRow] = []
        seen: set[tuple[str, str, str]] = set()

        def add(edge: dict, direction: str) -> None:
            source_id = edge.get("source_id")
            target_id = edge.get("target_id")
            relation = edge.get("edge_type") or edge.get("link_type")
            if not (
                isinstance(source_id, str)
                and isinstance(target_id, str)
                and isinstance(relation, str)
                and relation
            ):
                return
            if relation.upper() in STRUCTURAL_EDGE_TYPES:
                return
            key = (source_id, relation, target_id)
            if key in seen or len(rows) >= MAX_RELATION_ROWS:
                if len(rows) >= MAX_RELATION_ROWS:
                    log.warning(
                        "explore: relation rows capped at %d for %s — some edges are not shown",
                        MAX_RELATION_ROWS,
                        item_id,
                    )
                return
            seen.add(key)
            if source_id == item_id:
                focus_id = target_id
            elif target_id == item_id:
                focus_id = source_id
            else:
                focus_id = target_id
            rows.append(
                RelationRow(
                    type=relation,
                    source=self.label_for(source_id),
                    target=self.label_for(target_id),
                    source_id=source_id,
                    target_id=target_id,
                    direction=direction,
                    focus_id=focus_id,
                )
            )

        for edge in edges:
            direction = "incoming" if edge.get("target_id") == item_id else "outgoing"
            add(edge, direction)

        # The entity hop — the same walk local_api._ask_relations performs, so a
        # memory item whose own edges are all structural still has a walkable
        # adjacency list instead of an empty pane.
        entity_ids = [
            n.get("item_id")
            for n in neighbors
            if str(n.get("link_type", "")).upper() in STRUCTURAL_EDGE_TYPES
            and isinstance(n.get("item_id"), str)
        ]
        deduped: list[str] = []
        for entity_id in entity_ids:
            if entity_id not in deduped and entity_id != item_id:
                deduped.append(entity_id)
        if len(deduped) > MAX_ENTITY_HOPS:
            log.warning(
                "explore: %s has %d structural neighbours; walking only the first %d",
                item_id,
                len(deduped),
                MAX_ENTITY_HOPS,
            )
            deduped = deduped[:MAX_ENTITY_HOPS]
        for entity_id in deduped:
            for edge in self.neighbors(entity_id).get("edges") or []:
                if isinstance(edge, dict):
                    add(edge, "via")

        return FocusView(
            item_id=str(node.get("item_id") or item_id),
            label=node_label(node, fallback=item_id),
            content=str(node.get("content") or ""),
            memory_type=str(node.get("memory_type") or "?"),
            node_category=str(node.get("node_category") or "?"),
            rows=tuple(rows),
            raw=node,
        )

    def resolve(self, term: str) -> Optional[str]:
        """Resolve a CLI argument to an item id: an exact id, else the top search hit."""
        try:
            node = self.get_node(term)
        except ExploreUnavailable:
            node = None
        if isinstance(node, dict) and node.get("item_id"):
            return str(node["item_id"])
        hits = self.search(term, top_k=1)
        for hit in hits:
            candidate = hit.get("item_id") or hit.get("id")
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    # ── live tail ───────────────────────────────────────────────────────

    async def stream_progress(self) -> AsyncIterator[dict]:
        """Yield one decoded SSE frame per `data:` block. Comments/keepalives skipped."""
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(None, connect=self._timeout),
            trust_env=False,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", "/memory/progress/stream") as response:
                if response.status_code >= 400:
                    raise ExploreUnavailable(
                        f"progress stream unavailable (HTTP {response.status_code})"
                    )
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    line = line.rstrip("\r")
                    if line.startswith(":"):
                        continue
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            data_lines = []
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                log.warning(
                                    "explore: dropped an unparseable SSE frame: %r", raw
                                )
                                continue
                            if isinstance(frame, dict):
                                yield frame
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())


def frame_line(frame: dict) -> str:
    """One display line per SSE frame, per the PLAT-PUSH-SSE-1 frame contract."""
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    kind = str(frame.get("kind") or "?")
    action = str(payload.get("action") or frame.get("status") or "?")
    subject = str(data.get("label") or data.get("memory_id") or "")
    seq = frame.get("seq")
    head = f"[{seq}]" if isinstance(seq, int) else ""
    return " ".join(part for part in (head, kind, action, subject) if part)


def frame_item_ids(frame: dict) -> set[str]:
    """Item ids a frame concerns, so the live pane can flash the focused node."""
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    ids = set()
    for key in ("memory_id", "item_id", "id", "source_id", "target_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            ids.add(value)
    return ids
