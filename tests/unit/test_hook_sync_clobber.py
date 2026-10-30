"""SM-HOOK-SYNC-CLOBBER-1: hook install never overwrites a hook the package did not write.

Daemon start (viewer_server._sync_hooks) and sm setup (setup._copy_hooks) both go through
hook_install.install_hooks. One row per destination state; HOME and the data dir are
tmp paths, so the real ~/.claude and ~/.smartmemory are never read or written.
"""

import hashlib
import json
import logging
import os

import pytest

from smartmemory_app import hook_install, setup, viewer_server
from smartmemory_app.setup_verify import verify_hooks

NEW = b"#!/bin/bash\n# v2 package hook\n"
OLD = b"#!/bin/bash\n# v1 package hook\n"
MINE = b"#!/bin/bash\n# maintained elsewhere: cap, spool\n"
NAME = "smartmemory-orient.sh"
MODIFIED_WARNING = f"hook {NAME} locally modified, not replaced"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Layouts of the destination. Each returns (path the hook is reached at, file holding its bytes).
def _plain(hooks, elsewhere, data):
    hooks.mkdir()
    if data is not None:
        (hooks / NAME).write_bytes(data)
    return hooks / NAME, hooks / NAME


def _file_link(hooks, elsewhere, data):
    hooks.mkdir()
    elsewhere.mkdir()
    (elsewhere / NAME).write_bytes(data)
    (hooks / NAME).symlink_to(elsewhere / NAME)
    return hooks / NAME, elsewhere / NAME


def _dir_link(hooks, elsewhere, data):
    elsewhere.mkdir()
    (elsewhere / NAME).write_bytes(data)
    hooks.symlink_to(elsewhere, target_is_directory=True)
    return hooks / NAME, elsewhere / NAME


def _dangling(hooks, elsewhere, data):
    hooks.mkdir()
    elsewhere.mkdir()
    (hooks / NAME).symlink_to(elsewhere / NAME)
    return hooks / NAME, elsewhere / NAME


# fmt: off
# name | via | layout | dest bytes | recorded bytes | released bytes | opt-out
#      | expected bytes | WARNING | recorded after | verify ok | verify note
ROWS = [
    ("missing dest -> copied, sha recorded", "daemon", _plain, None, None, None, False,
     NEW, False, NEW, True, ""),
    ("dest == last installed -> upgraded", "setup", _plain, OLD, OLD, None, False,
     NEW, False, NEW, True, ""),
    ("dest locally modified -> untouched", "daemon", _plain, MINE, OLD, None, False,
     MINE, True, OLD, True, "locally modified, not replaced"),
    ("no record, dest differs -> untouched", "setup", _plain, MINE, None, None, False,
     MINE, True, None, True, "locally modified, not replaced"),
    ("no record, dest is a released version -> upgraded", "daemon", _plain, OLD, None, OLD, False,
     NEW, False, NEW, True, ""),
    ("no record, dest already bundled -> recorded, not rewritten", "daemon", _plain, NEW, None, None, False,
     NEW, False, NEW, True, ""),
    ("record exists, dest is a released version -> untouched", "daemon", _plain, OLD, MINE, OLD, False,
     OLD, True, MINE, True, "locally modified, not replaced"),
    ("first run, no record file, nothing copied -> empty record written", "setup", _plain, MINE, None, None, False,
     MINE, True, None, True, "locally modified, not replaced"),
    ("opt-out -> untouched", "daemon", _plain, OLD, OLD, None, True,
     OLD, False, OLD, False, "missing or out of date"),
    ("symlinked file, locally modified -> link and file untouched", "daemon", _file_link, MINE, None, None, False,
     MINE, True, None, True, "locally modified, not replaced"),
    ("symlinked file, last installed -> upgraded through the link", "setup", _file_link, OLD, OLD, None, False,
     NEW, False, NEW, True, ""),
    ("symlinked dir, locally modified -> untouched", "daemon", _dir_link, MINE, OLD, None, False,
     MINE, True, OLD, True, "locally modified, not replaced"),
    ("dangling symlink -> nothing created", "setup", _dangling, None, None, None, False,
     None, True, None, True, "locally modified, not replaced"),
]
# fmt: on


@pytest.mark.parametrize(
    "name, via, layout, dest, recorded, released, opt_out, expected, warned, record_after, verify_ok, note",
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_hook_sync_never_replaces_a_hook_it_did_not_install(
    tmp_path,
    monkeypatch,
    caplog,
    name,
    via,
    layout,
    dest,
    recorded,
    released,
    opt_out,
    expected,
    warned,
    record_after,
    verify_ok,
    note,
):
    claude = tmp_path / "home" / ".claude"
    claude.mkdir(parents=True)
    src = tmp_path / "pkg-hooks"
    src.mkdir()
    (src / "orient.sh").write_bytes(NEW)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(setup, "CLAUDE_DIR", claude)
    monkeypatch.setattr(setup, "HOOKS_SRC", src)
    monkeypatch.setattr(setup, "_HOOK_FILE_MAP", {"orient.sh": NAME})
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setattr(viewer_server, "_shutdown_begun", lambda: False)
    if opt_out:
        monkeypatch.setenv("SMARTMEMORY_HOOK_SYNC", "0")
    else:
        monkeypatch.delenv("SMARTMEMORY_HOOK_SYNC", raising=False)
    monkeypatch.setattr(
        hook_install,
        "_RELEASED_SHAS",
        {"orient.sh": frozenset({_sha(released)} if released else ())},
    )
    hooks = claude / "hooks"
    path, holder = layout(hooks, tmp_path / "elsewhere", dest)
    if recorded is not None:
        data_dir.mkdir()
        (data_dir / "installed-hooks.json").write_text(
            json.dumps({"version": 1, "hooks": {str(path): _sha(recorded)}})
        )
    link_before = os.readlink(path) if path.is_symlink() else None
    running = (
        holder.open("rb") if holder.exists() else None
    )  # a hook bash is reading now

    with caplog.at_level(logging.INFO):
        if via == "daemon":
            viewer_server._sync_hooks()
        else:
            setup._copy_hooks()

    if expected is None:
        assert not holder.exists() and not os.path.exists(path)
    else:
        assert holder.read_bytes() == expected
    # A symlink is never replaced by a file.
    assert (os.readlink(path) if os.path.islink(path) else None) == link_before
    if layout is _dir_link:
        assert hooks.is_symlink()
    if running is not None:
        with running:
            assert (
                running.read() == dest
            )  # published by rename: the reader sees old bytes whole
    assert [p.name for p in holder.parent.iterdir() if p.name.endswith(".tmp")] == []

    assert (MODIFIED_WARNING in caplog.text) is warned
    for record in caplog.records:
        if MODIFIED_WARNING in record.getMessage():
            assert record.levelno == logging.WARNING
    if opt_out:
        assert any(
            r.levelno == logging.INFO and "Hook sync off" in r.getMessage()
            for r in caplog.records
        )

    record_file = data_dir / "installed-hooks.json"
    # Every guarded run leaves a record file, so the released-version table is used once.
    assert record_file.exists() or opt_out
    saved = json.loads(record_file.read_text())["hooks"] if record_file.exists() else {}
    assert saved.get(str(path)) == (_sha(record_after) if record_after else None)

    check = verify_hooks({})
    assert check.ok is verify_ok
    assert note in check.detail
