"""CLI-SETUP-UX-1: numbered setup steps, measured estimates, default local model none."""

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import click
import pytest

from smartmemory_app import setup, setup_steps, tour
from smartmemory_app.setup_steps import (
    STEP_ESTIMATE_SECONDS,
    StepTracker,
    format_elapsed,
    estimate_phrase,
    format_estimate,
    local_setup_plan,
)

# (seconds, phrase after "about"): every band of format_estimate, the measured
# values from STEP_ESTIMATE_SECONDS' comment, and the band edges.
ESTIMATE_ROWS = [
    (1, "5 seconds"),
    (5, "5 seconds"),
    (6, "10 seconds"),
    (71, "70 seconds"),
    (89, "90 seconds"),
    (96, "2 minutes"),
    (150, "2 minutes"),
    (302, "5 minutes"),
]

ELAPSED_ROWS = [
    (0.2, "0s"),
    (12, "12s"),
    (59.6, "1m 00s"),
    (72, "1m 12s"),
    (605, "10m 05s"),
]

# (step number, label): step 0 is the intro, 1 to 7 are the arc.
TOUR_LABEL_ROWS = [(0, "Getting ready:"), (1, "[1/7]:"), (7, "[7/7]:")]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("seconds,phrase", ESTIMATE_ROWS)
def test_format_estimate_rows(seconds, phrase):
    assert format_estimate(seconds) == phrase


@pytest.mark.parametrize("seconds,text", ELAPSED_ROWS)
def test_format_elapsed_rows(seconds, text):
    assert format_elapsed(seconds) == text


# Steps with a row in measure-output.txt. restart and daemon have none on purpose:
# stop_daemon and _start_daemon_local touch launchd and cannot be timed against a
# throwaway HOME, so setup prints no estimate for them (only the real elapsed time).
MEASURED_KEYS = {
    "config",
    "spacy",
    "embedding",
    "extra_models",
    "hooks",
    "skills",
    "register",
    "seed",
}
UNMEASURED_KEYS = {"restart", "daemon"}


def test_only_measured_steps_carry_an_estimate():
    assert set(STEP_ESTIMATE_SECONDS) == MEASURED_KEYS
    for provider in ("local", "openai"):
        for restart in (False, True):
            keys = {s.key for s in local_setup_plan(provider, restart=restart)}
            assert keys <= MEASURED_KEYS | UNMEASURED_KEYS
            for step in local_setup_plan(provider, restart=restart):
                assert (step.estimate_seconds is None) == (step.key in UNMEASURED_KEYS)
                assert (estimate_phrase(step) == "") == (step.key in UNMEASURED_KEYS)


def test_plan_shape():
    local = [s.name for s in local_setup_plan("local")]
    remote_embed = [s.name for s in local_setup_plan("openai", restart=True)]
    assert local[0] == "Config written" and local[-1] == "SmartMemory started"
    assert "Embedding model ready" in local
    assert "Embedding model ready" not in remote_embed
    assert "SmartMemory stopped" in remote_embed


def test_tracker_prints_counter_estimate_and_elapsed(capsys):
    clock = FakeClock()
    tracker = StepTracker(clock=clock)
    tracker.set_plan(local_setup_plan("local"))
    with tracker.step("Config written", live=False):
        clock.now += 2
    with tracker.step("spaCy model ready"):
        clock.now += 72
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[1/9] Saving your settings ... (about 5 seconds)",
        "[1/9] Saving your settings done (2s)",
        "[2/9] Downloading the language model ... (about 70 seconds)",
        "[2/9] Downloading the language model done (1m 12s)",
    ]


def test_tracker_failure_line_and_reraise(capsys):
    clock = FakeClock()
    tracker = StepTracker(clock=clock)
    tracker.set_plan(local_setup_plan("local"))
    with pytest.raises(click.ClickException):
        with tracker.step("spaCy model ready"):
            clock.now += 3
            raise click.ClickException("no network")
    captured = capsys.readouterr()
    assert "[2/9] Downloading the language model failed after 3s" in captured.err
    assert "done" not in captured.out


def test_tracker_counts_a_step_outside_the_plan(capsys):
    tracker = StepTracker(clock=FakeClock())
    with tracker.step("SmartMemory started", live=False):
        pass
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[1/1] SmartMemory started ..."  # no measurement, no estimate
    assert out[1].startswith("[1/1] SmartMemory started done (")


def test_tracker_streams_log_lines_plain_off_a_terminal(capsys):
    tracker = StepTracker(clock=FakeClock())
    tracker.set_plan(local_setup_plan("local"))
    with tracker.step("Embedding model ready") as log:
        log("Downloading the local AI model: 12.0 MB of 90.0 MB (13%)")
    out = capsys.readouterr().out
    assert "  Downloading the local AI model: 12.0 MB of 90.0 MB (13%)" in out


def _patched_setup(tmp_path, prompts, embedding="local"):
    return (
        patch("smartmemory_app.setup._ensure_spacy"),
        patch("smartmemory_app.setup._ensure_embedding_model"),
        patch("smartmemory_app.setup._ensure_lazy_models"),
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.config.save_config"),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("click.confirm", return_value=False),
        patch("click.prompt", side_effect=prompts),
    )


def _enter(stack, patches):
    return [stack.enter_context(p) for p in patches]


def test_click_setup_numbers_every_step_including_the_daemon(tmp_path, capsys):
    from contextlib import ExitStack

    prompts = ["none", "local", "sm", str(tmp_path)]
    with ExitStack() as stack:
        _enter(stack, _patched_setup(tmp_path, prompts))
        stack.enter_context(
            patch(
                "smartmemory_app.setup._start_daemon_local",
                return_value={"status": "ok"},
            )
        )
        # _setup_click builds the tracker; the daemon step and the two checks after it
        # (SETUP-RESUMABLE-1) share its counter.
        setup._setup_click("local", None)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[")]
    starts = [ln for ln in lines if ln.endswith(" ...") or " ... (about " in ln]
    assert len(starts) == 11
    assert starts[0].startswith("[1/11] Saving your settings")
    assert starts[8] == "[9/11] Starting SmartMemory ..."  # no measurement, no estimate
    assert starts[9] == "[10/11] Checking which mode the CLI runs in ..."
    assert starts[-1] == "[11/11] Saving and recalling a test memory ..."
    assert sum(1 for ln in lines if " done (" in ln) == 11


def test_apply_setup_result_reports_every_plan_step_in_order(tmp_path):
    steps = []
    with (
        patch("smartmemory_app.setup._ensure_spacy"),
        patch("smartmemory_app.setup._ensure_embedding_model"),
        patch("smartmemory_app.setup._ensure_lazy_models"),
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.config.save_config"),
    ):
        setup._apply_setup_result(
            setup.SetupResult(data_dir=str(tmp_path)), on_step=steps.append
        )
    planned = [s.name for s in local_setup_plan("local")]
    assert steps == planned[:-1]  # the daemon step is reported by its own caller


def test_default_local_model_is_none(monkeypatch, capsys):
    monkeypatch.setattr(click.termui, "visible_prompt_func", lambda _prompt: "")
    assert setup._prompt_local_model("ollama") == ""
    out = capsys.readouterr().out
    assert "Default: none" in out
    assert "sm config llm_model <model name>" in out


def test_named_local_model_is_kept(monkeypatch):
    monkeypatch.setattr(
        click.termui, "visible_prompt_func", lambda _prompt: "llama3.1:8b"
    )
    assert setup._prompt_local_model("lmstudio") == "llama3.1:8b"


def test_setup_local_with_ollama_enter_stores_no_model(tmp_path):
    from contextlib import ExitStack

    saved = []
    prompts = ["ollama", "none", "local", "sm", str(tmp_path)]
    with ExitStack() as stack:
        _enter(stack, _patched_setup(tmp_path, prompts))
        stack.enter_context(
            patch("smartmemory_app.config.save_config", side_effect=saved.append)
        )
        setup._setup_local()
    assert saved[0].llm_provider == "ollama"
    assert saved[0].llm_model == ""


def test_model_screen_none_row_clears_the_model():
    from smartmemory_app.setup_tui import NO_MODEL_ID, ModelScreen

    screen = ModelScreen("ollama")
    app = MagicMock()
    app._result = setup.SetupResult(llm_model="stale")
    with patch.object(ModelScreen, "app", new_callable=PropertyMock, return_value=app):
        event = MagicMock()
        event.option.id = NO_MODEL_ID
        screen.on_option_list_option_selected(event)
        assert app._result.llm_model == ""
        event.option.id = "llama3.1:8b"
        screen.on_option_list_option_selected(event)
        assert app._result.llm_model == "llama3.1:8b"


def test_progress_screen_rows_carry_counter_estimate_and_elapsed(monkeypatch):
    from smartmemory_app.setup_tui import ProgressScreen

    screen = ProgressScreen()
    screen._plan = local_setup_plan("local")
    widgets = {}

    def query_one(selector, _kind=None):
        return widgets.setdefault(selector, MagicMock())

    clock = iter([100.0, 172.0])
    monkeypatch.setattr("smartmemory_app.setup_tui.time.monotonic", lambda: next(clock))
    screen._step_started = 0.0
    with patch.object(screen, "query_one", side_effect=query_one):
        screen._mark_step_done("spaCy model ready")
    done = widgets["#step-spacy"].update.call_args.args[0]
    assert "[2/9]" in done and "spaCy model ready" in done and "took 1m 40s" in done
    running = widgets["#step-embedding"].update.call_args.args[0]
    assert "[3/9]" in running and "about 2 minutes" in running


@pytest.mark.parametrize("step,label", TOUR_LABEL_ROWS)
def test_tour_step_label(step, label):
    assert tour.tour_step_label(step) == label


def test_tour_counter_total_matches_the_arc():
    assert tour.TOUR_STEP_COUNT == 7


def test_user_facing_strings_have_no_em_dash_or_semicolon():
    from smartmemory_app import config
    from smartmemory_app.console import NO_TERMINAL_MESSAGE

    texts = [
        setup.NO_LOCAL_MODEL_NOTE,
        config.NO_LOCAL_MODEL_OFF_LINE,
        NO_TERMINAL_MESSAGE,
    ]
    for provider in ("local", "openai"):
        for step in local_setup_plan(provider, restart=True):
            texts += [step.name, step.title]
    texts += [format_estimate(s) for s, _ in ESTIMATE_ROWS]
    for text in texts:
        assert "\u2014" not in text and ";" not in text, text
    assert setup_steps.__doc__ is not None


def _user_strings(func):
    """String constants a function passes around, minus its docstring."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    doc = ast.get_docstring(node, clean=False)
    out = [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc
    ]
    return out


def test_choice_screens_and_prompts_have_no_em_dash_or_semicolon():
    from smartmemory_app import setup_tui

    texts = [d for _, d in setup_tui.LLM_PROVIDERS] + [
        d for _, d in setup_tui.EMBEDDING_PROVIDERS
    ]
    texts.append(setup_tui.NO_MODEL_LABEL)
    for func in (
        setup._setup_local,
        setup._ask_local_choices,  # SETUP-RESUMABLE-1: the prompts moved here
        setup._setup_click,
        setup._finish_local_run,
        setup._resume_setup,
        setup._verify_after_tui,
        setup._remote_login,
        setup._prompt_local_model,
        setup_tui.WelcomeScreen.compose,
        setup_tui.EmbeddingScreen.compose,
        setup_tui.ProgressScreen._run_setup.__wrapped__,
    ):
        texts += _user_strings(func)
    for text in texts:
        assert "\u2014" not in text and ";" not in text, text


def test_download_bar_accepts_the_tqdm_display_calls_hf_xet_makes():
    from smartmemory_app.hf_progress import DownloadProgressReporter

    lines = []
    reporter = DownloadProgressReporter(lines.append, interval=0)
    bar = reporter.new_bar(total=100)
    bar.set_postfix_str("x")
    bar.set_transfer_postfix_str("x")
    bar.set_description("x")
    bar.update_transfer(5)
    bar.refresh()
    bar.clear()
    bar.update(50)
    assert bar.pos == 0 and "50%" in lines[-1]


# CLI-SETUP-UX-FU-F2: every progress event huggingface_hub sends to a bar either moves
# the shown bytes or carries speed text the log line keeps. One row per event kind.
# (id, calls on a 1000-byte bar, text the last log line holds, text it must not hold)
_XET_EVENTS = [
    ("transfer-only", [("update_transfer", 80)], "80 bytes of 1000 bytes (8%)", "B/s"),
    (
        "transfer-ahead-of-reconstruction",
        [("update_transfer", 800), ("update", 50)],
        "800 bytes of 1000 bytes (80%)",
        "",
    ),
    (
        "reconstruction-finishes",
        [("update_transfer", 800), ("update", 1000)],
        "(100%)",
        "",
    ),
    # a plain HTTP download reports the same bytes through both calls: never summed
    (
        "http-same-bytes-twice",
        [("update", 100), ("update_transfer", 100)],
        "100 bytes of 1000 bytes (10%)",
        "200 bytes",
    ),
    (
        "transfer-speed-kept",
        [("set_transfer_postfix_str", "  1.5MB/s  "), ("update_transfer", 80)],
        "(8%), 1.5MB/s",
        "",
    ),
    (
        "reconstruction-speed-kept",
        [("set_postfix_str", "  2.0MB/s  "), ("update", 10)],
        "(1%), 2.0MB/s",
        "",
    ),
    (
        "transfer-speed-wins",
        [
            ("set_transfer_postfix_str", "1.5MB/s"),
            ("set_postfix_str", "9.9MB/s"),
            ("update_transfer", 80),
        ],
        ", 1.5MB/s",
        "9.9MB/s",
    ),
    (
        "unknown-speed-dropped",
        [("set_transfer_postfix_str", "???B/s"), ("update_transfer", 80)],
        "(8%)",
        "?",
    ),
    (
        "display-only-calls-change-nothing",
        [("update", 10), ("set_description", "x"), ("refresh",), ("clear",)],
        "10 bytes of 1000 bytes (1%)",
        "B/s",
    ),
]


@pytest.mark.parametrize("case", _XET_EVENTS, ids=[c[0] for c in _XET_EVENTS])
def test_download_bar_counts_every_progress_event(case):
    from smartmemory_app.hf_progress import DownloadProgressReporter

    _id, calls, holds, lacks = case
    lines = []
    bar = DownloadProgressReporter(lines.append, interval=0).new_bar(total=1000)
    for method, *args in calls:
        getattr(bar, method)(*args)
    assert holds in lines[-1], lines
    assert not lacks or lacks not in lines[-1], lines


def test_real_xet_reporter_transfer_update_shows_progress():
    """The reviewer's probe P5: the installed Xet reporter, transfer bytes only."""
    import logging
    from types import SimpleNamespace

    from huggingface_hub.utils._xet_progress_reporting import (
        XetDownloadProgressReporter,
    )

    from smartmemory_app.hf_progress import DownloadProgressReporter

    lines = []
    bar = DownloadProgressReporter(lines.append, interval=0).new_bar(total=1000)
    xet = XetDownloadProgressReporter(
        reconstruction_desc="model",
        log_level=logging.INFO,
        external_reconstruction_bar=bar,
    )

    def report(transferred):
        xet.update_progress(
            SimpleNamespace(
                total_bytes_completed=0,
                total_transfer_bytes_completed=transferred,
                total_bytes=1000,
                total_bytes_completion_rate=None,
                total_transfer_bytes_completion_rate=1.5e6,
            )
        )

    before = len(lines)
    report(80)
    assert len(lines) > before and "80 bytes" in lines[-1], lines
    # Xet sends the speed text after the byte update, so it shows on the next line.
    report(160)
    assert "160 bytes" in lines[-1] and "B/s" in lines[-1], lines


# ── fix round 1: setup promises only what it measured and does what the user chose ──

CLEAN_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "SMARTMEMORY_LLM_MODEL",
    "SMARTMEMORY_LLM_PROVIDER",
    "SMARTMEMORY_MODE",
)

# (provider, saved model, key in the environment, extraction on). One row per case:
# both local providers with none (the reviewer's P2), none with a real key already in
# the environment (the key must not switch a local provider with no model back on),
# a named model, and a cloud provider that never had a model.
LOCAL_MODEL_ROWS = [
    ("ollama", "", None, False),
    ("lmstudio", "", None, False),
    ("ollama", "", ("OPENAI_API_KEY", "sk-real"), False),
    ("lmstudio", "llama3.1:8b", None, True),
    ("groq", "", ("GROQ_API_KEY", "gsk-real"), True),
]


@pytest.mark.parametrize("provider,model,key,enabled", LOCAL_MODEL_ROWS)
def test_no_local_model_means_no_llm_at_runtime(
    tmp_path, monkeypatch, provider, model, key, enabled
):
    import os

    from smartmemory_app import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with patch.dict(os.environ):
        for name in CLEAN_ENV_KEYS:
            os.environ.pop(name, None)
        if key:
            os.environ[key[0]] = key[1]
        with (
            patch("smartmemory_app.setup._ensure_spacy"),
            patch("smartmemory_app.setup._ensure_embedding_model"),
            patch("smartmemory_app.setup._ensure_lazy_models"),
            patch("smartmemory_app.setup._copy_hooks"),
            patch("smartmemory_app.setup._copy_skills"),
            patch("smartmemory_app.setup._register_hooks"),
            patch("smartmemory_app.setup._seed_data_dir"),
        ):
            setup._apply_setup_result(
                setup.SetupResult(
                    llm_provider=provider,
                    llm_model=model,
                    data_dir=str(tmp_path / "data"),
                )
            )
        loaded = config.load_config()
        assert loaded.llm_model == model
        assert config.llm_key_present() is enabled
        assert (os.environ.get("SMARTMEMORY_LLM_MODEL") or "") == model
        if not enabled:
            # the placeholder key that flips extraction on was not minted
            assert os.environ.get("OPENAI_API_KEY") == (key[1] if key else None)
        assert config.local_llm_without_model(loaded) is (
            provider in ("ollama", "lmstudio") and not model
        )


# (provider, saved model, ambient route, Agent SDK installed, extraction on). One row per
# case of review r2 H2: the off flag is not enough, core's route resolver decides whether a
# Lite save queues LLM extraction. Both local providers with none, an ambient key and the
# Agent SDK (neither is a model choice), then a named model and a cloud provider, which
# must behave as at 8a45ca4.
NO_MODEL_RUNTIME_ROWS = [
    ("ollama", "", None, False, False),
    ("lmstudio", "", None, False, False),
    ("ollama", "", "OPENAI_API_KEY", False, False),
    ("lmstudio", "", None, True, False),
    ("ollama", "llama3.2", None, False, True),
    ("groq", "", "GROQ_API_KEY", False, True),
]


@pytest.mark.parametrize(
    "provider,model,ambient,agent_sdk,enabled", NO_MODEL_RUNTIME_ROWS
)
def test_no_local_model_queues_no_llm_extraction(
    tmp_path, monkeypatch, capsys, provider, model, ambient, agent_sdk, enabled
):
    import os
    import socket
    import sqlite3

    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.pipeline.state import PipelineState
    from smartmemory.pipeline.work_graph.lite_save import save_to_work_graph
    from smartmemory.pipeline.work_graph.sqlite_store import SQLiteWorkGraph
    from smartmemory.scope_provider import DefaultScopeProvider
    from smartmemory.utils import llm as core_llm

    from smartmemory_app import config, storage, viewer_server

    # Teardown restores core's resolver, so the hold never outlives this test.
    monkeypatch.setattr(core_llm, "llm_route_available", core_llm.llm_route_available)
    unheld = getattr(
        core_llm.llm_route_available, "__wrapped__", core_llm.llm_route_available
    )
    monkeypatch.setattr(core_llm, "_claude_agent_sdk_available", lambda: agent_sdk)
    attempts = []

    def no_network(sock, address):
        attempts.append(address)
        raise OSError("network blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class ForegroundRunner:
        # CORE-INGEST-SPLIT-1: core reads runner.stages (pre_store_stage) to split prepare
        # from apply; run_to is the prepare half, run_from the apply half.
        stages = [SimpleNamespace(name=n) for n in ("classify", "store")]

        def run_to(self, **kwargs):
            return PipelineState(item_id="test_setupux_none", text="t", entity_ids={})

        def run_from(self, state, *args, **kwargs):
            return state

    with patch.dict(os.environ):
        for name in CLEAN_ENV_KEYS + ("LLM_PROVIDER", "LLM_API_KEY", "OPENAI_MODEL"):
            os.environ.pop(name, None)
        if ambient:
            os.environ[ambient] = "synthetic-test-key"
        with (
            patch("smartmemory_app.setup._ensure_spacy"),
            patch("smartmemory_app.setup._ensure_embedding_model"),
            patch("smartmemory_app.setup._ensure_lazy_models"),
            patch("smartmemory_app.setup._copy_hooks"),
            patch("smartmemory_app.setup._copy_skills"),
            patch("smartmemory_app.setup._register_hooks"),
            patch("smartmemory_app.setup._seed_data_dir"),
        ):
            setup._apply_setup_result(
                setup.SetupResult(
                    llm_provider=provider,
                    llm_model=model,
                    data_dir=str(tmp_path / "data"),
                )
            )
        storage.apply_runtime_config()
        route_on = core_llm.llm_route_available()[0]
        capsys.readouterr()
        viewer_server._print_llm_extraction_status()
        boot = capsys.readouterr().out
        db = tmp_path / "test_setupux_work.db"
        _, result = save_to_work_graph(
            ForegroundRunner(),
            text="t",
            config=PipelineConfig.lite(),
            metadata={},
            work_graph=SQLiteWorkGraph(str(db)),
            data_dir=None,
            scope_provider=DefaultScopeProvider(single_tenant=True),
            enable_ontology=False,
        )
        with sqlite3.connect(db) as conn:
            kinds = {row[0] for row in conn.execute("SELECT kind FROM work_nodes")}
        wrapper_env = {
            name: os.environ.get(name)
            for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "SMARTMEMORY_LLM_MODEL")
        }
        unheld_route = unheld()[0]

    assert route_on is enabled
    assert ("extract" in kinds) is enabled
    assert ("LLM extraction: enabled (deferred)" in boot) is enabled
    assert (config.NO_LOCAL_MODEL_OFF_LINE in boot) is not enabled
    assert attempts == []
    if provider == "ollama" and model:
        assert wrapper_env == {
            "OPENAI_BASE_URL": "http://localhost:11434/v1",
            "OPENAI_API_KEY": "local-llm-no-key",
            "SMARTMEMORY_LLM_MODEL": model,
        }
    elif not enabled:
        # Nothing the wrapper set is a route: no endpoint, no placeholder, no model.
        assert wrapper_env["OPENAI_BASE_URL"] is None
        assert wrapper_env["SMARTMEMORY_LLM_MODEL"] is None
        assert wrapper_env["OPENAI_API_KEY"] == (
            "synthetic-test-key" if ambient == "OPENAI_API_KEY" else None
        )
        # and core, asked without the hold, finds none either unless one is ambient.
        assert unheld_route is bool(ambient or agent_sdk)
    else:
        # A cloud provider pins its default model (TOUR-ASK-502-1), no endpoint or key.
        assert wrapper_env == {
            "OPENAI_BASE_URL": None,
            "OPENAI_API_KEY": None,
            "SMARTMEMORY_LLM_MODEL": "openai/gpt-oss-120b",
        }


def test_click_setup_ends_with_the_how_to_turn_it_on_line(tmp_path, capsys):
    from contextlib import ExitStack

    from smartmemory_app.config import NO_LOCAL_MODEL_OFF_LINE, SmartMemoryConfig

    with ExitStack() as stack:
        _enter(
            stack,
            _patched_setup(tmp_path, ["ollama", "none", "local", "sm", str(tmp_path)]),
        )
        stack.enter_context(
            patch(
                "smartmemory_app.setup._start_daemon_local",
                return_value={"status": "ok"},
            )
        )
        stack.enter_context(
            patch(
                "smartmemory_app.config.load_config",
                return_value=SmartMemoryConfig(llm_provider="ollama", llm_model=""),
            )
        )
        setup._setup_click("local", None)
    out = capsys.readouterr().out.splitlines()
    assert NO_LOCAL_MODEL_OFF_LINE in out


# (daemon running when the questions end, running after the downloads, stop runs).
# The base flow asked after the downloads, so the second answer decides.
DAEMON_RACE_ROWS = [
    (False, False, False),
    (True, True, True),
    (False, True, True),  # started during the downloads: the reviewer's P3 race
    (True, False, False),  # stopped during the downloads: nothing left to stop
]


@pytest.mark.parametrize("before,after,stops", DAEMON_RACE_ROWS)
def test_daemon_state_is_checked_after_the_downloads(
    tmp_path, capsys, before, after, stops
):
    from contextlib import ExitStack

    prompts = ["none", "local", "sm", str(tmp_path)]
    with ExitStack() as stack:
        _enter(stack, _patched_setup(tmp_path, prompts))
        stack.enter_context(
            patch("smartmemory_app.daemon.is_running", side_effect=[before, after])
        )
        stop = stack.enter_context(patch("smartmemory_app.daemon.stop_daemon"))
        stack.enter_context(patch("time.sleep"))
        setup._setup_local(StepTracker(clock=FakeClock()))
    assert stop.call_count == int(stops)
    out = capsys.readouterr().out
    assert ("Stopping the running SmartMemory ..." in out) is stops


# (stdin, stdout, stderr) is a terminal -> a full-screen app may start.
TERMINAL_ROWS = [
    ((True, True, True), True),
    ((False, True, True), False),
    ((True, False, True), False),
    ((True, True, False), False),  # stderr piped: the tour wrote 863 ESC bytes here
    ((False, False, False), False),
]


def _streams(monkeypatch, flags):
    for name, tty in zip(("stdin", "stdout", "stderr"), flags):
        monkeypatch.setattr(f"sys.{name}", MagicMock(isatty=lambda tty=tty: tty))


@pytest.mark.parametrize("flags,expected", TERMINAL_ROWS)
def test_interactive_terminal_needs_all_three_streams(monkeypatch, flags, expected):
    from smartmemory_app.console import interactive_terminal

    _streams(monkeypatch, flags)
    assert interactive_terminal() is expected


@pytest.mark.parametrize("flags,expected", TERMINAL_ROWS)
def test_setup_tui_needs_all_three_streams(monkeypatch, flags, expected):
    _streams(monkeypatch, flags)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert setup._can_run_tui() is expected


def test_tour_and_explorer_print_one_plain_line_off_a_terminal(monkeypatch, capsys):
    from smartmemory_app import cli
    from smartmemory_app.tui import app as explore_app

    # pytest's own captured streams are not terminals, so no stream patching is needed.
    monkeypatch.setattr(
        tour, "TourSessionRunner", MagicMock(side_effect=AssertionError)
    )
    monkeypatch.setattr(
        explore_app, "ExploreApp", MagicMock(side_effect=AssertionError)
    )
    assert tour.run_tour(code=True, no_viewer=True) is None
    cli.explore_cmd.callback(None)
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert "\x1b" not in text and "\r" not in text
    assert captured.out.splitlines() == [
        "The guided tour needs a terminal. Run it directly in a terminal window.",
        "The graph explorer needs a terminal. Run it directly in a terminal window.",
    ]


def test_progress_row_has_no_estimate_for_the_daemon_step(monkeypatch):
    from smartmemory_app.setup_tui import ProgressScreen

    screen = ProgressScreen()
    screen._plan = local_setup_plan("local")
    widgets = {}
    monkeypatch.setattr("smartmemory_app.setup_tui.time.monotonic", lambda: 7.0)
    screen._daemon_started_at = 0.0
    with patch.object(
        screen,
        "query_one",
        side_effect=lambda sel, _k=None: widgets.setdefault(sel, MagicMock()),
    ):
        screen._update_daemon_elapsed()
    row = widgets["#step-daemon"].update.call_args.args[0]
    assert "[9/9]" in row and "(7s)" in row and "about" not in row
