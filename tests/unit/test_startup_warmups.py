"""Unit coverage for concurrent daemon startup warmups."""

import threading

import pytest


def test_startup_warmups_overlap_and_report_truthful_completion():
    from smartmemory_app.storage import _run_startup_warmups

    rendezvous = threading.Barrier(2)
    lines: list[str] = []

    def ensure_spacy() -> None:
        rendezvous.wait(timeout=1)

    def require_embedding() -> None:
        rendezvous.wait(timeout=1)

    _run_startup_warmups(lines.append, ensure_spacy, require_embedding)

    assert lines[:2] == [
        "Loading language tools (spaCy)...",
        "Checking the local AI model...",
    ]
    assert len(lines) == 4
    assert any(line.startswith("Language tools ready (") for line in lines[2:])
    assert any(line.startswith("Local AI model ready (") for line in lines[2:])


def test_startup_warmups_reraise_one_failure_after_sibling_finishes():
    from smartmemory_app.storage import _run_startup_warmups

    rendezvous = threading.Barrier(2)
    sibling_finished = threading.Event()
    failure = RuntimeError("spaCy unavailable")
    lines: list[str] = []

    def fail_spacy() -> None:
        rendezvous.wait(timeout=1)
        raise failure

    def finish_embedding() -> None:
        rendezvous.wait(timeout=1)
        sibling_finished.set()

    with pytest.raises(RuntimeError) as exc_info:
        _run_startup_warmups(lines.append, fail_spacy, finish_embedding)

    assert exc_info.value is failure
    assert sibling_finished.is_set()
    assert not any(line.startswith("Language tools ready") for line in lines)
    assert any(line.startswith("Local AI model ready") for line in lines)


def test_startup_warmups_report_both_failures():
    from smartmemory_app.storage import _run_startup_warmups

    rendezvous = threading.Barrier(2)
    spacy_failure = RuntimeError("spaCy unavailable")
    embedding_failure = ValueError("embedding unavailable")

    def fail(error: Exception) -> None:
        rendezvous.wait(timeout=1)
        raise error

    with pytest.raises(ExceptionGroup) as exc_info:
        _run_startup_warmups(
            lambda _line: None,
            lambda: fail(spacy_failure),
            lambda: fail(embedding_failure),
        )

    assert exc_info.value.exceptions == (spacy_failure, embedding_failure)
