"""Remote-skip classifier for observe payloads adopted from the hook spill folder (SM-SPILL-ADOPT-CLASSIFY-1).

A remote Bash call or an infra MCP call can carry host output with secrets, and ``lifecycle observe``
stores the start of the result as a recallable memory. The ops observe hook drops such payloads before it
spills them (OBSERVE-SPILL-SKIP-REMOTE-1, ``smart-memory-ops/claude/hooks/smartmemory-observe.sh``), but
spill files written before that check, and adopted spill jobs requeued from ``dead/``, never passed it.
This module is the same classifier in Python, so ``capture_queue`` can refuse them at adoption.

It is a line-for-line port of the hook's two stages, and must stay identical to them:

1. Precheck on the payload text (the hook's ``observe_payload_is_remote``): any infra MCP name, or, for a
   Bash payload or one whose ``"tool_name"`` cannot be read, any token of ``REMOTE_TOKENS``.
2. Allowlist (the hook's ``OBSERVE_LOCAL_PL`` perl): a shell-family tool is observed only when its command is
   PROVABLY LOCAL: every simple command is a table command word with arguments inside that command's
   shape. Not provable (expansions, heredocs, unknown commands, remote tokens in any word, unparseable
   JSON, a missing tool name, ...) is a reject. Non-shell tools (Edit, Read, ...) always pass.

``classify`` returns the reject class (a short fixed phrase, never payload text) or ``None`` to observe.
Any error inside it is the ``classifier failed`` reject: fail closed, an over-match only drops an
observation. ``tests/contract/test_observe_classify_parity.py`` pins the tables to the hook's file and
runs both classifiers on one corpus when the ops checkout is present.
"""

from __future__ import annotations

import json
import os
import re

# The hook's OBSERVE_REMOTE_TOKENS, same order. SMARTMEMORY_OBSERVE_REMOTE_TOKENS_EXTRA adds tokens, as there.
REMOTE_TOKENS = (
    "mcp__smartmemory-infra__",
    "remote_run.py",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "mosh",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "GIT_SSH",
    "sshCommand",
    "hetzner/sm",
    "sm-hetzner",
)
INFRA_MCP = "mcp__smartmemory-infra__"
CLASSIFIER_FAILED = "classifier failed"

LOCAL = frozenset(
    """ls cat head tail wc grep egrep fgrep rg sort uniq cut tr echo printf pwd cd mkdir rmdir rm cp mv
    touch chmod ln stat file du df diff cmp comm paste column nl fold od xxd shasum md5 basename dirname realpath readlink date
    true false test [ which tee jq sleep tree uname whoami id ps pgrep lsof git find awk sed""".split()
)
ENVOK = frozenset(
    "LC_ALL LANG LC_CTYPE TZ NO_COLOR FORCE_COLOR CLICOLOR COLUMNS TERM PYTHONUTF8".split()
)
GITTOP = frozenset(
    "--no-pager -P --no-optional-locks --literal-pathspecs --no-replace-objects".split()
)
GITSUB = frozenset(
    """status log diff show branch rev-parse add commit ls-files ls-tree grep blame merge-base describe
    rev-list cat-file shortlog worktree checkout switch restore reset show-ref for-each-ref reflog merge cherry-pick revert stash tag mv rm""".split()
)
# Order matters: the first entry that matches at a position wins (the perl's grep over @OPS).
OPS = (
    "&&", "||", ";;", "|&", "&>>", "&>", ">>", ">&", "<&", "<>", ">|", "<<<", "<<-", "<<",
    "<(", ">(", ";", "&", "|", "(", ")", "<", ">",
)  # fmt: skip
REDIR = frozenset(("<", ">", ">>", ">&", "<&", "<>", ">|", "&>", "&>>", "<<<"))
SHELL_TOOL = re.compile(
    r"bash|shell|ssh|terminal|exec|command|remote|output|monitor", re.I
)

# bash `[[ =~ ]]`: [[:space:]] is the POSIX class, not Python's Unicode \s.
_TOOL_NAME = re.compile(r'"tool_name"[ \t\n\r\f\v]*:[ \t\n\r\f\v]*"([^"]*)"')
_EXTRA_NAME = re.compile(r"[A-Za-z0-9_.+-]+")


class _Reject(Exception):
    pass


def _tokens() -> list[str]:
    extra = os.environ.get("SMARTMEMORY_OBSERVE_REMOTE_TOKENS_EXTRA", "")
    return [*REMOTE_TOKENS, *extra.split()]


def _local() -> frozenset[str]:
    extra = os.environ.get("SMARTMEMORY_OBSERVE_LOCAL_EXTRA", "")
    return LOCAL | {w for w in extra.split() if _EXTRA_NAME.fullmatch(w)}


def _precheck(text: str, tokens: list[str]) -> bool:
    """The hook's ``observe_payload_is_remote`` on the payload text."""
    if INFRA_MCP in text:
        return True
    match = _TOOL_NAME.search(text)
    if match is not None and match.group(1) != "Bash":
        return False
    return any(tok in text for tok in tokens)


def _lex(s: str) -> str | list[tuple[str, str]]:
    t: list[tuple[str, str]] = []
    w, inw, i, n = "", False, 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                return "unparseable command"
            x = s[i + 1]
            if x != "\n":
                w += x
                inw = True
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return "unparseable command"
            w += s[i + 1 : j]
            inw = True
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while True:
                if j >= n:
                    return "unparseable command"
                x = s[j]
                if x == '"':
                    break
                if x == "\\" and j + 1 < n and s[j + 1] in '$`"\\\n':
                    if s[j + 1] != "\n":
                        w += s[j + 1]
                    j += 2
                    continue
                if x in "$`":
                    return "expansion"
                w += x
                j += 1
            inw = True
            i = j + 1
            continue
        if c in "$`":
            return "expansion"
        if c == "#" and not inw:
            # zsh reads `(#q...)` after an operator as a glob qualifier that can run code.
            if i > 0 and s[i - 1] not in " \t\n":
                return "unparseable command"
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in " \t":
            if inw:
                t.append(("w", w))
                w, inw = "", False
            i += 1
            continue
        if c == "\n" or c in ";&|()<>":
            if c in "<>" and inw and re.match(r"^\d+$", w):
                w, inw = "", False
            if inw:
                t.append(("w", w))
                w, inw = "", False
            if c == "\n":
                t.append(("op", ";"))
                i += 1
                continue
            op = next(o for o in OPS if s.startswith(o, i))
            if op in ("<(", ">("):
                return "process substitution"
            if op in ("<<", "<<-"):
                return "heredoc"
            t.append(("op", op))
            i += len(op)
            continue
        if re.match(r"[\x00-\x08\x0b-\x1f\x7f]", c):
            return "control character"
        w += c
        inw = True
        i += 1
    if inw:
        t.append(("w", w))
    return t


def _git_ok(w: list[str]) -> str | None:
    while w and w[0].startswith("-"):
        a = w.pop(0)
        if a in ("-C", "--git-dir", "--work-tree"):
            if not w:
                return "git option not in local table"
            w.pop(0)
            continue
        if re.match(r"^--(?:git-dir|work-tree)=", a) or a in GITTOP:
            continue
        return "git option not in local table"
    if not w or w[0] not in GITSUB:
        return "git subcommand not in local table"
    if any(re.match(r"^(?:-O|--op)", a) for a in w):
        return "git pager option"
    return None


def _awk_ok(w: list[str]) -> str | None:
    while w and w[0].startswith("-"):
        a = w.pop(0)
        if a == "--":
            break
        if a in ("-F", "-v"):
            if not w:
                return "awk option not in local shapes"
            w.pop(0)
            continue
        if re.match(r"^-[Fv].", a):
            continue
        return "awk option not in local shapes"
    if any(re.search(r"system|getline|[|>@]|/inet", a) for a in w):
        return "awk program not in local shapes"
    return None


_SED_AD = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"


def _sed_cmd_ok(x: str) -> bool:
    x = re.sub(r"^\s+", "", x)
    x = re.sub(r"\s+\Z", "", x)
    if x == "":
        return True
    x = re.sub(rf"^{_SED_AD}(?:\s*,\s*{_SED_AD})?\s*!?\s*", "", x, count=1)
    if re.match(r"^[pdqQ=nNDPgGhHxlz]$", x):
        return True
    m = re.match(r"^s([^\\\n])", x)
    if m is None:
        return False
    dl, j, parts, n = m.group(1), 2, 0, len(x)
    while j < n and parts < 2:
        ch = x[j]
        if ch == "\\":
            j += 2
            continue
        if ch == dl:
            parts += 1
        j += 1
    return parts == 2 and re.match(r"^[gpiI0-9]*\Z", x[j:]) is not None


def _sed_ok(w: list[str]) -> str | None:
    sc: list[str] = []
    rest: list[str] = []
    saw = False
    while w:
        a = w.pop(0)
        if a == "--":
            rest.extend(w)
            break
        if a in ("-e", "--expression"):
            if not w:
                return "sed option not in local shapes"
            sc.append(w.pop(0))
            saw = True
            continue
        m = re.match(r"^(?:-e|--expression=)(.+)\Z", a, re.S)
        if m:
            sc.append(m.group(1))
            saw = True
            continue
        if a == "-i":
            if w and w[0] == "":
                w.pop(0)
            continue
        if (
            re.match(r"^-[nErsuz]+\Z", a)
            or re.match(r"^-i.", a)
            or re.match(
                r"^--(?:quiet|silent|regexp-extended|separate|unbuffered|null-data|in-place(?:=.*)?)\Z",
                a,
            )
        ):
            continue
        if re.match(r"^-.", a):
            return "sed option not in local shapes"
        rest.append(a)
    if not saw and rest:
        sc.append(rest.pop(0))
    if not sc:
        return "sed script not in local shapes"
    for s in sc:
        for p in re.split(r"[;\n]", s):
            if not _sed_cmd_ok(p):
                return "sed script not in local shapes"
    return None


def _simple(w: list[str], local: frozenset[str]) -> str | None:
    w = list(w)
    while w and w[0] in ("{", "}", "!"):
        w.pop(0)
    while w and (m := re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=", w[0])):
        if m.group(1) not in ENVOK:
            return "env assignment not in local table"
        w.pop(0)
    if not w:
        return None
    if w[0] == "env":
        w.pop(0)
        while w:
            a = w[0]
            if a == "-i":
                w.pop(0)
                continue
            if a == "-u":
                w.pop(0)
                if not w:
                    return "env option not in local table"
                w.pop(0)
                continue
            if re.match(r"^(?:-u|--unset=)[A-Za-z_][A-Za-z0-9_]*\Z", a):
                w.pop(0)
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", a)
            if m:
                if m.group(1) not in ENVOK:
                    return "env assignment not in local table"
                w.pop(0)
                continue
            if a.startswith("-"):
                return "env option not in local table"
            break
        if not w:
            return "env without command"
    c = w.pop(0)
    if c not in local:
        return "command not in local table"
    if c == "git":
        return _git_ok(w)
    if c == "awk":
        return _awk_ok(w)
    if c == "sed":
        return _sed_ok(w)
    if c == "printf" and any(a.startswith("-v") for a in w):
        return "printf -v"
    if c in ("test", "[") and any(a in ("-v", "-R") for a in w):
        return "test -v"
    if c == "find" and any(re.match(r"^-(?:exec|execdir|ok|okdir)\Z", a) for a in w):
        return "find exec"
    # long options may be abbreviated (getopt_long, git parse-options): match the shortest prefix that can reach them.
    if c == "rg" and any(a.startswith("--pr") for a in w):
        return "rg preprocessor"
    if c == "sort" and any(a.startswith("--com") for a in w):
        return "sort compress program"
    return None


def _command(cmd: str, tokens: list[str], local: frozenset[str]) -> str | None:
    t = _lex(cmd)
    if isinstance(t, str):
        return t
    cmds: list[list[str]] = [[]]
    words: list[str] = []
    i = 0
    while i < len(t):
        k, v = t[i]
        if k == "w":
            cmds[-1].append(v)
            words.append(v)
        elif v in REDIR:
            if i + 1 >= len(t) or t[i + 1][0] != "w":
                return "redirect without target"
            words.append(t[i + 1][1])
            i += 1
        else:
            cmds.append([])
        i += 1
    for w in words:
        if re.search(r"/dev/(?:tcp|udp)/", w):
            return "network redirect"
        # a quoted `$(`, backtick, `${` or `$[` survives quote removal and is re-expanded by arithmetic contexts.
        if re.search(r"`|\$[({\[]", w):
            return "embedded substitution"
        if any(k in w for k in tokens):
            return "remote token"
    for c in cmds:
        why = _simple(c, local)
        if why is not None:
            return why
    return None


def _no_constant(name: str):
    raise ValueError(f"JSON constant {name} is not valid JSON")


def _scalar(value) -> str | None:
    """A JSON value as the perl sees a non-reference scalar, or None for null and references (incl. booleans)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return None


def _allowlist(text: str, tokens: list[str]) -> str | None:
    """The hook's ``OBSERVE_LOCAL_PL`` on the payload text."""
    try:
        d = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return "unparseable payload"
    if not isinstance(d, dict):
        return "unparseable payload"
    tool = _scalar(d.get("tool_name"))
    if tool is None:
        return "no tool name"
    if tool.startswith(INFRA_MCP):
        return "infra mcp"
    if tool != "Bash" and not SHELL_TOOL.search(tool):
        return None
    ti = d.get("tool_input")
    cmd = _scalar(ti.get("command")) if isinstance(ti, dict) else None
    if cmd is None:
        return "shell tool without command"
    return _command(cmd, tokens, _local())


def payload_text(payload) -> str:
    """The text the hook classified: a spilled non-JSON payload is kept as its raw string, any other value is
    re-encoded the way ``observe_drain.py`` feeds ``smartmemory-observe.sh --classify``."""
    return payload if isinstance(payload, str) else json.dumps(payload)


def classify(payload) -> str | None:
    """Reject class for one observe payload (a spill envelope's ``payload``), or None to observe it.

    Fail closed: any error is ``classifier failed``. The class never contains payload text.
    """
    try:
        text = payload_text(payload)
        tokens = _tokens()
        if _precheck(text, tokens):
            return "remote token"
        return _allowlist(text, tokens)
    except Exception:  # noqa: BLE001 - fail closed: a classifier crash quarantines, never queues
        return CLASSIFIER_FAILED
