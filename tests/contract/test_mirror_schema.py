"""Always-on pins and optional checkout comparison for mirror contract drift."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "smartmemory_app/mirror_schema.json"
# Reviewed together against the authoritative contract, never generated in test.
# CLAIMS-20-FIX2: DecisionBody also carries the contract's optional decided_by/recommended_by (additive, so the
# label stays); the other post-1.2.0 contract defs are not vendored here yet.
CONTRACT_VERSION = "1.2.0"
SCHEMA_SHA256 = "ba7e57a7dede62be8fa4bc81285f9b0e9f13d6b24cb01b399008225d26bb34f6"


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


def test_packaged_decision_body_carries_the_optional_who_fields():
    """CLAIMS-20-FIX2: a Lite decision that names who decided/recommended passes local unit validation."""
    from smartmemory_app.mirror_integrity import MirrorStateError, validate

    body = json.loads(SCHEMA.read_text())["$defs"]["DecisionBody"]
    assert not {"decided_by", "recommended_by"} & set(body["required"])
    for name in ("decided_by", "recommended_by"):
        assert body["properties"][name] == {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": body["properties"][name]["description"],
        }
    assert body["additionalProperties"] is False
    with pytest.raises(MirrorStateError):
        validate("DecisionBody", {"decided_by": ""})
