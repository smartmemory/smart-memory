"""LITE-RECALL-WIDEN-UNBOUNDED-1 fix 4: a git worktree shares its main checkout's workspace id.

``git rev-parse --show-toplevel`` of a linked worktree is the worktree, so every worktree session got a fresh, empty
workspace: recall found nothing and widened over the whole store. A plain checkout and a non-git directory keep the
id they had before (``ws_`` + sha1 of the realpath of the toplevel, or of the cwd).
"""

import hashlib
import os
import shutil
import subprocess

import pytest

from smartmemory_app.recall_format import derive_workspace_id


def base_id(path):
    """The id before this change: sha1 of the realpath of the toplevel (or of the cwd outside git)."""
    return "ws_" + hashlib.sha1(os.path.realpath(path).encode("utf-8")).hexdigest()[:12]


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    """A scratch tree git cannot climb out of, with no inherited repo or workspace override."""
    root = tmp_path.resolve()
    for name in (
        "SMARTMEMORY_WORKSPACE_ID",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root))
    return root


def repo(path):
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    (path / "src").mkdir()
    return path


needs_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="runner image has no git (OPS-RUNNER-IMAGE-GIT-1); the lander runs these locally until after 2.0",
)


@needs_git
def test_worktree_shares_the_main_checkout_id(tmp):
    main = repo(tmp / "main")
    git("worktree", "add", "-q", "-b", "test_wt", str(tmp / "wt"), cwd=main)
    (tmp / "wt" / "deep").mkdir()

    assert derive_workspace_id(str(main)) == base_id(main)
    assert derive_workspace_id(str(tmp / "wt")) == base_id(main)
    assert derive_workspace_id(str(tmp / "wt" / "deep")) == base_id(main)


@needs_git
def test_plain_checkout_and_non_git_dir_keep_their_ids(tmp):
    plain = repo(tmp / "plain")
    loose = tmp / "loose"
    loose.mkdir()
    link = tmp / "link"
    link.symlink_to(plain)

    assert derive_workspace_id(str(plain)) == base_id(plain)
    assert derive_workspace_id(str(plain / "src")) == base_id(plain)
    assert derive_workspace_id(str(link / "src")) == base_id(plain)
    assert derive_workspace_id(str(loose)) == base_id(loose)


@needs_git
def test_bare_repo_worktree_keeps_its_own_id(tmp):
    """No main checkout exists for a bare repo: each worktree keeps the id it had (its toplevel)."""
    seed = repo(tmp / "seed")
    git("clone", "-q", "--bare", str(seed), str(tmp / "store.git"), cwd=tmp)
    git("worktree", "add", "-q", str(tmp / "bare_wt"), cwd=tmp / "store.git")

    assert derive_workspace_id(str(tmp / "bare_wt")) == base_id(tmp / "bare_wt")
