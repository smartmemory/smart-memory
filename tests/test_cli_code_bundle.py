"""`smartmemory code bundle` (CODE-BUNDLE-CLI-1): store-free local snapshot, atomic write, refusals."""

import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from smartmemory_app.cli_code import code_group

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test_bundle",
    "GIT_AUTHOR_EMAIL": "test_bundle@example.invalid",
    "GIT_COMMITTER_NAME": "test_bundle",
    "GIT_COMMITTER_EMAIL": "test_bundle@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SMARTMEMORY_CODE_CHECKPOINT_DIR", str(tmp_path / "test_bundle_checkpoints")
    )
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    try:
        yield tmp_path
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def make_checkout(root, *, broken=False):
    (root / "web").mkdir(parents=True)
    (root / "app.py").write_text(
        "def py_fn():\n    return helper()\n\n\ndef helper():\n    return 1\n"
    )
    (root / "web" / "app.ts").write_text(
        "export function tsFn() { return other(); }\nfunction other() { return 1; }\n"
    )
    if broken:
        (root / "bad.py").write_text("def oops(:\n    pass\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def no_store(monkeypatch):
    """Any attempt to open a backend, graph, vector store or memory fails the test."""

    def boom(*args, **kwargs):
        raise AssertionError("code bundle must be store-free")

    import smartmemory_app.storage as storage
    from smartmemory.graph.smartgraph import SmartGraph

    monkeypatch.setattr(storage, "get_memory", boom)
    monkeypatch.setattr(SmartGraph, "__init__", boom)
    for dotted in (
        "smartmemory.stores.vector.vector_store.VectorStore.__init__",
        "smartmemory.smart_memory.SmartMemory.__init__",
    ):
        try:
            monkeypatch.setattr(dotted, boom)
        except (ImportError, AttributeError):
            pass


def run(*args):
    return CliRunner().invoke(code_group, ["bundle", *map(str, args)])


def test_bundle_cli_writes_snapshot_and_summary_without_a_store(workdir, no_store):
    root = make_checkout(workdir / "test_bundle_checkout")
    out = workdir / "out" / "snap.json"
    result = run(root, "--repo", "demo", "--out", out)
    assert result.exit_code == 0, result.output
    snapshot = json.loads(out.read_text())
    assert (
        snapshot["repo"] == "demo"
        and snapshot["complete"] is True
        and snapshot["schema_version"] == "1"
    )
    assert {e["file_path"] for e in snapshot["entities"]} == {"app.py", "web/app.ts"}
    line = [
        x for x in result.output.splitlines() if x.startswith("[code:bundle] repo=")
    ][0]
    assert (
        f"entities={len(snapshot['entities'])}" in line
        and f"relations={len(snapshot['relations'])}" in line
    )
    assert (
        "complete=true" in line
        and f"commit_hash={snapshot['commit_hash']}" in line
        and f"out={out}" in line
    )
    assert not [p for p in out.parent.iterdir() if p.name != "snap.json"]


def test_bundle_cli_requires_repo_and_out(workdir):
    root = make_checkout(workdir / "test_bundle_checkout")
    assert run(root, "--out", workdir / "s.json").exit_code != 0
    assert run(root, "--repo", "demo").exit_code != 0


def test_bundle_cli_has_no_commit_hash_flag(workdir):
    root = make_checkout(workdir / "test_bundle_checkout")
    assert (
        run(
            root, "--repo", "d", "--out", workdir / "s.json", "--commit-hash", "abc"
        ).exit_code
        != 0
    )


def test_bundle_cli_refusal_writes_nothing_and_keeps_existing_out(workdir, no_store):
    root = make_checkout(workdir / "test_bundle_checkout", broken=True)
    fresh = workdir / "fresh.json"
    result = run(root, "--repo", "demo", "--out", fresh)
    assert (
        result.exit_code != 0
        and "[code:bundle] error:" in result.output
        and "bad.py" in result.output
    )
    assert not fresh.exists() and not list(workdir.glob(".fresh.json*"))
    existing = workdir / "existing.json"
    existing.write_text("PRIOR")
    assert run(root, "--repo", "demo", "--out", existing).exit_code != 0
    assert existing.read_text() == "PRIOR"


def test_bundle_cli_allow_partial_exits_zero_with_warning(workdir, no_store):
    root = make_checkout(workdir / "test_bundle_checkout", broken=True)
    out = workdir / "partial.json"
    result = run(root, "--repo", "demo", "--out", out, "--allow-partial")
    assert result.exit_code == 0, result.output
    snapshot = json.loads(out.read_text())
    assert snapshot["complete"] is False and [
        f["path"] for f in snapshot["failed_paths"]
    ] == ["bad.py"]
    assert "complete=false" in result.output
    assert (
        "[code:bundle] warning: incomplete" in result.output
        and "bad.py" in result.output
    )


def test_bundle_cli_atomic_write_failure_leaves_prior_file_and_no_temp(
    workdir, monkeypatch
):
    root = make_checkout(workdir / "test_bundle_checkout")
    out = workdir / "snap.json"
    out.write_text("PRIOR")
    import smartmemory_app.cli_code as cli_code

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cli_code.os, "replace", explode)
    result = run(root, "--repo", "demo", "--out", out)
    assert result.exit_code != 0 and "[code:bundle] error:" in result.output
    assert (
        out.read_text() == "PRIOR"
        and [p.name for p in workdir.iterdir() if p.name.startswith(".snap")] == []
    )


@pytest.mark.parametrize("ignored", [False, True])
def test_bundle_cli_warns_when_out_inside_checkout_and_not_ignored(workdir, ignored):
    root = make_checkout(workdir / "test_bundle_checkout")
    if ignored:
        (root / ".gitignore").write_text("snap.json\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "ignore")
    result = run(root, "--repo", "demo", "--out", root / "snap.json")
    assert result.exit_code == 0, result.output
    assert ("not git-ignored" in result.output) is (not ignored)


def test_bundle_cli_out_outside_checkout_has_no_warning(workdir):
    root = make_checkout(workdir / "test_bundle_checkout")
    result = run(root, "--repo", "demo", "--out", workdir / "snap.json")
    assert result.exit_code == 0 and "git-ignored" not in result.output


@pytest.mark.parametrize("fields", ["full", "minimal"])
def test_bundle_cli_fields_modes(workdir, fields):
    root = make_checkout(workdir / "test_bundle_checkout")
    out = workdir / f"{fields}.json"
    assert run(root, "--repo", "demo", "--out", out, "--fields", fields).exit_code == 0
    entity = json.loads(out.read_text())["entities"][0]
    assert ("framework_evidence" in entity) is (fields == "full")


def test_bundle_cli_language_narrowing_and_determinism(workdir):
    root = make_checkout(workdir / "test_bundle_checkout")
    a, b = workdir / "a.json", workdir / "b.json"
    assert (
        run(root, "--repo", "demo", "--out", a, "--language", "python").exit_code == 0
    )
    assert {e["file_path"] for e in json.loads(a.read_text())["entities"]} == {"app.py"}
    assert run(root, "--repo", "demo", "--out", a).exit_code == 0
    assert run(root, "--repo", "demo", "--out", b).exit_code == 0
    assert a.read_bytes() == b.read_bytes()


def test_bundle_cli_write_refuses_nan_and_keeps_prior_file(workdir):
    from smartmemory_app.cli_code import _write_atomic

    out = workdir / "snap.json"
    out.write_text("prior")
    with pytest.raises(ValueError):
        _write_atomic(out, {"x": float("nan")})
    assert out.read_text() == "prior"
    assert [p.name for p in workdir.iterdir() if p.name.endswith(".tmp")] == []


def test_bundle_cli_fields_help_names_the_stratum_consumer():
    result = CliRunner().invoke(code_group, ["bundle", "--help"])
    assert "STRAT-CODEGRAPH-1" in "".join(result.output.split())


@pytest.mark.parametrize(
    "path,no_filename",
    [("unreadable", True), ("unreadable: part", False), ("目录: 部分", True)],
)
def test_bundle_cli_partial_collection_keeps_the_exact_path(workdir, path, no_filename):
    import os
    import sys

    root = make_checkout(workdir / "test_bundle_cli_collection_identity")
    denied = root / path
    denied.mkdir()
    (denied / "hidden.py").write_text("def hidden(): return 1\n")
    out = workdir / "test_bundle_cli_collection.json"
    script = """
import os, sys
from click.testing import CliRunner
from smartmemory_app.cli_code import code_group
root, path, no_filename, out = sys.argv[1:]
def audit(event, args):
    if event == 'os.scandir' and str(args[0]) == os.path.join(root, path):
        if no_filename == 'True':
            raise PermissionError('test_bundle CLI denial: no filename')
        raise PermissionError(13, 'test_bundle CLI denial: colon', os.path.join(root, path))
sys.addaudithook(audit)
r = CliRunner().invoke(code_group, ['bundle', root, '--repo', 'test_bundle', '--out', out, '--allow-partial'])
assert r.exit_code == 0, (r.output, r.exception)
assert '[code:bundle] warning: incomplete' in r.output
print(r.output)
"""
    run = subprocess.run(
        [sys.executable, "-c", script, str(root), path, str(no_filename), str(out)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    snapshot = json.loads(out.read_text())
    assert snapshot["complete"] is False
    assert [f["path"] for f in snapshot["failed_paths"]] == [path]
    assert snapshot["entities"]
    assert list(out.parent.glob("." + out.name + ".*")) == []
