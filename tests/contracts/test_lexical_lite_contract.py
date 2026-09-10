"""Collect the single shared L-D3 replay against this wrapper checkout."""

from pathlib import Path
from runpy import run_path

_core = Path(__file__).resolve().parents[3] / "smart-memory-core"
test_lite_lexical_fill_and_forwarding_contract = run_path(
    str(_core / "tests/remediation/test_lexical_index.py")
)["test_lite_lexical_fill_and_forwarding_contract"]
