"""Validate frozen U1 projections and U2 persistence against the filed contract.

This module does not select/project source data. It verifies already-normalized
payloads, full inventories and expanded operations at the persistence boundary.
mirror_schema.json is an exact copy of the filed contract's $defs.
"""

from copy import deepcopy
from hashlib import sha256
from functools import lru_cache
import json
import logging
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from smartmemory.okf.resource import build_resource, parse_resource
import rfc8785

log = logging.getLogger(__name__)
_SCHEMA = json.loads(Path(__file__).with_name("mirror_schema.json").read_text())


class MirrorStateError(RuntimeError):
    """Fail-closed local protocol error, using contract ErrorCode values."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def refuse(code: str, detail: str):
    log.warning("Mirror refused: %s: %s", code, detail)
    raise MirrorStateError(code, detail)


@lru_cache(maxsize=None)
def _validator(name: str):
    return Draft202012Validator(
        {**_SCHEMA, "$ref": f"#/$defs/{name}"}, format_checker=FormatChecker()
    )


def validate(name: str, value: dict) -> None:
    error = next(_validator(name).iter_errors(value), None)
    if error:
        # Do not echo values (payloads or accidental credentials) into diagnostics.
        refuse(
            "INVALID_REQUEST", f"invalid {name} at /{'/'.join(map(str, error.path))}"
        )

    normalized_sets(value)


def normalized_sets(value) -> None:
    """Reject non-normalized contract sets without touching arbitrary JSON data."""
    if isinstance(value, list):
        for child in value:
            normalized_sets(child)
    elif isinstance(value, dict):
        for field in ("depends_on_operations", "required_properties", "dependencies"):
            items = value.get(field)
            if items is None:
                continue
            keys = [key(item) for item in items] if field == "dependencies" else items
            if keys != sorted(set(keys)):
                refuse("INVALID_REQUEST", f"non-normalized set: {field}")
        # Only descend through protocol structure, never user JSON properties,
        # document extensions, examples, or declaration property schemas.
        for field in (
            "snapshots",
            "pending",
            "manifest",
            "entries",
            "unit",
            "members",
            "frozen_payloads",
            "payload",
            "declaration",
        ):
            if field in value:
                normalized_sets(value[field])


def canonical(value) -> bytes:
    """JCS with the contract's additional integer-valued binary64 bound."""

    def numbers(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if int(v) == v and abs(v) > 9007199254740991:
                raise ValueError("unsafe integer")
        elif isinstance(v, dict):
            for child in v.values():
                numbers(child)
        elif isinstance(v, list):
            for child in v:
                numbers(child)

    try:
        numbers(value)
        return rfc8785.dumps(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MirrorStateError("INVALID_PAYLOAD", "noncanonical JSON value") from exc


def digest(value) -> str:
    return "sha256:" + sha256(canonical(value)).hexdigest()


def key(value) -> tuple[str, str]:
    artifact = value.get("artifact", value)
    return artifact["kind"], artifact["source_key"]


def source_hash(blob: dict) -> str:
    normalized_sets(blob)
    return digest(
        {
            "domain": "lite-mirror/source/v1",
            **blob["artifact"],
            "payload": blob["payload"],
        }
    )


def manifest_root(manifest: dict) -> str:
    normalized_sets(manifest)
    h = manifest["header"]
    return digest(
        {
            "domain": "lite-mirror/manifest/v1",
            **{k: h[k] for k in ("source_checkpoint", "projection", "pair_generation")},
            "entries": sorted(manifest["entries"], key=key),
        }
    )


def group_hash(unit: dict) -> str:
    normalized_sets(unit)
    return digest(
        {
            "domain": "lite-mirror/group/v1",
            "members": [
                {
                    "artifact": m["artifact"],
                    "source_hash": m["source_hash"],
                    "dependencies": sorted(m["dependencies"], key=key),
                }
                for m in sorted(unit["members"], key=key)
            ],
        }
    )


def operation_hash(snapshot: dict, unit: dict) -> str:
    normalized_sets(unit)
    header = snapshot["manifest"]["header"]
    return digest(
        {
            "domain": "lite-mirror/operation/v1",
            "pair_id": snapshot["pair_id"],
            "server_epoch": snapshot["server_epoch"],
            **{
                k: header[k]
                for k in ("source_checkpoint", "pair_generation", "projection")
            },
            "unit": {k: v for k, v in unit.items() if k != "operation_hash"},
        }
    )


def expand(snapshot: dict, stored: dict) -> dict:
    unit = deepcopy(stored)
    blobs = {b["source_hash"]: b for b in snapshot["frozen_payloads"]}
    for member in unit["members"]:
        if member["action"] == "put":
            ref = member.pop("payload_ref")
            blob = blobs.get(ref)
            if (
                not blob
                or ref != member["source_hash"]
                or blob["artifact"] != member["artifact"]
            ):
                refuse(
                    "SNAPSHOT_CONTEXT_MISSING", "missing or mismatched frozen payload"
                )
            member["payload"] = deepcopy(blob["payload"])
    return unit


def freeze_unit(unit: dict) -> dict:
    """Reference payloads already present in a complete PendingSnapshot."""
    validate("ApplyUnit", unit)
    result = deepcopy(unit)
    for member in result["members"]:
        if member["action"] == "put":
            del member["payload"]
            member["payload_ref"] = member["source_hash"]
    return result


def validate_snapshot(snapshot: dict, rows: list[dict]) -> None:
    validate("PendingSnapshot", snapshot)
    manifest = snapshot["manifest"]
    header = manifest["header"]
    entries = manifest["entries"]
    if len(entries) != header["artifact_count"] or len(
        {key(e) for e in entries}
    ) != len(entries):
        refuse("MANIFEST_INCOMPLETE", "inventory count or duplicate artifact")
    if manifest_root(manifest) != header["manifest_root"]:
        refuse("HASH_MISMATCH", "manifest root")
    if len(canonical(snapshot)) > 52428800:
        refuse("MANIFEST_TOO_LARGE", "frozen context exceeds 50 MiB")
    witness = snapshot["source_witness"]
    if (
        witness["identity"] != header["source_checkpoint"]
        or witness["pair_generation"] != header["pair_generation"]
    ):
        refuse("PAIR_MISMATCH", "snapshot witness/checkpoint")
    blobs = {}
    for blob in snapshot["frozen_payloads"]:
        if source_hash(blob) != blob["source_hash"] or blob["source_hash"] in blobs:
            refuse("HASH_MISMATCH", "frozen payload hash or duplicate blob")
        if blob["payload"]["kind"] != blob["artifact"]["kind"]:
            refuse("INVALID_PAYLOAD", "artifact kind")
        if blob["artifact"]["kind"] in ("memory", "decision", "record"):
            resource = blob["payload"]["document"]["resource"]
            if (
                resource != blob["artifact"]["source_key"]
                or build_resource(*parse_resource(resource)) != resource
            ):
                refuse("INVALID_PAYLOAD", "resource must equal canonical source key")
        blobs[blob["source_hash"]] = blob
    for entry in entries:
        if entry["membership"] == "selected":
            blob = blobs.get(entry["source_hash"])
            if not blob or blob["artifact"] != entry["artifact"]:
                refuse("SNAPSHOT_CONTEXT_MISSING", "selected inventory payload absent")
    ids = snapshot["operation_ids"]
    if (
        len(set(ids)) != len(ids)
        or set(ids) != {r["unit"]["operation_id"] for r in rows}
        or len(ids) != len(rows)
    ):
        refuse("SNAPSHOT_CONTEXT_MISSING", "operation index mismatch")
    terminal = {r["unit"]["operation_id"] for r in rows if r["state"] == "terminal"}
    if set(snapshot["terminal_operation_ids"]) != terminal:
        refuse("SNAPSHOT_CONTEXT_MISSING", "terminal index mismatch")
    context = snapshot["deletion_context"]
    if context:
        validate_deletion(snapshot)
    by_key = {key(e): e for e in entries}
    for row in rows:
        validate("PendingRow", row)
        if (
            row["manifest_id"] != header["manifest_id"]
            or row["snapshot_root"] != header["manifest_root"]
        ):
            refuse("HASH_MISMATCH", "pending header reference")
        unit = expand(snapshot, row["unit"])
        validate("ApplyUnit", unit)
        if len({key(m) for m in unit["members"]}) != len(unit["members"]):
            refuse("INVALID_REQUEST", "duplicate unit member")
        if len(canonical(unit)) > 1048576:
            refuse("UNIT_TOO_LARGE", "expanded unit exceeds 1 MiB")
        if unit["group_hash"] != group_hash(unit) or unit[
            "operation_hash"
        ] != operation_hash(snapshot, unit):
            refuse("HASH_MISMATCH", "unit digest")
        for member in unit["members"]:
            entry = by_key.get(key(member))
            if member["action"] in ("put", "check"):
                if not entry or entry["membership"] != "selected":
                    refuse("MANIFEST_INCOMPLETE", "nonselected put/check")
                # Preparation may use a temporary declaration payload, but it
                # still must be frozen and hash-verified above.
                if (
                    not unit["restore_context"]
                    and member["source_hash"] != entry["source_hash"]
                ):
                    refuse("HASH_MISMATCH", "member differs from frozen inventory")
            elif entry and entry["membership"] != "excluded":
                refuse("INVALID_REQUEST", "delete of present selected/blocked artifact")
        if inferred_removal(snapshot, unit):
            if not context or unit["delete_approval"] != context["approval"]:
                refuse("DELETE_APPROVAL_REQUIRED", "missing bound deletion context")
            removals = {key(r): r for r in context["plan"]["removals"]}
            for member in unit["members"]:
                effect = None
                if member["action"] == "delete":
                    effect = "delete"
                elif (
                    member["action"] == "put"
                    and member["payload"]["kind"]
                    in ("type_declaration", "relation_declaration")
                    and member["payload"]["declaration"]["tier"] == "retired"
                ):
                    effect = "retire"
                if effect is None:
                    continue
                removal = removals.get(key(member))
                if not removal or any(
                    removal[field] != expected
                    for field, expected in (
                        ("artifact", member["artifact"]),
                        ("group_hash", unit["group_hash"]),
                        ("effect", effect),
                        ("base", member["base"]),
                    )
                ):
                    refuse(
                        "DELETE_APPROVAL_REQUIRED",
                        "destructive member lacks exact removal coverage",
                    )


def validate_deletion(snapshot: dict) -> None:
    context = snapshot["deletion_context"]
    plan, approval = context["plan"], context["approval"]
    h = snapshot["manifest"]["header"]
    for field in (
        "pair_generation",
        "source_checkpoint",
        "manifest_id",
        "manifest_root",
    ):
        if plan[field] != h[field]:
            refuse("PAIR_MISMATCH", "deletion header mismatch")
    if any(plan[k] != snapshot[k] for k in ("pair_id", "server_epoch")):
        refuse("PAIR_MISMATCH", "deletion pair mismatch")
    removals = plan["removals"]
    if len(removals) > 5000 or len(canonical(plan)) > 1048576:
        refuse("DELETE_PLAN_TOO_LARGE", "whole-push removal plan")
    if len({key(r) for r in removals}) != len(removals):
        refuse("INVALID_REQUEST", "duplicate removal")
    count = sum(
        r["effect"] == "delete"
        and r["artifact"]["kind"] in ("memory", "decision", "record")
        for r in removals
    )
    if count != plan["item_delete_count"] or (count and not plan["owned_item_count"]):
        refuse("INVALID_REQUEST", "invalid whole-push D/N")
    if plan["deletion_set_hash"] != digest(
        {"domain": "lite-mirror/deletions/v1", "removals": sorted(removals, key=key)}
    ):
        refuse("HASH_MISMATCH", "removal set")
    if plan["plan_hash"] != digest(
        {
            "domain": "lite-mirror/delete-plan/v1",
            **{k: v for k, v in plan.items() if k != "plan_hash"},
        }
    ):
        refuse("HASH_MISMATCH", "deletion plan")
    if approval["plan_hash"] != plan["plan_hash"]:
        refuse("HASH_MISMATCH", "approval binding")


def inferred_removal(snapshot: dict, unit: dict) -> bool:
    """Consent applies to inferred/coupled removals, not source-explicit retirement."""
    context = snapshot["deletion_context"]
    return any(m["action"] == "delete" for m in unit["members"]) or bool(
        context
        and any(
            r["group_hash"] == unit["group_hash"] for r in context["plan"]["removals"]
        )
    )


def destructive(snapshot: dict, unit: dict) -> bool:
    if inferred_removal(snapshot, unit):
        return True
    blobs = {b["source_hash"]: b["payload"] for b in snapshot["frozen_payloads"]}
    for member in unit["members"]:
        if member["action"] != "put":
            continue
        payload = member.get("payload", blobs.get(member["source_hash"], {}))
        if (
            payload.get("kind") in ("type_declaration", "relation_declaration")
            and payload["declaration"]["tier"] == "retired"
        ):
            return True
    return False
