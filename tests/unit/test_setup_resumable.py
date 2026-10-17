"""SETUP-RESUMABLE-1: sm setup as a resumable, checked step list.

Tables, one row per step or per case (no stacked parametrize):
  * the saved progress file (atomic, versioned, no secrets)
  * one check per setup step, each with a world where it passes and one where it fails
  * the resume rule (which step a re-run starts at)
  * --yes end to end, the [k/n] counter with measured estimates, one fix line per failure
  * the mode check (an inherited SMARTMEMORY_MODE names itself and the fix)
  * the save-and-recall round trip: for real against Lite in a throwaway HOME, and
    against a hosted API faked at the httpx transport
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import stat
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from smartmemory_app import (
    config,
    daemon,
    doctor,
    setup,
    setup_run,
    setup_state,
    setup_steps,
    setup_verify,
    storage,
)
from smartmemory_app.setup_state import DONE, FAILED, PENDING, VERIFIED
from smartmemory_app.setup_verify import Check

SYNTHETIC_KEY = "sm_test_SYNTHETICKEY0123456789abcdef"

INSTALL_FUNCS = (
    "_ensure_spacy",
    "_ensure_embedding_model",
    "_ensure_lazy_models",
    "_copy_hooks",
    "_copy_skills",
    "_register_hooks",
    "_seed_data_dir",
)

# The local plan of a --yes run (embedding local, no running daemon).
LOCAL_KEYS = [
    "config",
    "spacy",
    "embedding",
    "extra_models",
    "hooks",
    "skills",
    "register",
    "seed",
    "daemon",
    "mode",
    "roundtrip",
]


def _choices(**over) -> dict:
    return {**setup.LOCAL_DEFAULTS, "restart": False, **over}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway HOME with every install step and the daemon start faked."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "SMARTMEMORY_MODE",
        "SMARTMEMORY_WORKSPACE_ID",
        "SMARTMEMORY_TEAM_ID",
        "SMARTMEMORY_API_URL",
        "SMARTMEMORY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    # Never reach the user's real daemon on its default port.
    monkeypatch.setenv("SMARTMEMORY_DAEMON_PORT", str(_closed_port()))
    claude = home / ".claude"
    monkeypatch.setattr(setup, "CLAUDE_DIR", claude)
    monkeypatch.setattr(setup, "HOOKS_DEST", claude / "hooks")
    monkeypatch.setattr(setup, "SETTINGS", claude / "settings.json")
    monkeypatch.setattr(setup, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(setup, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(
        setup,
        "check_installation",
        lambda: SimpleNamespace(ok=True, socks_support_ok=True),
    )
    monkeypatch.setattr(setup, "_can_run_tui", lambda: False)
    monkeypatch.setattr("smartmemory_app.launch_metrics.emit", lambda *a, **k: None)
    monkeypatch.setattr(storage, "_remote_memory", None)
    monkeypatch.setattr(storage, "_memory", None)
    monkeypatch.setattr(storage, "_data_path", None)

    calls: list[str] = []
    for name in INSTALL_FUNCS:
        monkeypatch.setattr(setup, name, lambda *a, _n=name, **k: calls.append(_n))

    def start(**kwargs):
        calls.append("_start_daemon_local")
        return {"status": "ok"}

    monkeypatch.setattr(setup, "_start_daemon_local", start)
    monkeypatch.setattr(daemon, "is_running", lambda **kwargs: False)
    monkeypatch.setattr(daemon, "stop_daemon", lambda: calls.append("stop_daemon"))
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    return SimpleNamespace(tmp=tmp_path, home=home, claude=claude, calls=calls)


@pytest.fixture
def real_checks(monkeypatch):
    """The real per-step checks instead of the conftest all-pass stub."""
    monkeypatch.setattr(setup_verify, "verify_step", setup_verify._verify_step)


def _fake_checks(monkeypatch, failing: dict[str, Check] | None = None) -> list[str]:
    """Every check passes except ``failing``. Returns the keys checked, in order."""
    seen: list[str] = []
    failing = failing or {}

    def verify(key, choices):
        seen.append(key)
        return failing.get(key, Check(True, f"{key} ok"))

    monkeypatch.setattr(setup_verify, "verify_step", verify)
    return seen


def _invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(setup.setup, list(args), input=input)


# ── saved progress file ─────────────────────────────────────────────────────


def test_progress_file_is_versioned_owner_only_and_holds_no_secret(world, monkeypatch):
    """A remote run with a real key: the key reaches the keychain fake, never the file."""
    stored = []
    monkeypatch.setattr(config, "set_api_key", stored.append)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200,
            json={"email": "test@example.test", "default_team_id": "ws_test_team"},
            request=httpx.Request("GET", url),
        ),
    )
    result = _invoke(
        "--mode",
        "remote",
        "--api-key",
        SYNTHETIC_KEY,
        "--api-url",
        "https://api.test.invalid",
    )
    assert result.exit_code == 0, result.output
    assert stored == [SYNTHETIC_KEY]
    path = setup_state.state_path()
    text = path.read_text()
    assert SYNTHETIC_KEY not in text
    assert SYNTHETIC_KEY[8:] not in text  # no partial copy either
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(text)
    assert data["schema"] == setup_state.SCHEMA
    assert set(data["choices"]) <= setup_state.CHOICE_KEYS
    assert [s["key"] for s in data["steps"]] == [
        s.key for s in setup_steps.remote_setup_plan()
    ]
    assert all(s["status"] == VERIFIED and s["at"] for s in data["steps"])
    # Atomic write: no temp file is left beside it.
    assert [
        p.name for p in path.parent.iterdir() if p.name.startswith(".setup-state-")
    ] == []


def test_progress_file_refuses_a_secret_shaped_choice_and_redacts_reasons(world):
    state = setup_state.SetupState.new(
        {"mode": "local", "api_key": SYNTHETIC_KEY}, ["config"]
    )
    with pytest.raises(ValueError, match="api_key"):
        setup_state.save(state)
    assert not setup_state.state_path().exists()

    state = setup_state.SetupState.new({"mode": "local"}, ["auth"])
    state.mark(
        "auth", FAILED, "request failed: Authorization: Bearer sk-ant-" + "x" * 30
    )
    setup_state.save(state)
    assert "sk-ant-" not in setup_state.state_path().read_text()


# One row per unreadable shape: each starts a fresh run, with a WARNING naming why.
_BAD_FILES = [
    ("not-json", "{not json", "unreadable"),
    (
        "other-schema",
        json.dumps({"schema": 99, "choices": {}, "steps": []}),
        "schema 99",
    ),
    ("malformed", json.dumps({"schema": 1, "choices": {}, "steps": [{}]}), "malformed"),
]


@pytest.mark.parametrize("row", _BAD_FILES, ids=[r[0] for r in _BAD_FILES])
def test_an_unusable_progress_file_starts_a_fresh_run(world, monkeypatch, caplog, row):
    _, body, warning = row
    path = setup_state.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _fake_checks(monkeypatch)
    result = _invoke("--yes")
    assert result.exit_code == 0, result.output
    assert "Resuming" not in result.output
    assert warning in caplog.text
    assert setup_state.load().complete


# ── one check per step ──────────────────────────────────────────────────────


def _cfg(**over):
    return config.SmartMemoryConfig(**{**{"mode": "local"}, **over})


def _good_config(w, mp):
    config.save_config(setup_run._cfg_from(_choices()))


def _spacy(present):
    def arrange(w, mp):
        import spacy

        mp.setattr(spacy.util, "is_package", lambda name: present)

    return arrange


def _embedding(present):
    def arrange(w, mp):
        def require(**kwargs):
            if not present:
                raise FileNotFoundError("missing")

        mp.setattr("smartmemory.tools.factory._require_embedding_model", require)

    return arrange


def _helpers(present):
    def arrange(w, mp):
        def resolve(*a, **k):
            if not present:
                raise FileNotFoundError("missing")
            return w.tmp

        mp.setattr("smartmemory.utils.hf_models.resolve_local_path", resolve)

    return arrange


def _real(name):
    """Run the real install function (undoing the world's fake) into the tmp HOME."""
    original = getattr(setup, name)

    def arrange(w, mp):
        mp.setattr(setup, name, original)
        if name == "_seed_data_dir":
            original(str(w.tmp / "data"))
        else:
            original()

    return arrange


def _removed(*parts):
    """A world where the effect is gone: the file or folder under the tmp root is deleted."""

    def arrange(w, mp):
        import shutil

        target = w.tmp.joinpath(*parts)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    return arrange


def _doctor_row(func, status):
    def arrange(w, mp):
        if func == "check_credentials":
            config.save_config(_cfg(mode="remote", api_key_set=True))
        mp.setattr(
            doctor,
            func,
            lambda ctx: doctor.Row(
                "row", status, "detail", "" if status == doctor.PASS else "fix it"
            ),
        )

    return arrange


def _mode(env):
    def arrange(w, mp):
        config.save_config(_cfg(mode="local"))
        if env:
            mp.setenv("SMARTMEMORY_MODE", env)

    return arrange


def _nothing(w, mp):
    pass


# (key, arrange a world where the check passes, one where it fails or None)
_CHECK_ROWS = [
    ("config", _good_config, _nothing),
    ("spacy", _spacy(True), _spacy(False)),
    ("embedding", _embedding(True), _embedding(False)),
    ("extra_models", _helpers(True), _helpers(False)),
    ("hooks", _real("_copy_hooks"), _removed("home", ".claude", "hooks")),
    ("skills", _real("_copy_skills"), _removed("home", ".claude", "skills")),
    (
        "register",
        _real("_register_hooks"),
        _removed("home", ".claude", "settings.json"),
    ),
    ("seed", _real("_seed_data_dir"), _removed("data", "entity_patterns.jsonl")),
    ("restart", _nothing, None),  # a stop leaves nothing to re-check
    (
        "daemon",
        _doctor_row("_check_daemon", doctor.PASS),
        _doctor_row("_check_daemon", doctor.FAIL),
    ),
    (
        "auth",
        _doctor_row("check_credentials", doctor.PASS),
        _doctor_row("check_credentials", doctor.FAIL),
    ),
    ("mode", _mode(""), _mode("remote")),
]


def test_every_step_of_every_plan_has_a_check_row():
    keys = {s.key for s in setup_steps.setup_plan("local", "local", restart=True)}
    keys |= {s.key for s in setup_steps.remote_setup_plan()}
    assert set(setup_verify._VERIFIERS) == keys
    # roundtrip has its own tests below (real Lite and faked hosted).
    assert {r[0] for r in _CHECK_ROWS} == keys - {"roundtrip"}


@pytest.mark.parametrize("row", _CHECK_ROWS, ids=[r[0] for r in _CHECK_ROWS])
def test_step_check_passes_on_its_effect_and_fails_without_it(
    world, monkeypatch, real_checks, row
):
    key, good, bad = row
    choices = _choices(seed_dir=str(world.tmp / "data"))
    if key == "auth":
        choices = {"mode": "remote"}
    with monkeypatch.context() as mp:
        good(world, mp)
        check = setup_verify.verify_step(key, choices)
        assert check.ok, check
    if bad is None:
        return
    config.config_path().unlink(missing_ok=True)
    with monkeypatch.context() as mp:
        bad(world, mp)
        check = setup_verify.verify_step(key, choices)
    assert not check.ok, check
    assert check.fix.startswith(("Run:", "Check", "Fix", "Unset", "fix")), check


def test_a_check_that_crashes_is_a_failed_check_not_a_crash(
    world, monkeypatch, real_checks
):
    monkeypatch.setitem(setup_verify._VERIFIERS, "spacy", lambda choices: 1 / 0)
    check = setup_verify.verify_step("spacy", _choices())
    assert not check.ok and "ZeroDivisionError" in check.detail and check.fix


# ── resume rule ─────────────────────────────────────────────────────────────


def _saved(statuses: dict[str, str], **choices) -> None:
    state = setup_state.SetupState.new(_choices(**choices), LOCAL_KEYS)
    for key in LOCAL_KEYS:
        state.mark(key, statuses.get(key, VERIFIED))
    setup_state.save(state)


def _through(key: str, then: str = PENDING) -> dict[str, str]:
    """Every step before ``key`` verified, ``key`` and after set to ``then``."""
    at = LOCAL_KEYS.index(key)
    return {k: (VERIFIED if i < at else then) for i, k in enumerate(LOCAL_KEYS)}


# (id, saved statuses, failing checks now, first step that runs again)
_RESUME_ROWS = [
    ("stopped-before-daemon", _through("daemon"), {}, "daemon"),
    (
        "daemon-died-after-verify",
        _through("roundtrip", FAILED),
        {"daemon": Check(False, "down", "Run: sm start")},
        "daemon",
    ),
    (
        "config-file-deleted",
        _through("roundtrip", FAILED),
        {"config": Check(False, "gone", "Run: sm setup")},
        "config",
    ),
    ("failed-in-the-middle", {**_through("hooks"), "hooks": FAILED}, {}, "hooks"),
    (
        "optional-step-failed",
        {**_through("daemon"), "extra_models": FAILED},
        {},
        "daemon",
    ),
    ("done-but-unchecked", {**_through("seed"), "seed": DONE}, {}, "daemon"),
]


@pytest.mark.parametrize("row", _RESUME_ROWS, ids=[r[0] for r in _RESUME_ROWS])
def test_rerun_resumes_at_the_first_step_whose_effect_is_gone(world, monkeypatch, row):
    _, statuses, failing, first = row
    _saved(statuses)
    # A broken effect fails its check once (the resume scan). Running the step again
    # brings it back, so the check after the re-run passes.
    broken = dict(failing)
    monkeypatch.setattr(
        setup_verify,
        "verify_step",
        lambda key, choices: broken.pop(key, None) or Check(True, f"{key} ok"),
    )
    result = _invoke(input="")  # no answers: a resume must not ask anything
    assert result.exit_code == 0, result.output
    at = LOCAL_KEYS.index(first)
    assert f"at step {at + 1} of {len(LOCAL_KEYS)}" in result.output
    assert "sm setup --restart" in result.output
    body = {
        "spacy": "_ensure_spacy",
        "embedding": "_ensure_embedding_model",
        "extra_models": "_ensure_lazy_models",
        "hooks": "_copy_hooks",
        "skills": "_copy_skills",
        "register": "_register_hooks",
        "seed": "_seed_data_dir",
        "daemon": "_start_daemon_local",
    }
    expected = [body[k] for k in LOCAL_KEYS[at:] if k in body]
    assert world.calls == expected
    for index, key in enumerate(LOCAL_KEYS[:at], start=1):
        assert f"[{index}/{len(LOCAL_KEYS)}]" in result.output
    assert setup_state.load().complete


def test_restart_flag_forgets_the_saved_run_and_starts_over(world, monkeypatch):
    _saved(_through("daemon"))
    _fake_checks(monkeypatch)
    result = _invoke("--restart", "--yes")
    assert result.exit_code == 0, result.output
    assert "Resuming" not in result.output
    assert world.calls[0] == "_ensure_spacy" and "_start_daemon_local" in world.calls
    assert setup_state.load().complete


# (id, saved state, setup flags) where a re-run does NOT resume
_FRESH_ROWS = [
    ("finished-run", {}, ["--yes"]),
    ("other-mode-flag", _through("daemon"), ["--yes", "--mode", "local"]),
]


@pytest.mark.parametrize("row", _FRESH_ROWS, ids=[r[0] for r in _FRESH_ROWS])
def test_a_finished_run_or_a_different_mode_is_a_new_setup(world, monkeypatch, row):
    _, statuses, flags = row
    mode = "remote" if row[0] == "other-mode-flag" else "local"
    _saved(statuses, mode=mode)
    _fake_checks(monkeypatch)
    result = _invoke(*flags)
    assert result.exit_code == 0, result.output
    assert "Resuming" not in result.output
    assert world.calls[0] == "_ensure_spacy"


def test_resume_reruns_the_stop_step_only_while_it_is_unfinished(world, monkeypatch):
    """The daemon state is asked again on resume: a running one is stopped first."""
    _saved(_through("daemon"))
    monkeypatch.setattr(daemon, "is_running", lambda **kwargs: True)
    _fake_checks(monkeypatch)
    result = _invoke()
    assert result.exit_code == 0, result.output
    assert world.calls == ["stop_daemon", "_start_daemon_local"]
    assert (
        "[9/12] SmartMemory stopped" in result.output
        or "[9/12] Stopping" in result.output
    )


# ── --yes, counter, estimates, fix lines ────────────────────────────────────


def test_yes_runs_every_step_with_no_prompt_and_saves_the_defaults(world, monkeypatch):
    _fake_checks(monkeypatch)
    result = _invoke("--yes", input="")
    assert result.exit_code == 0, result.output
    cfg = config.load_config(apply_env=False)
    assert (cfg.mode, cfg.llm_provider, cfg.embedding_provider, cfg.spacy_model) == (
        "local",
        "groq",
        "local",
        "en_core_web_sm",
    )
    state = setup_state.load()
    assert [s.key for s in state.steps] == LOCAL_KEYS and state.complete
    assert world.calls == [
        "_ensure_spacy",
        "_ensure_embedding_model",
        "_ensure_lazy_models",
        "_copy_hooks",
        "_copy_skills",
        "_register_hooks",
        "_seed_data_dir",
        "_start_daemon_local",
    ]


def test_yes_in_remote_mode_needs_a_key_and_says_how(world):
    result = _invoke("--yes", "--mode", "remote", input="")
    assert result.exit_code == 1
    assert "Fix: run sm setup --mode remote --yes --api-key KEY" in result.output


def test_counter_shows_k_of_n_with_the_measured_estimate_per_step(world, monkeypatch):
    _fake_checks(monkeypatch)
    result = _invoke("--yes")
    assert result.exit_code == 0, result.output
    plan = setup_steps.setup_plan("local", "local")
    starts = [
        ln for ln in result.output.splitlines() if " ..." in ln and ln.startswith("[")
    ]
    assert len(starts) == len(plan)
    for index, (line, step) in enumerate(zip(starts, plan), start=1):
        phrase = setup_steps.estimate_phrase(step)
        want = f"[{index}/{len(plan)}] {step.title} ..."
        assert line == (f"{want} ({phrase})" if phrase else want), line
    # The checks have no measured row, so they print no estimate (no invented numbers).
    assert all(step.estimate_seconds is None for step in setup_steps.VERIFY_STEPS)
    assert setup_verify.MODE_HELP in result.output


_BODIES = {
    "spacy": "_ensure_spacy",
    "embedding": "_ensure_embedding_model",
    "hooks": "_copy_hooks",
    "skills": "_copy_skills",
    "register": "_register_hooks",
    "seed": "_seed_data_dir",
    "daemon": "_start_daemon_local",
}


@pytest.mark.parametrize("key", LOCAL_KEYS)
def test_every_failed_step_prints_one_plain_fix_line_and_is_saved_failed(
    world, monkeypatch, key
):
    if key in _BODIES:  # the step itself raises
        monkeypatch.setattr(
            setup,
            _BODIES[key],
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _fake_checks(monkeypatch)
        want_fix = setup_run.RUN_FIX[key]
    else:  # the step runs, its check fails
        _fake_checks(
            monkeypatch, {key: Check(False, "effect missing", "Run: sm doctor")}
        )
        want_fix = "Run: sm doctor"
    result = _invoke("--yes")
    record = setup_state.load().record(key)
    if key == "extra_models":  # optional: a warning, the run goes on
        assert result.exit_code == 0, result.output
        assert record.status == FAILED and record.reason == "effect missing"
        assert f"Fix: {want_fix}" in result.output
        return
    assert result.exit_code != 0, result.output
    assert record.status == FAILED and record.reason
    fix_lines = [ln for ln in result.output.splitlines() if ln.startswith("Fix: ")]
    assert fix_lines == [f"Fix: {want_fix}"], result.output
    later = LOCAL_KEYS[LOCAL_KEYS.index(key) + 1 :]
    assert all(setup_state.load().record(k).status == PENDING for k in later)


def _strings(obj) -> list[str]:
    """String constants in a module or function, minus docstrings and log calls."""
    tree = ast.parse(inspect.getsource(obj))
    docs = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    logs = {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("warning", "debug", "info")
        for arg in ast.walk(node)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docs
        and id(n) not in logs
    ]


def test_new_user_facing_strings_have_no_em_dash_or_semicolon():
    texts = _strings(setup_run) + _strings(setup_verify)
    texts += [s.name for s in setup_steps.VERIFY_STEPS] + [
        s.title for s in setup_steps.VERIFY_STEPS
    ]
    texts += [s.title for s in setup_steps.remote_setup_plan()]
    assert setup_verify.MODE_HELP in texts and setup_run.RUN_FIX["daemon"] in texts
    for text in texts:
        assert "—" not in text and ";" not in text, text


# ── mode check (CLI-LOCAL-MODE-1) ───────────────────────────────────────────


def test_an_inherited_mode_variable_fails_setup_and_names_itself(world, monkeypatch):
    calls = []

    def verify(key, choices):
        calls.append(key)
        if key == "mode":
            return setup_verify._verify_step(key, choices)
        return Check(True, "ok")

    monkeypatch.setattr(setup_verify, "verify_step", verify)
    monkeypatch.setenv("SMARTMEMORY_MODE", "remote")
    result = _invoke("--yes")
    assert result.exit_code == 1, result.output
    assert "SMARTMEMORY_MODE" in result.output
    assert "Fix: Unset SMARTMEMORY_MODE" in result.output
    assert "roundtrip" not in calls  # stops at the mismatch
    assert setup_state.load().record("mode").status == FAILED


def test_mode_check_agrees_with_the_resolver_every_command_uses(world, real_checks):
    config.save_config(_cfg(mode="local"))
    assert config.load_config().mode == "local"
    check = setup_verify.verify_step("mode", {"mode": "local"})
    assert check.ok and "local mode" in check.detail
    check = setup_verify.verify_step("mode", {"mode": "remote"})
    assert not check.ok and check.fix == "Run: sm config mode remote"


# ── save and recall round trip ──────────────────────────────────────────────


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def lite(world, monkeypatch, real_checks):
    """Real Lite in the throwaway HOME, no daemon (the CLI's in-process route)."""
    data = world.tmp / "data"
    config.save_config(
        _cfg(
            mode="local",
            llm_provider="none",
            embedding_provider="local",
            daemon_port=_closed_port(),
            data_dir=str(data),
        )
    )
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "ws_test_setup_lite")
    yield data
    if storage._memory is not None:
        close = getattr(storage._memory, "close", None)
        if close:
            close()


def test_round_trip_saves_reads_back_and_finds_a_memory_in_lite(lite, capsys):
    check = setup_verify.round_trip({"mode": "local"}, sleep=lambda s: None)
    assert check.ok, check
    assert "mode local, tenant none" in check.detail
    assert "workspace $SMARTMEMORY_WORKSPACE_ID" in check.detail
    assert "ws_test_setup_lite" not in check.detail  # env values are named, not shown
    # The test memory is removed again.
    from smartmemory_app import cli

    out = CliRunner().invoke(
        cli.search_cmd, [setup_verify.ROUND_TRIP_PREFIX, "--json", "--top-k", "20"]
    )
    rows = setup_verify._json_tail(out.output) if out.output.strip() else []
    assert not any(
        setup_verify.ROUND_TRIP_PREFIX in str(r.get("content", "")) for r in rows
    ), rows


def test_round_trip_fails_when_the_memory_lands_outside_the_reported_scope(
    lite, monkeypatch
):
    from smartmemory_app import scope_header

    real = scope_header.resolve_scope

    def elsewhere(*args, **kwargs):
        scope = real(*args, **kwargs)
        return SimpleNamespace(**{**vars(scope), "workspace_id": "ws_test_reported"})

    monkeypatch.setattr(scope_header, "resolve_scope", elsewhere)
    check = setup_verify.round_trip({"mode": "local"}, sleep=lambda s: None)
    assert not check.ok
    assert "ws_test_setup_lite" in check.detail and "ws_test_reported" in check.detail


def test_setup_yes_ends_with_a_real_round_trip_in_lite(lite, monkeypatch):
    real = setup_verify._verify_step
    monkeypatch.setattr(
        setup_verify,
        "verify_step",
        lambda key, choices: real(key, choices)
        if key in ("mode", "roundtrip")
        else Check(True, "ok"),
    )
    monkeypatch.setitem(setup.LOCAL_DEFAULTS, "llm_provider", "none")
    monkeypatch.setitem(setup.LOCAL_DEFAULTS, "data_dir", str(lite))
    result = _invoke("--yes")
    assert result.exit_code == 0, result.output
    assert "[11/11] Saving and recalling a test memory ..." in result.output
    assert "saved, read back and found a test memory in mode local" in result.output
    state = setup_state.load()
    assert (
        state.complete and "workspace $SMARTMEMORY_WORKSPACE_ID" in state.verified_scope
    )


class _Hosted:
    """The hosted API at the httpx transport: records every request's workspace header."""

    def __init__(self, stamp_workspace: str):
        self.stamp = stamp_workspace
        self.headers: list[str] = []
        self.items: dict[str, dict] = {}

    def get(self, url, **kw):
        assert url.endswith("/auth/me"), url
        return httpx.Response(
            200,
            json={
                "email": "test@example.test",
                "default_team_id": "ws_test_team",
                "tenant_id": "tn_test",
            },
            request=httpx.Request("GET", url),
        )

    def request(self, method, url, headers=None, **kw):
        self.headers.append((headers or {}).get("X-Workspace-Id"))
        path = url.split("api.test.invalid", 1)[1]
        req = httpx.Request(method, url)
        if method == "POST" and path == "/memory/ingest":
            item = {"item_id": "it_test_1", "content": kw["json"]["content"]}
            self.items["it_test_1"] = item
            return httpx.Response(200, json=item, request=req)
        if method == "GET" and path.startswith("/memory/it_test_1"):
            item = {
                **self.items["it_test_1"],
                "workspace_id": self.stamp,
                "tenant_id": "tn_test",
            }
            return httpx.Response(200, json=item, request=req)
        if method == "POST" and path == "/memory/search":
            return httpx.Response(200, json=list(self.items.values()), request=req)
        if method == "DELETE":
            self.items.pop("it_test_1", None)
            return httpx.Response(204, request=req)
        return httpx.Response(404, json={"detail": path}, request=req)


# (id, workspace the service stamps on the item, check passes)
_HOSTED_ROWS = [
    ("same-workspace", "ws_test_team", True),
    ("other-workspace", "ws_test_elsewhere", False),
]


@pytest.mark.parametrize("row", _HOSTED_ROWS, ids=[r[0] for r in _HOSTED_ROWS])
def test_round_trip_against_a_faked_hosted_api(world, monkeypatch, real_checks, row):
    _, stamp, ok = row
    hosted = _Hosted(stamp)
    monkeypatch.setattr(httpx, "get", hosted.get)
    monkeypatch.setattr(httpx, "request", hosted.request)
    monkeypatch.setenv("SMARTMEMORY_API_KEY", SYNTHETIC_KEY)
    config.save_config(
        _cfg(
            mode="remote",
            api_url="https://api.test.invalid",
            api_key_set=True,
            team_id="ws_test_team",
        )
    )
    check = setup_verify.round_trip({"mode": "remote"}, sleep=lambda s: None)
    assert check.ok is ok, check
    assert "tenant tn_test, workspace ws_test_team" in check.detail
    assert SYNTHETIC_KEY not in check.detail
    assert hosted.headers and set(hosted.headers) == {"ws_test_team"}
    assert hosted.items == {}  # the test memory was deleted
