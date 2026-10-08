"""Real parser -> authenticated ASGI contract receiver -> disposable SQLite.

The receiver exercises transport and persistence without live credentials or
listening sockets. It is not a substitute for the service's FalkorDB tests.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from smartmemory_app.cli_code import code_group
from smartmemory_app.remote_backend import RemoteBackendError, RemoteMemory


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "test_p6_checkout"
    root.mkdir()
    (root / "a.py").write_text('def hello():\n    """Greeting."""\n    return "hi"\n')
    (root / "b.py").write_text(
        "from a import hello\ndef caller():\n    return hello()\n"
    )
    (root / "vendor").mkdir()
    (root / "vendor" / "broken.py").write_text("def (bad syntax")
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    db_path = tmp_path / "test_p6_hosted.sqlite"
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.execute(
        "CREATE TABLE nodes (workspace TEXT, repo TEXT, name TEXT, payload TEXT)"
    )
    db.execute("CREATE TABLE edges (workspace TEXT, repo TEXT, payload TEXT)")
    for workspace in ("test_p6_workspace", "test_p6_other"):
        db.execute(
            "INSERT INTO nodes VALUES (?, 'test_p6_repo', 'old', '{}')", (workspace,)
        )
    db.commit()
    state = {"calls": [], "failure": None}
    app = FastAPI()

    @app.get("/auth/me")
    def me():
        return {"default_team_id": "test_p6_workspace"}

    @app.post("/memory/code/index")
    async def index(request: Request):
        body = await request.json()
        state["calls"].append(
            (request.method, request.url.path, dict(request.headers), body)
        )
        assert request.headers["authorization"] == "Bearer test_p6_key"
        workspace = request.headers["x-workspace-id"]
        assert workspace == "test_p6_workspace"
        assert set(body) == {
            "repo",
            "entities",
            "relations",
            "commit_hash",
            "parse_summary",
            "repo_identity",  # CODE-INDEXER-HARDEN-1 F28
        }
        summary = body["parse_summary"]
        assert summary["files_failed"] == 0
        assert summary["files_partial"] == 0
        assert summary["files_clean"] == len(
            {entity["file_path"] for entity in body["entities"]}
        )
        assert summary["publication"] == "not_attempted"
        assert summary["g16_complete"] is False
        failure = state["failure"]
        if isinstance(failure, tuple):
            return JSONResponse(status_code=failure[0], content=failure[1])
        if failure == "rollback":
            # Real transaction rolls back overwritten state and a partial write.
            try:
                with db:
                    db.execute(
                        "DELETE FROM nodes WHERE workspace=? AND repo=?",
                        (workspace, body["repo"]),
                    )
                    db.execute(
                        "INSERT INTO nodes VALUES (?, ?, 'partial', '{}')",
                        (workspace, body["repo"]),
                    )
                    raise RuntimeError("injected write failure")
            except RuntimeError:
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": "replacement failed, prior index retained",
                        "replaced": False,
                    },
                )
        with db:
            db.execute(
                "DELETE FROM nodes WHERE workspace=? AND repo=?",
                (workspace, body["repo"]),
            )
            db.execute(
                "DELETE FROM edges WHERE workspace=? AND repo=?",
                (workspace, body["repo"]),
            )
            for entity in body["entities"]:
                db.execute(
                    "INSERT INTO nodes VALUES (?, ?, ?, ?)",
                    (workspace, body["repo"], entity["name"], json.dumps(entity)),
                )
            for relation in body["relations"]:
                db.execute(
                    "INSERT INTO edges VALUES (?, ?, ?)",
                    (workspace, body["repo"], json.dumps(relation)),
                )
        response = {
            "entities_created": len(body["entities"]),
            "edges_created": len(body["relations"]),
            "commit_hash": body["commit_hash"],
            "replaced": True,
        }
        if state.get("embeddings_generated") is not None:
            # A current service reports REST embeddings (CODE-INDEXER-HARDEN-1 F25).
            response["embeddings_generated"] = state["embeddings_generated"]
        if failure == "short_counts":
            response["entities_created"] -= 1
        elif failure == "unconfirmed":
            response["replaced"] = False
        elif failure == "invalid":
            response = {"unexpected": True}
        elif failure == "cleanup":
            return JSONResponse(
                status_code=500,
                content={"detail": "replacement cleanup failed", "replaced": False},
            )
        return response

    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_p6_key")
    monkeypatch.setenv("SMARTMEMORY_DISABLE_LAUNCH_METRICS", "1")
    with TestClient(app) as client:

        def request(method, url, **kwargs):
            # Recent Starlette uses httpx2. Bridge the ASGI bytes into the
            # wrapper's httpx response type so its real status exceptions fire.
            kwargs.pop("timeout", None)
            response = client.request(method, url, **kwargs)
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers=dict(response.headers),
                request=httpx.Request(method, url),
            )

        monkeypatch.setattr(
            httpx, "get", lambda url, **kwargs: request("GET", url, **kwargs)
        )
        monkeypatch.setattr(httpx, "request", request)
        remote = RemoteMemory(api_url="http://testserver", team_id="test_p6_workspace")
        try:
            yield remote, db, state
        finally:
            db.close()
            db_path.unlink()


def invoke(checkout, hosted, monkeypatch, *flags):
    from smartmemory_app import storage

    monkeypatch.setattr(storage, "get_memory", lambda: hosted[0])
    return CliRunner().invoke(
        code_group,
        [
            "index",
            str(checkout),
            "--repo",
            "test_p6_repo",
            "--exclude",
            "vendor",
            "--commit-hash",
            "test_sha",
            *flags,
        ],
    )


def test_hosted_cli_persists_entities_and_resolved_cross_file_edges(
    checkout, hosted, monkeypatch, caplog
):
    remote, db, state = hosted
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "phase=done" in result.output
    assert "files=2" in result.output
    assert "embeddings=0" in result.output
    assert "does not generate vector embeddings" in caplog.text
    method, path, headers, body = state["calls"][0]
    assert (method, path) == ("POST", "/memory/code/index")
    assert len(state["calls"]) == 1
    assert body["commit_hash"] == "test_sha"
    assert all(
        not Path(entity["file_path"]).is_absolute() for entity in body["entities"]
    )
    assert str(checkout) not in json.dumps(body)
    assert {entity["name"] for entity in body["entities"]} >= {"hello", "caller"}
    assert any(
        rel["relation_type"] == "CALLS" and "a.py::hello" in rel["target_id"]
        for rel in body["relations"]
    )
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM nodes WHERE workspace='test_p6_workspace'"
        )
    }
    assert names >= {"hello", "caller"} and "old" not in names
    assert db.execute(
        "SELECT name FROM nodes WHERE workspace='test_p6_other'"
    ).fetchall() == [("old",)]
    assert db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == len(
        body["relations"]
    )


@pytest.mark.parametrize(
    "status, body, message",
    [
        (
            402,
            {"detail": {"error": "repo_quota_exceeded", "limit": 1, "current": 1}},
            "max_repos",
        ),
        (
            413,
            {"error": "request_body_too_large", "max_bytes": 512},
            "MAX_REQUEST_BODY_BYTES",
        ),
        (401, {"detail": "expired"}, "invalid or expired"),
        (403, {"detail": "wrong workspace"}, "wrong workspace"),
        (422, {"detail": "invalid entity"}, "invalid entity"),
    ],
)
def test_server_refusals_retain_prior_index(
    checkout, hosted, monkeypatch, status, body, message
):
    hosted[2]["failure"] = (status, body)
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert message in result.output
    assert "phase=done" not in result.output
    assert len(hosted[2]["calls"]) == 1
    assert hosted[1].execute(
        "SELECT name FROM nodes WHERE workspace='test_p6_workspace'"
    ).fetchall() == [("old",)]
    if status == 413:
        assert "512" in result.output
    if status == 402:
        assert '"limit": 1' in result.output


def test_replacement_failure_rolls_back_and_is_not_retried(
    checkout, hosted, monkeypatch
):
    hosted[2]["failure"] = "rollback"
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "prior index retained" in result.output
    assert "phase=done" not in result.output
    assert len(hosted[2]["calls"]) == 1
    assert hosted[1].execute(
        "SELECT name FROM nodes WHERE workspace='test_p6_workspace'"
    ).fetchall() == [("old",)]


@pytest.mark.parametrize(
    "failure", ["short_counts", "unconfirmed", "invalid", "cleanup"]
)
def test_partial_or_unconfirmed_server_writes_fail_explicitly(
    checkout, hosted, monkeypatch, failure
):
    hosted[2]["failure"] = failure
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "phase=done" not in result.output
    assert "partial server changes may exist" in result.output.lower()
    assert len(hosted[2]["calls"]) == 1


def test_parse_error_refuses_without_upload(checkout, hosted, monkeypatch, caplog):
    (checkout / "bad.py").write_text("def (invalid")
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "parse error" in result.output
    assert "replacement refused" in caplog.text
    assert hosted[2]["calls"] == []


@pytest.mark.parametrize(
    "languages",
    [("python",), ("typescript",), ("python", "typescript")],
    ids=["python", "typescript", "both"],
)
def test_unreadable_directory_refuses_and_preserves_prior_index(
    checkout, hosted, monkeypatch, caplog, languages
):
    blocked = checkout / "src"
    blocked.mkdir()
    (blocked / "important.py").write_text("def important(): pass\n")
    initial = invoke(checkout, hosted, monkeypatch)
    assert initial.exit_code == 0, initial.output
    db = hosted[1]
    prior_nodes = db.execute("SELECT * FROM nodes ORDER BY workspace, name").fetchall()
    prior_edges = db.execute("SELECT * FROM edges ORDER BY payload").fetchall()
    assert any(
        json.loads(row[3]).get("file_path") == "src/important.py" for row in prior_nodes
    )
    permissions = blocked.stat().st_mode
    try:
        blocked.chmod(0)
        # Exercise actual filesystem denial, rather than a patched collector.
        with pytest.raises(PermissionError):
            list(blocked.iterdir())
        flags = [flag for language in languages for flag in ("--language", language)]
        result = invoke(checkout, hosted, monkeypatch, *flags)
        assert result.exit_code == 1, result.output
        assert "phase=done" not in result.output
        assert "incomplete directory traversal" in result.output
        assert str(blocked) in result.output
        assert "Prior index was not changed" in result.output
        assert "replacement refused" in caplog.text
        assert len(hosted[2]["calls"]) == 1
        assert (
            db.execute("SELECT * FROM nodes ORDER BY workspace, name").fetchall()
            == prior_nodes
        )
        assert (
            db.execute("SELECT * FROM edges ORDER BY payload").fetchall() == prior_edges
        )
    finally:
        blocked.chmod(permissions)


def test_empty_checkout_refuses_without_upload(tmp_path, hosted, monkeypatch):
    result = invoke(tmp_path, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "0 entities" in result.output
    assert hosted[2]["calls"] == []


def test_default_body_cap_refuses_whole_request(checkout, hosted, monkeypatch):
    from smartmemory_app import hosted_code

    monkeypatch.setattr(hosted_code, "MAX_REQUEST_BODY_BYTES", 100)
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "MAX_REQUEST_BODY_BYTES=100" in result.output
    assert "cannot safely accept chunks" in result.output
    assert hosted[2]["calls"] == []


def test_commit_suppression_and_no_store_opened(checkout, hosted, monkeypatch):
    from smartmemory_app import storage

    def no_store(*args, **kwargs):
        pytest.fail("hosted parser opened a local memory store")

    monkeypatch.setattr(storage, "_get_local_memory", no_store)
    result = hosted[0].ingest_code(
        str(checkout), "test_p6_repo", commit_hash="", exclude_dirs=["vendor"]
    )
    assert result.replaced is True
    assert result.commit_hash == ""
    assert hosted[2]["calls"][0][3]["commit_hash"] == ""


def test_typescript_parser_and_exclusions(checkout, hosted, monkeypatch):
    pytest.importorskip("tree_sitter")
    (checkout / "component.ts").write_text(
        "export function greet(name: string) { return name; }\n"
    )
    result = invoke(checkout, hosted, monkeypatch, "--language", "typescript")
    assert result.exit_code == 0, result.output
    entities = hosted[2]["calls"][0][3]["entities"]
    assert {entity["file_path"] for entity in entities} == {"component.ts"}
    assert any(entity["name"] == "greet" for entity in entities)


def test_missing_workspace_refuses_before_upload(checkout, hosted):
    hosted[0]._team_id = ""
    with pytest.raises(RemoteBackendError, match="requires a configured workspace"):
        hosted[0].ingest_code(str(checkout), "test_p6_repo")
    assert hosted[2]["calls"] == []


def test_unsupported_language_refuses_before_upload(checkout, hosted):
    with pytest.raises(ValueError, match="only python and typescript"):
        hosted[0].ingest_code(str(checkout), "test_p6_repo", languages=["ruby"])
    assert hosted[2]["calls"] == []


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_is_explicit_and_not_replayed(
    checkout, hosted, monkeypatch, error
):
    def fail_request(*args, **kwargs):
        raise error("injected transport failure")

    monkeypatch.setattr(httpx, "request", fail_request)
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 1
    assert "phase=done" not in result.output
    assert "partial server changes may exist" in result.output
    assert hosted[2]["calls"] == []


def test_symlink_outside_checkout_refuses_before_upload(
    checkout, hosted, monkeypatch, tmp_path
):
    outside = tmp_path / "outside.py"
    outside.write_text("def secret(): pass\n")
    try:
        (checkout / "escape.py").symlink_to(outside)
        result = invoke(checkout, hosted, monkeypatch)
        assert result.exit_code == 1
        assert hosted[2]["calls"] == []
    finally:
        outside.unlink()


def test_commit_autodetection_and_warning_when_unavailable(checkout, hosted, caplog):
    result = hosted[0].ingest_code(
        str(checkout), "test_p6_repo", exclude_dirs=["vendor"]
    )
    assert result.commit_hash == ""
    assert "no commit hash" in caplog.text


def test_missing_api_key_refuses_before_upload(checkout, hosted):
    hosted[0]._access_token = ""
    with pytest.raises(RemoteBackendError, match="requires a SmartMemory API key"):
        hosted[0].ingest_code(str(checkout), "test_p6_repo")
    assert hosted[2]["calls"] == []


# --------------------------------------------------------------------------- CODE-INDEXER-HARDEN-1 U5


def _git(root, *args):
    import subprocess

    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test_p6",
            "GIT_AUTHOR_EMAIL": "test_p6@example.invalid",
            "GIT_COMMITTER_NAME": "test_p6",
            "GIT_COMMITTER_EMAIL": "test_p6@example.invalid",
        },
    )


def test_upload_carries_path_identity_without_leaking_the_path(checkout, hosted):
    hosted[0].ingest_code(str(checkout), "test_p6_repo", exclude_dirs=["vendor"])
    body = hosted[2]["calls"][0][3]
    assert body["repo_identity"].startswith("path:")
    assert len(body["repo_identity"]) == len("path:") + 64
    assert str(checkout) not in json.dumps(body)


def test_dirty_checkout_uploads_dirty_provenance_and_remote_identity(
    checkout, hosted, caplog
):
    _git(checkout, "init", "-q")
    _git(checkout, "add", "a.py", "b.py")
    _git(checkout, "commit", "-q", "-m", "init")
    _git(checkout, "remote", "add", "origin", "https://user:tok@GitHub.com/acme/p6.git")
    import subprocess

    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    clean = hosted[0].ingest_code(
        str(checkout), "test_p6_repo", exclude_dirs=["vendor"]
    )
    # vendor/ is untracked, but it is inside the checkout, so the tree is dirty.
    assert clean.commit_hash.startswith(f"{head}-dirty-")
    (checkout / "vendor" / "broken.py").unlink()
    (checkout / "vendor").rmdir()
    assert hosted[0].ingest_code(str(checkout), "test_p6_repo").commit_hash == head
    (checkout / "a.py").write_text('def hello():\n    return "edited"\n')
    dirty = hosted[0].ingest_code(str(checkout), "test_p6_repo")
    body = hosted[2]["calls"][-1][3]
    assert body["commit_hash"] == dirty.commit_hash
    assert dirty.commit_hash.startswith(f"{head}-dirty-")
    assert dirty.commit_hash != clean.commit_hash
    assert body["repo_identity"] == "remote:github.com/acme/p6"
    assert "includes uncommitted changes" in caplog.text


def test_reported_server_embeddings_replace_the_no_embeddings_warning(
    checkout, hosted, monkeypatch, caplog
):
    hosted[2]["embeddings_generated"] = 4  # modules a, b plus hello and caller
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "entities=4" in result.output and "embeddings=4" in result.output
    assert "does not generate vector embeddings" not in caplog.text
    assert "coverage is partial" not in caplog.text

    hosted[2]["embeddings_generated"] = 1
    caplog.clear()
    result = invoke(checkout, hosted, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "embedded 1 of 4 entities" in caplog.text


def test_local_filesystem_remote_never_leaves_the_machine(tmp_path, hosted):
    """Fix round 1 (finding 7): a clone of a local bare repo uploads only hashes."""
    import subprocess

    bare = tmp_path / "test_p6_origin" / "p6.git"
    bare.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "test_p6_clone"
    work.mkdir()
    (work / "a.py").write_text("def hello():\n    return 1\n")
    _git(work, "init", "-q")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", f"file://{bare}")
    hosted[0].ingest_code(str(work), "test_p6_repo")
    body = hosted[2]["calls"][-1][3]
    encoded = json.dumps(body)
    assert body["repo_identity"].startswith("path:")
    assert (
        "/Users/" not in encoded
        and "file/" not in encoded
        and str(tmp_path) not in encoded
    )
