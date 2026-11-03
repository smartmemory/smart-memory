"""CONN-LOCAL-FS-1: `sm folder sync` over a fake daemon (real folder, real checkpoint file)."""

import pytest
from click.testing import CliRunner

pytest.importorskip("smartmemory_connectors.local_runner")

from smartmemory_app import cli as cli_mod  # noqa: E402
from smartmemory_app.folder_sync import FolderSyncError, state_path, sync_folder  # noqa: E402


class FakeDaemon:
    """Stands in for the daemon's /ingest and DELETE /{id}. ``down`` mimics an unreachable daemon (None)."""

    def __init__(self):
        self.items, self.calls, self.down, self.n = {}, [], False, 0

    def __call__(self, method, path, **kw):
        self.calls.append((method, path))
        if self.down:
            return None
        if method == "POST" and path == "/ingest":
            self.n += 1
            self.items[f"m{self.n}"] = kw["json"]
            return {"item_id": f"m{self.n}"}
        if method == "DELETE":
            import click

            if self.items.pop(path[1:], None) is None:
                raise click.ClickException("Memory not found")
            return {}
        raise AssertionError((method, path))


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    return root, FakeDaemon(), tmp_path / "state.json"


def run(env, **kw):
    root, daemon, state = env
    return sync_folder(
        str(root),
        preset="markdown",
        request=daemon,
        state_file=state,
        extractors={},
        **kw,
    )


def test_create_unchanged_replace_and_remove(env):
    root, daemon, _ = env
    (root / "a.md").write_text("alpha\n")
    (root / "b.md").write_text("beta\n")
    assert run(env, reconcile_every=0).created == 2 and len(daemon.items) == 2
    posts = sum(1 for c in daemon.calls if c[0] == "POST")
    assert (
        run(env, reconcile_every=0).unchanged == 2
        and sum(1 for c in daemon.calls if c[0] == "POST") == posts
    )
    (root / "a.md").write_text("alpha changed\n")
    r = run(env, reconcile_every=0)
    assert r.replaced == 1 and len(daemon.items) == 2
    assert ("DELETE", "/m1") in daemon.calls
    (root / "b.md").unlink()
    assert run(env, reconcile_every=0).archived == 0
    assert run(env, reconcile_every=0).archived == 1 and len(daemon.items) == 1


def test_items_carry_the_local_fs_origin_and_source_path(env):
    root, daemon, _ = env
    (root / "a.md").write_text("alpha\n")
    run(env)
    item = next(iter(daemon.items.values()))
    assert item["context"] == {"origin": "import:live:local_fs"}
    assert item["properties"]["source_path"].startswith("local_fs:files:")


def test_a_daemon_that_is_down_fails_records_not_the_run(env):
    root, daemon, _ = env
    (root / "a.md").write_text("alpha\n")
    daemon.down = True
    r = run(env)
    assert r.failed == 1 and r.created == 0
    daemon.down = False
    assert run(env).created == 1


def test_fixed_text_errors(env, tmp_path):
    with pytest.raises(FolderSyncError, match="does not exist"):
        sync_folder(str(tmp_path / "nope"), preset="markdown", request=FakeDaemon())
    with pytest.raises(FolderSyncError, match="Unknown preset"):
        sync_folder(str(env[0]), preset="x", request=FakeDaemon())


def test_state_path_is_per_root_and_inside_the_data_dir(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert state_path(str(a), str(tmp_path)) != state_path(str(b), str(tmp_path))
    assert state_path(str(a), str(tmp_path)).parent == tmp_path / "folders"


def test_cli_prints_a_summary_and_exits_nonzero_on_failures(env, monkeypatch):
    root, daemon, _ = env
    (root / "a.md").write_text("alpha\n")
    monkeypatch.setattr(cli_mod, "require_local", lambda c: None)
    monkeypatch.setattr(cli_mod, "_daemon_request", daemon)
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(root.parent / "data"))
    res = CliRunner().invoke(cli_mod.cli, ["folder", "sync", str(root)])
    assert res.exit_code == 0 and "created 1" in res.output
    daemon.down = True
    (root / "b.md").write_text("beta\n")
    res = CliRunner().invoke(cli_mod.cli, ["folder", "sync", str(root)])
    assert res.exit_code == 1 and "failed 1" in res.output
