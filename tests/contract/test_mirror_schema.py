"""Always-on pins and optional checkout comparison for mirror contract drift."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "smartmemory_app/mirror_schema.json"
# Reviewed together against the authoritative contract, never generated in test.
CONTRACT_VERSION = "1.2.0"
SCHEMA_SHA256 = "ba6791d7a4732ffd11c0bb45986ab683546ced4967cad7c4f97641f056444c3a"


def test_packaged_mirror_schema_version_and_hash():
    assert CONTRACT_VERSION == "1.2.0"
    assert sha256(SCHEMA.read_bytes()).hexdigest() == SCHEMA_SHA256, (
        f"Packaged mirror schema drifted from contract {CONTRACT_VERSION}; "
        "review against the authoritative contract before updating the pin"
    )


def test_packaged_mirror_schema_matches_authoritative_contract():
    default = (
        REPO
        / "../../smart-memory-docs/docs/features/DIST-LITE-SYNC-1/mirror-contract.json"
    )
    override = os.environ.get("SMARTMEMORY_MIRROR_CONTRACT")
    candidates = ([Path(override)] if override else []) + [default]
    contract_path = next((path for path in candidates if path.is_file()), None)
    if contract_path is None:
        pytest.skip(
            "Authoritative mirror contract unavailable: neither SMARTMEMORY_MIRROR_CONTRACT nor ../../smart-memory-docs/docs/features/DIST-LITE-SYNC-1/mirror-contract.json exists"
        )
    contract = json.loads(contract_path.read_text())
    assert contract["contract_version"] == CONTRACT_VERSION
    assert json.loads(SCHEMA.read_text())["$defs"] == contract["$defs"]
