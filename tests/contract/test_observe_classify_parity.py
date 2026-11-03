"""SM-SPILL-ADOPT-CLASSIFY-1: ``observe_classify`` stays identical to the ops observe hook's classifier.

The hook (``smart-memory-ops/claude/hooks/smartmemory-observe.sh``, OBSERVE-SPILL-SKIP-REMOTE-1) is the first
line; this package's port guards spill adoption. Two checks keep them the same:

1. Tables: the token list, command tables, operator lists and the shell-tool pattern are parsed out of the
   hook file and compared to the Python constants.
2. Behaviour: one corpus of payloads with the expected reject class. Python is checked against the column
   on every run; when the hook file is present, ``bash <hook> --classify`` is checked against it too (fed the
   way ``observe_drain.py`` feeds it), so a change on either side that is not mirrored fails here.

The hook is found by ``SMARTMEMORY_OBSERVE_HOOK`` or as a ``smart-memory-ops`` checkout next to an ancestor
of this repo; without it, both hook checks skip (the corpus column still pins the Python side).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from smartmemory_app import observe_classify as oc

_REL = Path("smart-memory-ops/claude/hooks/smartmemory-observe.sh")


def _hook() -> Path | None:
    env = os.environ.get("SMARTMEMORY_OBSERVE_HOOK")
    if env:
        return Path(env) if Path(env).is_file() else None
    for parent in Path(__file__).resolve().parents:
        if (parent / _REL).is_file():
            return parent / _REL
    return None


HOOK = _hook()
needs_hook = pytest.mark.skipif(
    HOOK is None, reason="ops observe hook checkout not present"
)


def _bash(command, **extra) -> dict:
    return {
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        **extra,
    }


# payload, expected reject class (None = observed)
CORPUS = [
    ({"tool_name": "Edit", "tool_input": {"file_path": "/w/ssh.md"}}, None),
    ({"tool_name": "Read", "tool_input": {"file_path": "/w/a.py"}}, None),
    (_bash("ls -la"), None),
    (_bash("git status && ls | head -3"), None),
    (_bash("cd /x && git -C /y status"), None),
    (_bash("ls 2>/dev/null"), None),
    (_bash("ls # comment"), None),
    (_bash("{ ls; }"), None),
    (_bash("LC_ALL=C sort f"), None),
    (_bash("env -i ls"), None),
    (_bash("sed -n 1,5p f"), None),
    (_bash("sed -e 's/a/b/g' f"), None),
    (_bash("awk -F, '{print $1}' f"), None),
    (_bash("ssh host uptime"), "remote token"),
    (_bash("s''sh host uptime"), "remote token"),
    (_bash("\\s\\s\\h host"), "remote token"),
    (_bash("echo hetzner/sm"), "remote token"),
    (_bash("ls", tool_response={"stdout": "rsync"}), "remote token"),
    ({"tool_name": "mcp__smartmemory-infra__run", "tool_input": {}}, "remote token"),
    ('{"tool_name": "mcp\\u005f_smartmemory-infra__run", "tool_input": {}}', "infra mcp"),  # escaped past the precheck
    ({"tool_name": "mcp__term__exec", "tool_input": {"command": "ls"}}, None),
    ({"tool_name": "mcp__term__exec", "tool_input": {"command": "s''sh h"}}, "remote token"),
    ({"tool_name": "mcp__term__exec", "tool_input": {}}, "shell tool without command"),
    ({"tool_name": "BashOutput", "tool_input": {"bash_id": "1"}}, "shell tool without command"),
    (_bash("docker -H tcp://x ps"), "command not in local table"),
    (_bash("python3 x.py"), "command not in local table"),
    (_bash("sudo ls"), "command not in local table"),
    (_bash(5), "command not in local table"),
    (_bash("echo $HOME"), "expansion"),
    (_bash('echo "$(id)"'), "expansion"),
    (_bash("echo '$(id)'"), "embedded substitution"),
    (_bash("cat <<EOF\nx\nEOF"), "heredoc"),
    (_bash("cat <(ls)"), "process substitution"),
    (_bash("echo 'open"), "unparseable command"),
    (_bash("ls;#x"), "unparseable command"),
    (_bash("ls\r"), "control character"),
    (_bash("ls >"), "redirect without target"),
    (_bash("cat </dev/tcp/1.2.3.4/80"), "network redirect"),
    (_bash("FOO=1 ls"), "env assignment not in local table"),
    (_bash("env"), "env without command"),
    (_bash("env -x ls"), "env option not in local table"),
    (_bash("git -c core.pager=x log"), "git option not in local table"),
    (_bash("git fetch"), "git subcommand not in local table"),
    (_bash("git log -Oorder"), "git pager option"),
    (_bash("awk '{system(\"x\")}' f"), "awk program not in local shapes"),
    (_bash("awk -f prog f"), "awk option not in local shapes"),
    (_bash("sed 'e id' f"), "sed script not in local shapes"),
    (_bash("sed -f s f"), "sed option not in local shapes"),
    (_bash("printf -v x y"), "printf -v"),
    (_bash("test -v X"), "test -v"),
    (_bash("find . -exec rm {} +"), "find exec"),
    (_bash("rg --pre x y"), "rg preprocessor"),
    (_bash("sort --compress-program=x f"), "sort compress program"),
    (_bash(None), "shell tool without command"),
    ({"session_id": "s"}, "no tool name"),
    ({"tool_name": True}, "no tool name"),
    ({"tool_name": 5}, None),
    ([1], "unparseable payload"),
    ("not json", "unparseable payload"),
    ("not json ssh", "remote token"),
]  # fmt: skip


def test_python_classifier_matches_corpus(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_OBSERVE_REMOTE_TOKENS_EXTRA", raising=False)
    monkeypatch.delenv("SMARTMEMORY_OBSERVE_LOCAL_EXTRA", raising=False)
    got = [(p, oc.classify(p)) for p, _ in CORPUS]
    assert got == [(p, want) for p, want in CORPUS]


@needs_hook
def test_hook_classifier_matches_corpus(tmp_path):
    if shutil.which("bash") is None or shutil.which("perl") is None:
        pytest.skip("bash or perl missing")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "SMARTMEMORY_DATA_DIR": str(tmp_path / "data"),
    }
    got = []
    for payload, _ in CORPUS:
        r = subprocess.run(
            ["bash", str(HOOK), "--classify"],
            input=oc.payload_text(payload).encode(),
            capture_output=True,
            timeout=30,
            env=env,
        )
        out = r.stdout.decode().strip()
        got.append((payload, None if (r.returncode, out) == (0, "pass") else out))
    assert got == [(p, want) for p, want in CORPUS]


def _qw(src: str, var: str) -> set[str]:
    m = re.search(
        rf"my {re.escape(var)} = map \{{ \$_ => 1 \}} \(?qw\((.*?)\)", src, re.S
    )
    assert m, var
    return set(m.group(1).split())


@needs_hook
def test_hook_tables_match_python():
    src = HOOK.read_text(encoding="utf-8")
    tokens = re.search(r'^OBSERVE_REMOTE_TOKENS="(.*)"$', src, re.M).group(1)
    tokens = re.sub(r"\$\{[^}]*\}", "", tokens).split()
    ops = re.search(r"^my @OPS = \((.*?)\);$", src, re.M).group(1)
    redir = re.search(r"^my %REDIR = map \{ \$_ => 1 \} \((.*?)\);$", src, re.M).group(
        1
    )
    shell = re.search(r'\$tool eq "Bash" \|\| \$tool =~ /(.*?)/i;', src).group(1)
    infra = re.search(r'index\(\$tool, "(.*?)"\) == 0', src).group(1)
    got = (
        tuple(tokens),
        _qw(src, "%LOCAL"),
        _qw(src, "%ENVOK"),
        _qw(src, "%GITTOP"),
        _qw(src, "%GITSUB"),
        tuple(re.findall(r'"([^"]+)"', ops)),
        set(re.findall(r'"([^"]+)"', redir)),
        shell,
        infra,
    )
    want = (
        oc.REMOTE_TOKENS,
        set(oc.LOCAL),
        set(oc.ENVOK),
        set(oc.GITTOP),
        set(oc.GITSUB),
        oc.OPS,
        set(oc.REDIR),
        oc.SHELL_TOOL.pattern,
        oc.INFRA_MCP,
    )
    assert got == want
