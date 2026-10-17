"""Exercise Click parsing before any transport or positional selection."""

import pytest
from click.testing import CliRunner

from smartmemory_app.cli import add_cmd, cli, _parse_extra_props


@pytest.mark.parametrize(
    "args",
    [
        ["--project", "atlas", "whole text"],
        ["--project=atlas", "whole text"],
        ["whole text", "--project=atlas"],
        ["whole text", "--project", "atlas"],
        ["--type=semantic", "--project", "atlas", "whole text"],
    ],
)
def test_property_normalized_before_text(args):
    with add_cmd.make_context("add", args) as context:
        assert context.params["text"] == "whole text"
        assert _parse_extra_props(context.args, context.params["explicit_props"]) == {
            "project": "atlas"
        }


def test_terminator_keeps_option_like_text():
    with add_cmd.make_context(
        "add", ["--project=atlas", "--", "--literal text"]
    ) as context:
        assert context.params["text"] == "--literal text"


@pytest.mark.parametrize(
    "args, message",
    [
        (["some", "unquoted", "words"], "Quote the text or use --all -"),
        (["text", "--project"], "requires a value"),
        (["text", "--project", "--all"], "requires a value"),
        (["text", "--project="], "non-empty"),
        (["text", "--project=a", "--prop", "project=b"], "duplicate property"),
        (["text", "--origin=hook:observe"], "Reserved properties"),
    ],
)
def test_invalid_input_refused_before_transport(args, message):
    result = CliRunner().invoke(cli, ["add", *args])
    assert result.exit_code != 0
    assert message in result.output


def test_remote_add_properties_reach_real_http_and_persist(tmp_path, monkeypatch):
    import json
    import sqlite3
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from smartmemory_app import storage

    monkeypatch.setattr(storage, "_remote_memory", None)
    database = tmp_path / "test_F1_remote.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE requests(body TEXT)")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            assert self.path == "/memory/ingest"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO requests VALUES (?)", (body.decode(),))
            self.send_response(200)
            response = b'{"item_id":"test_F1_remote_id"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "test_F1_offline_key")
    try:
        for args in (
            ["--project", "F1", "whole remote text"],
            ["whole remote text", "--project=F1"],
        ):
            result = CliRunner().invoke(cli, ["add", *args])
            assert result.exit_code == 0 and "test_F1_remote_id" in result.output, (
                result.output
            )
        with sqlite3.connect(database) as db:
            rows = [
                json.loads(row[0]) for row in db.execute("SELECT body FROM requests")
            ]
        assert len(rows) == 2
        assert all(
            row["content"] == "whole remote text" and row["context"]["project"] == "F1"
            for row in rows
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
