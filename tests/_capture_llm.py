"""Opt a test into the capture LLM path (HOOK-CAPTURE-NO-LLM-1).

WRAPPER-2.0-LLM-PROVIDER-NONE-PORT-1: ``SMARTMEMORY_CAPTURE_LLM=1`` alone no
longer reaches an LLM, because the default ``llm_provider = "none"`` wins over
it. A test that exercises the opt-in path also picks Groq in the config file,
the same way main's LLM-on rows set a groq config.
"""

from __future__ import annotations

from pathlib import Path

CAPTURE_LLM_ENV = "SMARTMEMORY_CAPTURE_LLM"
GROQ_CONFIG = '[smartmemory]\nmode = "local"\n\n[local]\nllm_provider = "groq"\n'


def write_groq_config(path: Path) -> Path:
    """Write a config that picks Groq at ``path`` (a config.toml) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GROQ_CONFIG)
    return path


def opt_in_capture_llm(monkeypatch) -> Path:
    """Set the capture opt-in and pick Groq in the config the wrapper reads."""
    from smartmemory_app.config import config_path

    monkeypatch.setenv(CAPTURE_LLM_ENV, "1")
    return write_groq_config(config_path())
