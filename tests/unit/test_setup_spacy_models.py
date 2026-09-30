"""Setup prepares the selected spaCy model and the background worker default."""

from unittest.mock import patch

import click
import pytest
from smartmemory.errors import MissingModelError

from smartmemory_app import setup


@pytest.mark.parametrize("path", ["tui", "click"])
@pytest.mark.parametrize(
    "size, expected",
    [
        ("md", ["en_core_web_md", "en_core_web_sm"]),
        ("lg", ["en_core_web_lg", "en_core_web_sm"]),
        ("sm", ["en_core_web_sm"]),
    ],
)
def test_setup_ensures_selected_and_worker_spacy_models(tmp_path, path, size, expected):
    calls = []
    with (
        patch(
            "smartmemory.tools.factory._ensure_spacy_model", side_effect=calls.append
        ),
        patch("smartmemory_app.setup._ensure_embedding_model"),
        patch("smartmemory_app.setup._copy_hooks"),
        patch("smartmemory_app.setup._copy_skills"),
        patch("smartmemory_app.setup._register_hooks"),
        patch("smartmemory_app.setup._seed_data_dir"),
        patch("smartmemory_app.config.save_config"),
        patch("smartmemory_app.daemon.is_running", return_value=False),
        patch("click.confirm", return_value=False),
        patch("click.prompt", side_effect=["none", "local", size, str(tmp_path)]),
    ):
        if path == "tui":
            setup._apply_setup_result(
                setup.SetupResult(spacy_model=f"en_core_web_{size}")
            )
        else:
            setup._setup_local()
    assert calls == expected


@pytest.mark.parametrize("failed_model", ["en_core_web_md", "en_core_web_sm"])
def test_spacy_setup_surfaces_core_error_unchanged(failed_model):
    failure = MissingModelError(f"Could not install {failed_model}. Run sm setup.")

    def install(model):
        if model == failed_model:
            raise failure

    with (
        patch("smartmemory.tools.factory._ensure_spacy_model", side_effect=install),
        pytest.raises(click.ClickException) as caught,
    ):
        setup._ensure_spacy("en_core_web_md")
    assert str(caught.value) == str(failure)
    assert caught.value.__cause__ is failure
