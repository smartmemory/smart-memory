#!/usr/bin/env python3
"""SETUP-FRESH-CI-1: install the `sm` CLI into a clean environment, like a new user.

Used by CI (.github/workflows/fresh-install.yml) and runnable locally:

    python3 scripts/fresh_install_check.py --wheel dist/smartmemory-X.Y.Z-py3-none-any.whl \
        [--core PATH] [--workdir DIR] [--python 3.12] [--keep]

``--core`` is an optional local smartmemory-core source tree or wheel, for a core
version that is not on PyPI yet (it is copied first, so the tree is never written to).

Every step prints ``STEP <name> PASS|FAIL|SKIP``. The exit code is non-zero when any
step fails. The script owns one temp directory (venv, throwaway HOME, project dir),
removes it on exit, and kills only the fake server it started. It never contacts
api.smartmemory.ai: the remote arm talks to a stdlib fake on 127.0.0.1.

Linux only by default. On macOS, ``sm setup`` registers a launchd job under the fixed
label ai.smartmemory.daemon, which belongs to the real user session and outlives this
script, so the script refuses unless ``--allow-launchd`` is given. Run it in a Linux
container instead.

Model downloads: ``sm setup`` fetches the spaCy model and the local embedding model,
exactly as for a new user. Those are the only network use besides PyPI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Answers to the prompts of `sm setup --mode local`, in order: coreference (no),
# LLM provider (none: blank keys), embedding provider, spaCy size, data directory.
SETUP_ANSWERS = "n\nnone\nlocal\nsm\n\n"
SETUP_TIMEOUT = 900
CMD_TIMEOUT = 300

# Doctor rows (name -> why a FAIL/WARN is expected) allowed in a keyless local setup.
# Anything else that is not PASS fails the step. Matched against the row name, which
# is the text before the first run of two or more spaces after the status marker.
EXPECTED_DOCTOR_ROWS = {
    "LLM": "provider is `none` and no LLM key exists, by design of this check",
}

# Harness-only variables. Crash reports go to a third-party host and the default
# daemon port may be taken, so the script sets both and records `use` decisions for
# them through `sm env set`, which keeps them out of the inherited-variable warning.
HARNESS_VARS = ("SMARTMEMORY_CRASH_REPORTS", "SMARTMEMORY_DAEMON_PORT")

# Wrapper's own names from env_sources/env_pin (CLI-ENV-DETECT-1) plus the ones
# config.load_config reads, used for the poisoned arm.
POISON = {
    "SMARTMEMORY_API_URL": "https://example.invalid",
    "SMARTMEMORY_TEAM_ID": "poison-team",
    "SMARTMEMORY_MODE": "remote",
    "SMARTMEMORY_LLM_PROVIDER": "poison-provider",
    "SMARTMEMORY_DATA_DIR": "/nonexistent/poison-data-dir",
}

FAKE_ME = {
    "email": "fresh-install@example.test",
    "tenant_id": "tenant_fake",
    "default_workspace_id": "ws_fake",
    "active_workspace_id": "ws_fake",
    "default_team_id": "ws_fake",
}
FAKE_READY = {
    "status": "ready",
    "version": "0.0.0-fake",
    "checks": [{"name": "falkordb", "ok": True}],
}

RESULTS: list[tuple[str, str]] = []


def say(msg: str = "") -> None:
    print(msg, flush=True)


def note(msg: str) -> None:
    for line in str(msg).splitlines() or [""]:
        say(f"    {line}")


def record(name: str, status: str, why: str = "") -> None:
    RESULTS.append((name, status))
    say(f"STEP {name} {status}" + (f" ({why})" if why else ""))


class StepFailed(Exception):
    pass


def check(cond: bool, why: str) -> None:
    if not cond:
        raise StepFailed(why)


def run_step(name: str, fn) -> bool:
    """Run ``fn``; print one STEP line. Returns True on PASS."""
    try:
        extra = fn()
    except StepFailed as exc:
        record(name, "FAIL", str(exc).splitlines()[0])
        for line in str(exc).splitlines()[1:]:
            note(line)
        return False
    except subprocess.TimeoutExpired as exc:
        record(
            name,
            "FAIL",
            f"timeout after {exc.timeout}s: {' '.join(map(str, exc.cmd))[:120]}",
        )
        return False
    except Exception as exc:  # a harness bug is a FAIL, never a silent pass
        record(name, "FAIL", f"harness error {type(exc).__name__}: {exc}")
        note(traceback.format_exc())
        return False
    record(name, "PASS", extra or "")
    return True


def skip(name: str, why: str) -> None:
    record(name, "SKIP", why)


# ── Sandbox ───────────────────────────────────────────────────────────────────


class Sandbox:
    """One owned temp dir: venv, throwaway HOMEs, project dirs, fake-server state."""

    def __init__(self, root: Path, py: str, uv: str):
        self.root = root
        self.py = py
        self.uv = uv
        self.venv = root / "venv"
        self.port = free_port()

    @property
    def bin(self) -> Path:
        return self.venv / "bin"

    def env(
        self, home: Path, extra: dict[str, str] | None = None, port: int | None = None
    ) -> dict[str, str]:
        """A minimal environment: no inherited SMARTMEMORY_*, no provider keys."""
        env = {
            "HOME": str(home),
            "PATH": os.pathsep.join(
                [
                    str(self.bin),
                    str(Path(self.uv).parent),
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                ]
            ),
            "TERM": "dumb",
            "CI": "1",
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.root / "tmp"),
            "SMARTMEMORY_CRASH_REPORTS": "0",
            "SMARTMEMORY_DAEMON_PORT": str(port or self.port),
        }
        env.update(extra or {})
        return env

    def sm(
        self, args, home, cwd, extra_env=None, stdin="", timeout=CMD_TIMEOUT, port=None
    ):
        return subprocess.run(
            [str(self.bin / "sm"), *args],
            cwd=cwd,
            env=self.env(home, extra_env, port),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def out_of(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or "") + (r.stderr or "")


def tail(text: str, n: int = 12) -> str:
    return "\n".join(text.splitlines()[-n:])


def wheel_version(wheel: Path) -> str:
    m = re.match(r"^[A-Za-z0-9_.]+-([0-9][^-]*)-", wheel.name)
    check(m is not None, f"cannot read the version from the wheel name {wheel.name}")
    return m.group(1)


# ── Fake remote service ───────────────────────────────────────────────────────


class FakeService:
    """Answers /auth/me and /ready on 127.0.0.1 with the shapes the wrapper's tests use."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []  # (path, Authorization header)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                outer.seen.append((path, self.headers.get("Authorization", "")))
                body = {"/auth/me": FAKE_ME, "/ready": FAKE_READY}.get(path)
                code = 200 if body is not None else 404
                raw = json.dumps(
                    body if body is not None else {"detail": "not found"}
                ).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # keep the output small
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


# ── Steps ─────────────────────────────────────────────────────────────────────

ROUND_TRIP_TEXT = "Freshcheck: Alice leads Project Atlas in Berlin"
DECISION_TEXT = "Freshcheck decision: we chose SQLite for the Atlas store"
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def new_home(sb: Sandbox, name: str) -> tuple[Path, Path]:
    home, proj = sb.root / f"home-{name}", sb.root / f"proj-{name}"
    home.mkdir()
    proj.mkdir()
    return home, proj


def decide_harness(
    sb: Sandbox, home: Path, proj: Path, port: int, names=HARNESS_VARS
) -> None:
    for name in names:
        r = sb.sm(["env", "set", name, "use"], home, proj, port=port)
        check(r.returncode == 0, f"`sm env set {name} use` failed\n{tail(out_of(r))}")


def step_venv(sb: Sandbox, want_py: str) -> str:
    r = subprocess.run(
        [sb.uv, "venv", "--python", want_py, str(sb.venv)],
        capture_output=True,
        text=True,
        timeout=CMD_TIMEOUT,
    )
    check(r.returncode == 0, f"uv venv failed\n{tail(out_of(r))}")
    got = subprocess.run(
        [
            str(sb.bin / "python"),
            "-c",
            "import sys;print('%d.%d' % sys.version_info[:2])",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    check(got == want_py, f"venv python is {got}, wanted {want_py}")
    return f"python {got}"


def step_install(sb: Sandbox, wheel: Path, core: Path | None) -> str:
    reqs: list[str] = []
    if core is not None:
        if core.is_dir():
            copy = sb.root / "core-src"
            shutil.copytree(
                core, copy, ignore=shutil.ignore_patterns(".git", "build", "*.egg-info")
            )
            reqs.append(f"{copy}[lite,wikipedia,typescript]")
        else:
            reqs.append(f"{core}[lite,wikipedia,typescript]")
    reqs.append(str(wheel))
    r = subprocess.run(
        [sb.uv, "pip", "install", "-p", str(sb.bin / "python"), *reqs],
        capture_output=True,
        text=True,
        timeout=SETUP_TIMEOUT,
    )
    check(r.returncode == 0, f"uv pip install failed\n{tail(out_of(r), 20)}")
    want = wheel_version(wheel)
    home, proj = new_home(sb, "version")
    r = sb.sm(["--version"], home, proj)
    check(r.returncode == 0, f"sm --version exited {r.returncode}\n{tail(out_of(r))}")
    check(
        want in r.stdout, f"sm --version printed {r.stdout.strip()!r}, wheel is {want}"
    )
    return r.stdout.strip()


def step_setup_local(
    sb, home, proj, port, extra_env=None
) -> subprocess.CompletedProcess:
    return sb.sm(
        ["setup", "--mode", "local"],
        home,
        proj,
        extra_env,
        SETUP_ANSWERS,
        SETUP_TIMEOUT,
        port,
    )


def assert_setup_ok(sb: Sandbox, r: subprocess.CompletedProcess, port: int) -> str:
    text = out_of(r)
    check(r.returncode == 0, f"sm setup exited {r.returncode}\n{tail(text, 25)}")
    check(
        f"daemon running at http://127.0.0.1:{port}" in text,
        f"setup did not report the daemon at 127.0.0.1:{port}\n{tail(text, 15)}",
    )
    return f"daemon on 127.0.0.1:{port}"


def doctor_rows(text: str) -> list[tuple[str, str, str]]:
    """(status, check, detail) from `sm doctor --json` stdout (log lines may precede it)."""
    start = text.find("{")
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    return [
        (str(c.get("status", "")), str(c.get("check", "")), str(c.get("detail", "")))
        for c in data.get("checks", [])
    ]


def run_doctor(sb, home, proj, port, extra_env=None):
    r = sb.sm(["doctor", "--json"], home, proj, extra_env, port=port)
    rows = doctor_rows(r.stdout)
    check(
        bool(rows),
        f"sm doctor --json printed no checks (exit {r.returncode})\n{tail(out_of(r))}",
    )
    return r, rows


def assert_doctor_clean(r, rows) -> str:
    bad = [(s, n, d) for s, n, d in rows if s != "pass"]
    unexpected = [(s, n, d) for s, n, d in bad if n not in EXPECTED_DOCTOR_ROWS]
    for s, n, d in bad:
        if n in EXPECTED_DOCTOR_ROWS:
            note(f"expected {s} row {n!r}: {EXPECTED_DOCTOR_ROWS[n]}")
    check(
        not unexpected,
        "unexpected doctor rows: "
        + "; ".join(f"{s} {n}: {d[:120]}" for s, n, d in unexpected),
    )
    check(r.returncode == 0, f"sm doctor exited {r.returncode}\n{tail(out_of(r))}")
    return f"{len(rows)} rows, {len(bad)} expected non-pass"


def poll(fn, what: str, seconds: int = 60):
    """Retry ``fn`` (returns a value or None) until it answers, since enrichment is async."""
    deadline = time.monotonic() + seconds
    last = None
    while True:
        last = fn()
        if last is not None:
            return last
        if time.monotonic() > deadline:
            raise StepFailed(f"{what}: nothing after {seconds}s")
        time.sleep(2)


def search_json(sb, home, proj, query) -> list[dict]:
    r = sb.sm(["search", query, "--json", "--top-k", "10"], home, proj)
    check(r.returncode == 0, f"sm search exited {r.returncode}\n{tail(out_of(r))}")
    start = r.stdout.find("[")
    check(start >= 0, f"sm search --json printed no JSON list\n{tail(out_of(r))}")
    return json.loads(r.stdout[start:])


def add_item(sb, home, proj, text, extra=()) -> str:
    r = sb.sm(["add", *extra, text], home, proj)
    check(r.returncode == 0, f"sm add exited {r.returncode}\n{tail(out_of(r))}")
    m = UUID_RE.search(r.stdout)
    check(m is not None, f"sm add printed no item id\n{tail(out_of(r))}")
    return m.group(0)


def step_add(sb, home, proj, state) -> str:
    state["item"] = add_item(sb, home, proj, ROUND_TRIP_TEXT)
    return f"id {state['item'][:8]}"


def step_search(sb, home, proj, state) -> str:
    def look():
        hits = [
            i
            for i in search_json(sb, home, proj, "Project Atlas Berlin")
            if i.get("item_id") == state["item"]
        ]
        return hits or None

    hits = poll(look, "search did not return the added sentence")
    check(
        len(hits) >= 1 and hits[0].get("content") == ROUND_TRIP_TEXT,
        "hit content differs from what was added",
    )
    return f"{len(hits)} hit"


def step_decision(sb, home, proj) -> str:
    """`sm add --type decision` is the CLI's only decision-creating command (there is no `sm decide`)."""
    item = add_item(sb, home, proj, DECISION_TEXT, ("--type", "decision"))

    def look():
        hits = [
            i
            for i in search_json(sb, home, proj, "chose SQLite Atlas store")
            if i.get("memory_type") == "decision" and i.get("content") == DECISION_TEXT
        ]
        return hits or None

    hits = poll(look, "decision not found by search")
    r = sb.sm(["get", item], home, proj)
    check(
        r.returncode == 0 and DECISION_TEXT in r.stdout,
        f"sm get {item[:8]} did not return the decision\n{tail(out_of(r))}",
    )
    return f"{len(hits)} decision hit(s) for 1 add, sm get ok"


def step_poison_setup(sb, home, proj, port, state) -> str:
    """Current behavior (CLI-ENV-DETECT-1 + SETUP-DOCTOR-1), asserted exactly:

    `sm setup --mode local` still writes a local config, prints the inherited-variable
    warning naming every undecided variable, then the post-setup doctor FAILs on the
    inherited-variable row and the unreachable service, so setup exits 1.
    """
    r = step_setup_local(sb, home, proj, port, POISON)
    text = out_of(r)
    state["poison_setup"] = text
    check(
        r.returncode == 1,
        f"expected exit 1 with poisoned env, got {r.returncode}\n{tail(text, 20)}",
    )
    warn = next(
        (
            ln
            for ln in text.splitlines()
            if ln.startswith("WARNING: SmartMemory is using settings inherited")
        ),
        "",
    )
    check(bool(warn), f"no inherited-settings warning\n{tail(text, 20)}")
    missing = [n for n in POISON if n not in warn]
    check(not missing, f"warning does not name {missing}: {warn[:300]}")
    check(
        not any(h in warn for h in HARNESS_VARS),
        "warning names a variable decided as `use`",
    )
    cfg = (home / ".config" / "smartmemory" / "config.toml").read_text()
    check(
        'mode = "local"' in cfg, "setup did not write the local mode it was asked for"
    )
    # SMARTMEMORY_MODE is covered by the `mode = "local"` check above ("remote" is also a table name).
    leaked = [n for n, v in POISON.items() if n != "SMARTMEMORY_MODE" and v in cfg]
    check(
        not leaked,
        f"setup wrote the value of {leaked} from the environment into the config file",
    )
    return f"exit 1, warning names {len(POISON)} variables, config stays local"


def step_poison_doctor(sb, home, proj, port) -> str:
    r = sb.sm(["doctor", "--json"], home, proj, POISON, port=port)
    rows = doctor_rows(r.stdout)
    by = {n: (s, d) for s, n, d in rows}
    check(r.returncode == 1, f"expected exit 1, got {r.returncode}\n{tail(out_of(r))}")
    s, d = by.get("Inherited SMARTMEMORY_* vars", ("", ""))
    check(s == "fail", f"Inherited-vars row is {s!r}, expected fail\n{rows}")
    missing = [n for n in POISON if n not in d]
    check(not missing, f"Inherited-vars row does not name {missing}")
    check(
        by.get("Mode", ("", ""))[0] == "warn" and "SMARTMEMORY_MODE" in by["Mode"][1],
        "Mode row does not name SMARTMEMORY_MODE",
    )
    check(
        by.get("SmartMemory service", ("", ""))[0] == "fail",
        "service row is not fail for the .invalid API URL",
    )
    return "fail on Inherited vars + SmartMemory service, warn on Mode"


def step_remote_setup(sb, home, proj, port, fake: FakeService) -> str:
    key = "fresh-check-fake-key"
    decide_harness(
        sb,
        home,
        proj,
        port,
        ("SMARTMEMORY_CRASH_REPORTS", "SMARTMEMORY_DAEMON_PORT", "SMARTMEMORY_API_KEY"),
    )
    r = sb.sm(
        ["setup", "--mode", "remote", "--api-key", key, "--api-url", fake.url],
        home,
        proj,
        {"SMARTMEMORY_API_KEY": key},
        port=port,
        timeout=SETUP_TIMEOUT,
    )
    text = out_of(r)
    check(r.returncode == 0, f"remote setup exited {r.returncode}\n{tail(text, 25)}")
    check(
        f"Authenticated as {FAKE_ME['email']}. Team: {FAKE_ME['default_team_id']}"
        in text,
        f"setup did not report the fake tenant/workspace\n{tail(text, 15)}",
    )
    cfg = (home / ".config" / "smartmemory" / "config.toml").read_text()
    check(
        'mode = "remote"' in cfg and fake.url in cfg,
        "config file is not remote against the fake server",
    )
    me = [a for p, a in fake.seen if p == "/auth/me"]
    check(
        me and me[0] == f"Bearer {key}",
        "fake server did not get the bearer key on /auth/me",
    )
    return f"authenticated as {FAKE_ME['email']}, team {FAKE_ME['default_team_id']}"


def step_remote_doctor(sb, home, proj, port, fake: FakeService) -> str:
    r, rows = run_doctor(
        sb, home, proj, port, {"SMARTMEMORY_API_KEY": "fresh-check-fake-key"}
    )
    by = {n: (s, d) for s, n, d in rows}
    check(
        r.returncode == 0, f"sm doctor exited {r.returncode}\n{tail(out_of(r))}\n{rows}"
    )
    s, d = by.get("Tenant and workspace", ("", ""))
    check(
        s == "pass"
        and FAKE_ME["tenant_id"] in d
        and FAKE_ME["default_workspace_id"] in d,
        f"Tenant and workspace row is {s!r}: {d[:200]}",
    )
    s, d = by.get("SmartMemory service", ("", ""))
    check(s == "pass" and fake.url in d, f"service row is {s!r}: {d[:200]}")
    return f"tenant {FAKE_ME['tenant_id']}, workspace {FAKE_ME['default_workspace_id']}"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def step_cleanup(
    sb: Sandbox,
    homes: list[tuple[Path, Path, int]],
    fake: FakeService | None,
    keep: bool,
) -> str:
    """Stop only what this run started: the daemons under its own HOMEs and its fake server."""
    pids: list[int] = []
    for home, proj, port in homes:
        pidfile = home / ".smartmemory" / "daemon.pid"
        if pidfile.exists():
            try:
                pids.append(int(pidfile.read_text().split()[0]))
            except (ValueError, IndexError):
                pass
        if (sb.bin / "sm").exists():
            try:
                sb.sm(["stop"], home, proj, port=port, timeout=90)
            except Exception as exc:
                note(f"warning: sm stop failed for {home.name}: {exc}")
    if fake is not None:
        fake.stop()
    left = [p for p in pids if pid_alive(p)]
    check(not left, f"daemon pids still alive after sm stop: {left}")
    if not keep:
        shutil.rmtree(sb.root, ignore_errors=True)
        check(not sb.root.exists(), f"could not remove {sb.root}")
    return "kept " + str(sb.root) if keep else "temp dir removed"


# ── Main ──────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--wheel", required=True, type=Path, help="built smartmemory CLI wheel"
    )
    ap.add_argument(
        "--core", type=Path, help="local smartmemory-core source dir or wheel"
    )
    ap.add_argument(
        "--workdir",
        type=Path,
        help="parent dir for the temp dir (default: $RUNNER_TEMP or system temp)",
    )
    ap.add_argument(
        "--python", default="3.12", help="Python for the venv (default 3.12)"
    )
    ap.add_argument(
        "--keep", action="store_true", help="keep the temp dir for inspection"
    )
    ap.add_argument(
        "--allow-launchd",
        action="store_true",
        help="macOS: let setup register the real launchd job",
    )
    args = ap.parse_args(argv)

    if sys.platform == "darwin" and not args.allow_launchd:
        say(
            "REFUSED: on macOS `sm setup` registers the launchd label ai.smartmemory.daemon in your"
        )
        say(
            "session, which outlives this script. Run it in a Linux container, or pass --allow-launchd."
        )
        return 2
    uv = shutil.which("uv")
    if uv is None:
        say("REFUSED: uv is not on PATH (pip install uv)")
        return 2
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        say(f"REFUSED: wheel not found: {wheel}")
        return 2

    parent = args.workdir or (
        Path(os.environ["RUNNER_TEMP"]) if os.environ.get("RUNNER_TEMP") else None
    )
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="fresh-install-check-", dir=parent)).resolve()
    (root / "tmp").mkdir()
    sb = Sandbox(root, args.python, uv)
    say(
        f"fresh-install check: wheel={wheel.name} core={args.core or 'PyPI'} temp={root}"
    )

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    fake = FakeService()
    fake.start()
    homes: list[tuple[Path, Path, int]] = []
    state: dict = {}
    try:
        ok = run_step("venv", lambda: step_venv(sb, args.python))
        ok = ok and run_step(
            "install",
            lambda: step_install(sb, wheel, args.core.resolve() if args.core else None),
        )
        if not ok:
            for n in (
                "setup-local",
                "doctor",
                "add",
                "search",
                "decision",
                "poison-setup",
                "poison-doctor",
                "remote-setup",
                "remote-doctor",
            ):
                skip(n, "install failed")
        else:
            home, proj = new_home(sb, "main")
            homes.append((home, proj, sb.port))
            if run_step(
                "setup-local",
                lambda: (
                    decide_harness(sb, home, proj, sb.port),
                    assert_setup_ok(
                        sb, step_setup_local(sb, home, proj, sb.port), sb.port
                    ),
                )[1],
            ):
                run_step(
                    "doctor",
                    lambda: assert_doctor_clean(*run_doctor(sb, home, proj, sb.port)),
                )
                if run_step("add", lambda: step_add(sb, home, proj, state)):
                    run_step("search", lambda: step_search(sb, home, proj, state))
                else:
                    skip("search", "add failed")
                run_step("decision", lambda: step_decision(sb, home, proj))
            else:
                for n in ("doctor", "add", "search", "decision"):
                    skip(n, "setup-local failed")

            phome, pproj = new_home(sb, "poison")
            pport = free_port()
            homes.append((phome, pproj, pport))
            run_step(
                "poison-setup",
                lambda: (
                    decide_harness(sb, phome, pproj, pport),
                    step_poison_setup(sb, phome, pproj, pport, state),
                )[1],
            )
            run_step(
                "poison-doctor", lambda: step_poison_doctor(sb, phome, pproj, pport)
            )

            rhome, rproj = new_home(sb, "remote")
            rport = free_port()
            homes.append((rhome, rproj, rport))
            if run_step(
                "remote-setup", lambda: step_remote_setup(sb, rhome, rproj, rport, fake)
            ):
                run_step(
                    "remote-doctor",
                    lambda: step_remote_doctor(sb, rhome, rproj, rport, fake),
                )
            else:
                skip("remote-doctor", "remote-setup failed")
    finally:
        run_step("cleanup", lambda: step_cleanup(sb, homes, fake, args.keep))

    failed = [n for n, st in RESULTS if st == "FAIL"]
    say(
        f"RESULT {'FAIL' if failed else 'PASS'}: "
        + (", ".join(failed) if failed else f"{len(RESULTS)} steps")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
