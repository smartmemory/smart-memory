"""CLI ``search --json`` output stays clean on every local encode path (F3-HOOK-VISIBILITY-TQDM-STDOUT-1).

The CLI sets the root logger to DEBUG for its file handler, and sentence-transformers turns its
tqdm bar on whenever its logger is at INFO/DEBUG. Core passes ``show_progress_bar=False`` at
every encode/predict site; these rows drive the real models through the real CLI and assert no
bar reaches the command's output. Under click < 8.2 ``CliRunner`` mixes stderr into stdout, so a
stderr bar broke ``json.loads(stdout)``; ``result.output`` carries stderr on every click version.
"""

import json
import shutil

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from smartmemory.models.memory_item import MemoryItem
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.plugins.embedding import EmbeddingService
from smartmemory.search.rerank import CrossEncoderReranker
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import cli, crash_reporter, hook_failures, local_api, storage
from smartmemory_app.config import LLM_KEY_ENV_VARS

TEXTS = {
    "test_tqdm_first": "Tool `Bash` called. Input: test_tqdm auth.py. Result: saved transcript",
    "test_tqdm_second": "Tool `Read` called. Input: test_tqdm auth.py. Result: file contents",
}
QUERY = "test_tqdm auth.py"
BAR_MARKERS = ("Batches", "Loading weights", "it/s]", "%|")

# (row id, mode, query, reranker, cold, model calls the row must exercise). ``cold`` empties the
# process-wide model caches so the CLI constructs both models inside the captured invoke, as a
# one-shot ``smartmemory search`` does on every run.
ROWS = [
    (
        "embed+rerank direct",
        "direct",
        QUERY,
        "cross-encoder",
        False,
        {"encode", "predict"},
    ),
    (
        "embed+rerank direct cold",
        "direct",
        QUERY,
        "cross-encoder",
        True,
        {"encode", "predict"},
    ),
    ("embed+rerank daemon", "daemon", QUERY, "cross-encoder", False, {"encode"}),
    ("embed only direct", "direct", QUERY, "none", False, {"encode"}),
    ("embed only daemon", "daemon", QUERY, "none", False, {"encode"}),
    ("wildcard direct", "direct", "*", "cross-encoder", False, set()),
]


@pytest.fixture
def reranker():
    """Overridden per row; read by ``memory`` because RerankerConfig reads the env at construction."""
    return "cross-encoder"


@pytest.fixture
def model_calls(monkeypatch):
    """Spy the real sentence-transformers entry points; calls still reach the models."""
    st = pytest.importorskip("sentence_transformers")
    calls = []
    for cls, name in ((st.SentenceTransformer, "encode"), (st.CrossEncoder, "predict")):
        real = getattr(cls, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, spy)
    return calls


@pytest.fixture
def memory(tmp_path, monkeypatch, reranker):
    root = tmp_path / "test_tqdm_isolated"
    root.mkdir()
    monkeypatch.setenv("SMARTMEMORY_RERANKER", reranker)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("SMARTMEMORY_MODE", "local")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    for key in LLM_KEY_ENV_VARS:
        monkeypatch.setenv(key, "")
    mem = None
    try:
        mem = create_lite_memory(
            str(root / "test_tqdm_data"),
            pipeline_profile=PipelineConfig.lite_hermetic(llm_enabled=False),
            spawn_worker=False,
        )
        # Two hits: the cross-encoder bypasses a pool of one (rerank.py ``bypass_small_pool``).
        for item_id, text in TEXTS.items():
            mem.add(
                MemoryItem(
                    item_id=item_id,
                    content=text,
                    memory_type="episodic",
                    origin="cli:add",
                )
            )
        monkeypatch.setattr(storage, "get_memory", lambda: mem)
        monkeypatch.setattr(local_api, "get_memory", lambda: mem)
        # CLI startup spawns daemon threads that flush the report outbox and consume hook-failure
        # markers. They outlive the test's per-test home patches and would claim queued entries in
        # the real ``~/.smartmemory/report-outbox``; they are unrelated to the output under test.
        monkeypatch.setattr(crash_reporter, "flush_in_background", lambda *a, **k: None)
        monkeypatch.setattr(
            hook_failures, "consume_in_background", lambda *a, **k: None
        )
        # Only bypass startup diagnostics; retrieval, models and HTTP routing remain real.
        monkeypatch.setattr(cli, "_prepare_direct_access", lambda **kwargs: None)
        yield mem
    finally:
        if mem is not None:
            mem.close()
        shutil.rmtree(root)


@pytest.mark.parametrize(
    ("mode", "query", "reranker", "cold", "expected_calls"),
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_search_json_output_has_no_progress_bar(
    memory, model_calls, monkeypatch, mode, query, cold, expected_calls
):
    if cold:
        monkeypatch.setattr(EmbeddingService, "_torch_models", {})
        monkeypatch.setattr(CrossEncoderReranker, "_models", {})
        monkeypatch.setattr(CrossEncoderReranker, "_attempted", set())
        monkeypatch.setattr(CrossEncoderReranker, "_loading", set())
    model_calls.clear()  # drop the fixture's own add-time encode
    with TestClient(local_api.api) as client:

        def daemon_request(method, path, **kwargs):
            response = client.request(method, path.removeprefix("/memory"), **kwargs)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(
            cli,
            "_daemon_request",
            daemon_request if mode == "daemon" else lambda *a, **k: None,
        )
        result = CliRunner().invoke(
            cli.cli, ["search", query, "--top-k", "10", "--json"]
        )

    assert result.exit_code == 0, result.output
    if not expected_calls <= set(model_calls):
        pytest.skip(
            f"encode path not exercised here (torch-free or uncached model): ran {sorted(set(model_calls))}"
        )
    assert {row["item_id"] for row in json.loads(result.stdout)} == set(TEXTS)
    bars = [marker for marker in BAR_MARKERS if marker in result.output]
    assert not bars, f"progress bar in CLI output ({bars}): {result.output[:300]!r}"
