"""WRAPPER-CONTRACT-130: the packaged mirror schema carries contract 1.3.0's defs.

What 1.3.0 changed versus the 1.2.0 copy (all additive, nothing the app writes got stricter):
ErrorCode gains PLAN_ENUMERATION_TOO_LARGE/UNAVAILABLE; MappingRow gains optional applied_lifecycle;
PlanMember gains optional prepared_effects/owned_incident_edges (new defs PreparedEffect,
PreparedEffectReference, OwnedIncidentEdge, PlanControlEndpoint); ReviewedMemberState, RunReport and
UnitResult change descriptions only. These tests pin the shapes the app produces and the new
server-side shapes it may receive.
"""

from pathlib import Path
import re

import pytest

from smartmemory_app.mirror_integrity import MirrorStateError, _validator, validate

APP = Path(__file__).resolve().parents[2] / "smartmemory_app"
HASH = "sha256:" + "0" * 64


def _codes_the_app_raises() -> set[str]:
    pattern = re.compile(r'(?:refuse|MirrorStateError)\(\s*"([A-Z_]+)"')
    return {
        code
        for path in APP.glob("*.py")
        for code in pattern.findall(path.read_text())
    }


def test_every_error_code_the_app_raises_is_a_contract_error_code():
    codes = _codes_the_app_raises()
    assert {"INVALID_REQUEST", "STORE_REPLACED", "DELETE_APPROVAL_REQUIRED"} <= codes
    assert not [c for c in sorted(codes) if not _validator("ErrorCode").is_valid(c)]


def test_contract_130_plan_enumeration_error_codes_are_known():
    for code in ("PLAN_ENUMERATION_TOO_LARGE", "PLAN_ENUMERATION_UNAVAILABLE"):
        assert _validator("ErrorCode").is_valid(code)
    assert not _validator("ErrorCode").is_valid("PLAN_ENUMERATION_PARTIAL")


def _ref(kind="memory", key="k1"):
    return {"kind": kind, "source_key": key}


def _reviewed(kind="memory", key="k1"):
    return {
        "artifact": _ref(kind, key),
        "current_hosted_hash": HASH,
        "current_presence": "present",
        "mirror_instance_id": None,
        "incident_reference_hash": HASH,
        "base_receipt_id": None,
    }


def _plan_member(**extra):
    return {
        "artifact": _ref(),
        "destination_id": "d1",
        "planned_outcome": "deleted",
        "estimated_new_charge": 0,
        "current_hosted_hash": HASH,
        "reviewed_state": _reviewed(),
        "incident_blockers": [],
        **extra,
    }


def _owned_edge():
    return {
        "artifact": _ref("edge", "e1"),
        "source": {"artifact": _ref()},
        "relation": "RELATES_TO",
        "target": {"opaque_key": "native:42"},
        "base": {
            "receipt_id": "r1",
            "source_hash": HASH,
            "hosted_hash": HASH,
            "hosted_presence": "present",
            "ownership": "owned",
        },
        "reviewed_state": _reviewed("edge", "e1"),
    }


def test_plan_member_from_a_1_2_0_server_still_validates():
    validate("PlanMember", _plan_member())


def test_plan_member_with_1_3_0_enumerations_validates():
    effects = [
        {"effect": "delete", "field_paths": [], "references": []},
        {
            "effect": "unlink",
            "field_paths": ["/payload/refs/0"],
            "references": [
                {"field_path": "/payload/refs/0", "target": {"opaque_key": "native:7"}}
            ],
        },
    ]
    validate(
        "PlanMember",
        _plan_member(prepared_effects=effects, owned_incident_edges=[_owned_edge()]),
    )


@pytest.mark.parametrize(
    "extra",
    [
        # unknown effect name
        {"prepared_effects": [{"effect": "purge", "field_paths": [], "references": []}]},
        # more than one each of unlink/retire/delete
        {"prepared_effects": [{"effect": e, "field_paths": [], "references": []} for e in ("delete", "retire", "unlink", "delete")]},
        # a control endpoint carrying content next to its identity
        {"owned_incident_edges": [{**_owned_edge(), "target": {"opaque_key": "native:42", "content": "x"}}]},
        # an owned-edge list may only carry owned mappings
        {"owned_incident_edges": [{**_owned_edge(), "base": {**_owned_edge()["base"], "ownership": "borrowed"}}]},
    ],
)
def test_plan_member_refuses_malformed_enumerations(extra):
    with pytest.raises(MirrorStateError):
        validate("PlanMember", _plan_member(**extra))


def _mapping_row(**extra):
    return {
        "pair_id": "p1",
        "artifact": _ref(),
        "destination_id": "d1",
        "physical_key": {"node_id": "n1"},
        "ownership": "owned",
        "head_receipt_id": None,
        "state": "present",
        "source_origin_claim": None,
        "mirror_instance_id": None,
        "deletion_eligible_incarnation": None,
        "charge_eligibility_consumed": False,
        "first_assessment_receipt_id": None,
        "latest_stage_receipt_id": None,
        **extra,
    }


def test_mapping_row_applied_lifecycle_is_optional_object_or_null():
    for extra in ({}, {"applied_lifecycle": None}, {"applied_lifecycle": {"tier": "warm"}}):
        validate("MappingRow", _mapping_row(**extra))
    with pytest.raises(MirrorStateError):
        validate("MappingRow", _mapping_row(applied_lifecycle="warm"))
