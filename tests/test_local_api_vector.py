"""Lite parity for the vector tab reads (WEB-VECTOR-SEARCH-TAB-1 unit 3).

The four `/memory/vector/*` routes of the Lite daemon are driven over a REAL USearch store (no mocks of
the store, the manager or the routes) and checked against `vector-contract.json`, the shape the hosted
service returns. Rows are explicit tables, one per surface. `smartmemory.stores.vector` is the system
under test through the core's own `VectorInspectManager`; only the memory node reader and the embedder
are stubbed, because the daemon's spaCy and embedding models are not needed to prove the wiring.

Also pins that `store_diagnostics` (the CLI doctor) reads the same facts as `/vector/stats`: its
read-only snapshot `describe()` equals the live backend's `describe()`.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartmemory.managers.vector_inspect import (
    VectorCapabilityUnsupported,
    VectorInspectManager,
    VectorNotFound,
    VectorRequestError,
)
from smartmemory.models.memory_item import MemoryItem
from smartmemory.scope_provider import DefaultScopeProvider
from smartmemory.stores.vector.backends.usearch import UsearchVectorBackend
from smartmemory.stores.vector.errors import VectorSubstrateError
from smartmemory.stores.vector.vector_store import VectorStore, _vector_backend_ctx

import smartmemory_app.local_api as local_api

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "vector-contract.json").read_text()
)
TYPES, ROUTES = CONTRACT["types"], CONTRACT["routes"]
DIM = 8


def _vec(seed: int) -> list[float]:
    return [float(((seed * 7 + i * 3) % 11) + 1) for i in range(DIM)]


# ---------- contract shape helpers ----------
def _assert_shape(obj: dict, spec: dict, where: str) -> None:
    optional = {k for k, v in spec.items() if isinstance(v, str) and "| absent" in v}
    required = set(spec) - optional
    assert required <= set(obj), f"{where}: missing {sorted(required - set(obj))}"
    assert set(obj) <= required | optional, (
        f"{where}: extra {sorted(set(obj) - required - optional)}"
    )


def assert_query(resp: dict) -> None:
    spec = ROUTES["query"]["response"]
    _assert_shape(resp, spec, "query")
    _assert_shape(resp["query"], spec["query"], "query.query")
    _assert_shape(resp["timing_ms"], spec["timing_ms"], "query.timing_ms")
    _assert_shape(resp["query"]["space"], TYPES["SpaceRef"], "query.space")
    scores = [h["score"] for h in resp["hits"]]
    assert scores == sorted(scores, reverse=True)
    for hit in resp["hits"]:
        _assert_shape(hit, TYPES["VectorHit"], "query.hit")
        _assert_shape(hit["space"], TYPES["SpaceRef"], "query.hit.space")
        assert 0.0 <= hit["score"] <= 1.0


def assert_stats(resp: dict) -> None:
    spec = ROUTES["stats"]["response"]
    _assert_shape(resp, spec, "stats")
    for key in ("capabilities", "embedder", "scope"):
        _assert_shape(resp[key], spec[key], f"stats.{key}")
    for coll in resp["collections"]:
        _assert_shape(coll, TYPES["CollectionStats"], "stats.collection")
        _assert_shape(coll["space"], TYPES["SpaceRef"], "stats.collection.space")


def assert_inspect(resp: dict) -> None:
    _assert_shape(resp, ROUTES["inspect"]["response"], "inspect")
    _assert_shape(resp["space"], TYPES["SpaceRef"], "inspect.space")
    _assert_shape(resp["item"], TYPES["VectorItem"], "inspect.item")
    _assert_shape(
        resp["item"]["vector"], TYPES["VectorItem"]["vector"], "inspect.item.vector"
    )
    for hit in resp["neighbours"]:
        _assert_shape(hit, TYPES["VectorHit"], "inspect.neighbour")


def assert_missing(resp: dict) -> None:
    _assert_shape(resp, ROUTES["missing"]["response"], "missing")
    for item in resp["items"]:
        _assert_shape(item, TYPES["MissingItem"], "missing.item")


def assert_projection(resp: dict) -> None:
    _assert_shape(resp, TYPES["ProjectionResponse"], "projection")
    _assert_shape(resp["space"], TYPES["SpaceRef"], "projection.space")
    for point in resp["points"]:
        _assert_shape(point, TYPES["ProjectionPoint"], "projection.point")
        assert len(point["coords"]) == resp["dims"]
        assert all(-1.0 <= c <= 1.0 for c in point["coords"])


SHAPES = {
    "projection": assert_projection,
    "query": assert_query,
    "stats": assert_stats,
    "inspect": assert_inspect,
    "missing": assert_missing,
}


# ---------- a real Lite vector world ----------
class World:
    """One USearch collection under `tmp_path` ("memory.fts.db", as Lite names it) behind the real manager."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.backend = UsearchVectorBackend(
            collection_name="memory", persist_directory=str(data_dir)
        )
        self.scope = DefaultScopeProvider(user_id="u", workspace_id="ws")
        self.nodes: dict[str, MemoryItem] = {}
        self.token = _vector_backend_ctx.set(self.backend)
        self.manager = VectorInspectManager(
            vector_store_factory=self._store_for,
            embed_fn=lambda text: _vec(len(text)),
            embedder_info=lambda: {"model_id": "test:fixed", "dimension": DIM},
            node_reader=lambda ids: {i: self.nodes[i] for i in ids if i in self.nodes},
            scope_info=lambda: {
                "workspace_id": "ws",
                "user_id": "u",
                "mode": "workspace",
            },
            type_is_searchable=lambda t: t not in ("pending", "metadata", "relation"),
        )

    def _store_for(self, collection: str) -> VectorStore:
        if collection != "memory":
            raise VectorNotFound("unknown_collection")
        return VectorStore(scope_provider=self.scope)

    def write(self, seed: int, memory_type: str = "semantic") -> str:
        item_id = f"item-{seed:03d}"
        VectorStore(scope_provider=self.scope).add(
            item_id, _vec(seed), metadata={"memory_type": memory_type}
        )
        self.nodes[item_id] = MemoryItem(
            item_id=item_id, content=f"note {item_id}", memory_type=memory_type
        )
        return item_id

    def drop_vector(self, item_id: str) -> None:
        """Leave the row the way a crashed write does: membership kept, index key gone."""
        key = self.backend._fts_conn.execute(
            f'SELECT key FROM "{self.backend._meta_table}" WHERE item_id = ?',
            (item_id,),
        ).fetchone()[0]
        self.backend._index.remove(key)

    # The facade surface the routes call (the core facade's vector_* are thin delegates to these).
    def __getattr__(self, name):
        if name.startswith("vector_"):
            return getattr(self.manager, name)
        raise AttributeError(name)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "data")
    try:
        yield w
    finally:
        _vector_backend_ctx.reset(w.token)
        w.backend.close()


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setattr(local_api, "_get_mem", lambda: world)
    app = FastAPI()
    app.mount("/memory", local_api.api)  # as viewer_server mounts it
    return TestClient(app)


@pytest.fixture
def seeded(world):
    ids = [world.write(s) for s in (1, 2, 3, 4)]
    world.write(5, memory_type="pending")  # not searchable: stays a row, still counted
    world.drop_vector(ids[3])
    return ids


# ---------- routes vs the contract ----------
def test_lite_aliases_are_the_contract_paths_under_the_memory_mount():
    for name, route in ROUTES.items():
        method, alias = route["lite_alias"].split(" ", 1)
        assert route["method"] == method
        assert route["path"] == "/memory" + alias, name


# One row per route: (route, method, path, request kwargs).
OK_ROWS = [
    (
        "query",
        "post",
        "/memory/vector/query",
        {"json": {"text": "hello", "include_embedding": True}},
    ),
    (
        "query",
        "post",
        "/memory/vector/query",
        {"json": {"embedding": _vec(2), "top_k": 2}},
    ),
    ("stats", "get", "/memory/vector/stats", {}),
    (
        "inspect",
        "get",
        "/memory/vector/items/item-001",
        {"params": {"top_k": 3, "include_embedding": "true"}},
    ),
    ("missing", "get", "/memory/vector/missing", {"params": {"limit": 10}}),
]


@pytest.mark.parametrize(
    "route,method,path,kwargs",
    OK_ROWS,
    ids=[f"{r[0]}-{i}" for i, r in enumerate(OK_ROWS)],
)
def test_route_returns_the_contract_shape(client, seeded, route, method, path, kwargs):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 200, resp.text
    SHAPES[route](resp.json())


def test_route_bodies_are_what_the_core_manager_returns(client, world, seeded):
    """Same keys AND values as the facade: the daemon adds no second shaping layer."""
    stats = client.get("/memory/vector/stats").json()
    expected = world.manager.vector_stats()
    assert stats == expected
    assert client.get("/memory/vector/missing").json() == world.manager.vector_missing()
    inspect = client.get("/memory/vector/items/item-001").json()
    assert inspect == world.manager.vector_inspect("item-001")


def test_lite_facts_on_a_usearch_store(client, world, seeded):
    stats = client.get("/memory/vector/stats").json()
    coll = stats["collections"][0]
    assert stats["backend"] == "usearch" and stats["single_tenant"] is True
    assert stats["capabilities"]["missing_list"] is True
    assert (
        coll["count"] == 5 and coll["index_count"] == 5
    )  # collection-wide total only because single_tenant
    assert coll["space"]["dimension"] == DIM and stats["embedder"] == {
        "model_id": "test:fixed",
        "dimension": DIM,
    }
    missing = client.get("/memory/vector/missing").json()
    assert [i["item_id"] for i in missing["items"]] == [seeded[3]] and missing[
        "total"
    ] == 1
    assert coll["missing_count"] == 1
    inspect = client.get(f"/memory/vector/items/{seeded[0]}").json()
    assert seeded[0] not in [h["item_id"] for h in inspect["neighbours"]]


def test_missing_list_paginates_with_the_cursor(client, world):
    ids = [world.write(s) for s in range(1, 6)]
    for item_id in ids:
        world.drop_vector(item_id)
    first = client.get("/memory/vector/missing", params={"limit": 2}).json()
    second = client.get(
        "/memory/vector/missing", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert [i["item_id"] for i in first["items"] + second["items"]] == ids[:4]
    assert first["next_cursor"] and second["next_cursor"] and first["total"] == 5


# ---------- error mapping ----------
# (request, status, detail). detail is the contract token for a 404 and the message for a 400.
ERROR_ROWS = [
    (
        "post",
        "/memory/vector/query",
        {"json": {}},
        400,
        "exactly one of text / embedding",
    ),
    (
        "post",
        "/memory/vector/query",
        {"json": {"text": "a", "embedding": _vec(1)}},
        400,
        "exactly one of text / embedding",
    ),
    (
        "post",
        "/memory/vector/query",
        {"json": {"embedding": [0.1] * (DIM + 1)}},
        400,
        f"{DIM + 1}",
    ),
    (
        "post",
        "/memory/vector/query",
        {"json": {"text": "a", "collection": "nope"}},
        404,
        "unknown_collection",
    ),
    ("get", "/memory/vector/items/never-written", {}, 404, "not_found"),
    (
        "get",
        "/memory/vector/items/item-005",
        {},
        200,
        None,
    ),  # pending-type row still has a vector
    (
        "get",
        "/memory/vector/items/item-004",
        {},
        404,
        "no_vector",
    ),  # membership row, index key gone
    (
        "get",
        "/memory/vector/missing",
        {"params": {"collection": "nope"}},
        404,
        "unknown_collection",
    ),
]


@pytest.mark.parametrize(
    "method,path,kwargs,status,detail",
    ERROR_ROWS,
    ids=[f"{r[1].split('/vector/')[1]}-{i}" for i, r in enumerate(ERROR_ROWS)],
)
def test_errors_map_to_the_contract_statuses(
    client, seeded, method, path, kwargs, status, detail
):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == status, resp.text
    if detail:
        assert detail in resp.json()["detail"]


class _Raising:
    """A facade whose every vector read raises one typed error (the manager's contract for each status)."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def __getattr__(self, name):
        if not name.startswith("vector_"):
            raise AttributeError(name)

        def fail(*args, **kwargs):
            raise self.exc

        return fail


# (exception the core raises, HTTP status the route answers). Never an empty 200.
RAISE_ROWS = [
    (VectorRequestError("bad request"), 400),
    (VectorNotFound("no_vector"), 404),
    (
        VectorCapabilityUnsupported(
            "backend 'x' does not list memories with no vector"
        ),
        501,
    ),
    (VectorSubstrateError("index unreadable"), 503),
]


@pytest.mark.parametrize(
    "exc,status", RAISE_ROWS, ids=[type(r[0]).__name__ for r in RAISE_ROWS]
)
def test_typed_core_errors_never_become_an_empty_page(monkeypatch, exc, status):
    monkeypatch.setattr(local_api, "_get_mem", lambda: _Raising(exc))
    app = FastAPI()
    app.mount("/memory", local_api.api)
    client = TestClient(app, raise_server_exceptions=False)
    for method, path, kwargs in (
        ("post", "/memory/vector/query", {"json": {"text": "a"}}),
        ("get", "/memory/vector/stats", {}),
        ("get", "/memory/vector/items/x", {}),
        ("get", "/memory/vector/missing", {}),
        ("get", "/memory/vector/projection", {}),
    ):
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == status, (path, resp.text)
        body = resp.json()
        assert "items" not in body and "hits" not in body and "points" not in body


def test_remote_proxy_mode_answers_501(monkeypatch):
    from smartmemory_app.remote_backend import RemoteMemory

    remote = RemoteMemory.__new__(
        RemoteMemory
    )  # no network: the route only type-checks it
    monkeypatch.setattr(local_api, "_get_mem", lambda: remote)
    app = FastAPI()
    app.mount("/memory", local_api.api)
    resp = TestClient(app).get("/memory/vector/stats")
    assert resp.status_code == 501 and "remote-proxy" in resp.json()["detail"]


# ---------- VEC-TAB-U6: /vector/projection ----------
_HAS_PROJECTION = hasattr(VectorInspectManager, "vector_projection")
needs_projection = pytest.mark.skipif(
    not _HAS_PROJECTION,
    reason="installed smartmemory-core predates VEC-TAB-U6 vector_projection; runs once core ships it",
)

# One row per case: (name, query params, status, detail fragment or the dims every point carries).
PROJECTION_ROWS = [
    ("default-3d", {}, 200, 3),
    ("2d", {"dims": "2"}, 200, 2),
    ("limit-1-samples", {"limit": "1"}, 200, 3),
    ("dims-4", {"dims": "4"}, 400, "dims"),
    ("dims-not-int", {"dims": "x"}, 400, "dims"),
    ("limit-not-int", {"limit": "many"}, 400, "integer"),
    ("method-unknown", {"method": "tsne"}, 400, "method"),
    ("umap-not-enabled", {"method": "umap"}, 501, "umap"),
    ("unknown-collection", {"collection": "nope"}, 404, "unknown_collection"),
]


@needs_projection
@pytest.mark.parametrize(
    "name,params,status,expect", PROJECTION_ROWS, ids=[r[0] for r in PROJECTION_ROWS]
)
def test_projection_route_on_a_usearch_store(
    client, world, seeded, monkeypatch, name, params, status, expect
):
    monkeypatch.delenv("SMARTMEMORY_VECTOR_PROJECTION_UMAP", raising=False)
    resp = client.get("/memory/vector/projection", params=params)
    assert resp.status_code == status, resp.text
    if status != 200:
        assert expect in str(resp.json()["detail"]).lower()
        return
    body = resp.json()
    assert_projection(body)
    assert body["dims"] == expect
    assert "embedding" not in json.dumps(body)  # coordinates and ids only
    ids = [p["item_id"] for p in body["points"]]
    assert ids == sorted(ids) and set(ids) <= set(seeded) | {"item-005"}
    if name == "limit-1-samples":
        assert body["sampled"] is True and len(ids) == 1 and body["limit"] == 1
    # Deterministic and the facade's own body: a second read returns the same points (from the cache).
    again = client.get("/memory/vector/projection", params=params).json()
    assert again["points"] == body["points"] and again["cached"] is True
    direct = world.manager.vector_projection(
        **{k: v for k, v in params.items() if k != "collection"}
    )
    assert direct["points"] == body["points"]


def test_projection_on_a_core_without_the_facade_answers_501(monkeypatch, caplog):
    class _OldCore:  # an installed smartmemory-core from before VEC-TAB-U6
        def vector_stats(self):
            return {}

    monkeypatch.setattr(local_api, "_get_mem", lambda: _OldCore())
    app = FastAPI()
    app.mount("/memory", local_api.api)
    resp = TestClient(app).get("/memory/vector/projection")
    assert resp.status_code == 501
    assert "vector_projection" in resp.json()["detail"]
    assert any(
        r.levelname == "WARNING" and "vector_projection unavailable" in r.getMessage()
        for r in caplog.records
    )


# ---------- doctor and /vector/stats agree ----------
def _write_graph_nodes(data_dir: Path, items: int) -> None:
    import sqlite3

    with sqlite3.connect(data_dir / "memory.db") as db:
        db.execute(
            "CREATE TABLE nodes(item_id TEXT, memory_type TEXT, properties TEXT)"
        )
        db.executemany(
            "INSERT INTO nodes VALUES (?, 'semantic', '{\"node_category\":\"memory\"}')",
            [(str(i),) for i in range(items)],
        )


def test_doctor_snapshot_describe_equals_the_live_backend_describe(
    client, world, seeded
):
    from smartmemory_app.store_diagnostics import describe_snapshot

    world.backend.flush()
    import sqlite3

    db = sqlite3.connect(f"{world.data_dir / 'memory.fts.db'}")
    state = dict(db.execute(f'SELECT k, v FROM "{world.backend._state_table}"'))
    membership = db.execute(
        f'SELECT count(*) FROM "{world.backend._meta_table}"'
    ).fetchone()[0]
    db.close()
    assert describe_snapshot(state, membership) == world.backend.describe()


def test_doctor_rows_and_stats_agree_on_dimension_and_count(client, world, tmp_path):
    from smartmemory_app.store_diagnostics import vector_rows

    for seed in (1, 2, 3):
        world.write(seed)
    world.backend.flush()
    _write_graph_nodes(world.data_dir, items=3)
    stats = client.get("/memory/vector/stats").json()
    coll = stats["collections"][0]
    rows = "\n".join(vector_rows(world.data_dir, DIM))
    assert f"Vector dimension: OK ({coll['space']['dimension']})" in rows
    assert f"Vector count: OK ({coll['index_count']} vectors vs 3 items)" in rows
