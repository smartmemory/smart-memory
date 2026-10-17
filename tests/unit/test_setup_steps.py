"""CLI-SETUP-UX-1: numbered setup steps, measured estimates, default local model none."""

from unittest.mock import MagicMock, PropertyMock, patch

import click
import pytest

from smartmemory_app import setup, setup_steps, tour
from smartmemory_app.setup_steps import (
    STEP_ESTIMATE_SECONDS,
    StepTracker,
    format_elapsed,
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


def test_every_plan_step_has_a_measured_estimate():
    for provider in ("local", "openai"):
        for restart in (False, True):
            for step in local_setup_plan(provider, restart=restart):
                assert step.key in STEP_ESTIMATE_SECONDS


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
    assert capsys.readouterr().out.startswith(
        "[1/1] SmartMemory started ... (about 5 seconds)"
    )


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
        # _setup_click builds the tracker and the daemon step is counted at its call site.
        setup._setup_click("local", None)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[")]
    starts = [ln for ln in lines if ln.endswith(")") and " ... (about " in ln]
    assert len(starts) == 9
    assert starts[0].startswith("[1/9] Saving your settings")
    assert starts[-1] == "[9/9] Starting SmartMemory ... (about 5 seconds)"
    assert sum(1 for ln in lines if " done (" in ln) == 9


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
    texts = [setup.NO_LOCAL_MODEL_NOTE]
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
        setup._setup_click,
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
