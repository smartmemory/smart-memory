"""Explicit rebind coordinator. Inject authenticated U3 receipt/fence calls.

No HTTP client lives here. A partition fence callback must return the confirmed
PairBinding only after its atomic generation fence has committed. Both data and
ontology partitions must agree. Missing receipts never substitute for a fence.
"""

from copy import deepcopy

from filelock import FileLock

from smartmemory.graph.backends.sqlite import (
    compare_and_set_mirror_source_identity,
    read_mirror_source_identity,
)
from smartmemory.okf.resource import mirror_lite_namespace
from smartmemory_app.mirror_integrity import refuse, validate
from smartmemory_app.mirror_state import file_identity, new_identity


def rebind(state, *, reason: str, receipt_lookup, fence_partition) -> dict:
    """Recover old outcomes, fence both partitions, then activate new incarnation.

    Callbacks: receipt_lookup(pair, operation_ids) -> ReceiptQueryEntry list;
    fence_partition(pair, RecoveryBinding, SourceIdentity, partition) -> PairBinding.
    They execute under .mirror.lock only. On failure the durable recovery marker
    and old mappings/receipts survive. Repeating uses the same pending identity.
    """
    state._require_lock()
    state.mark_recovery_required()
    doc = state.read()
    pair, witness = doc["pair"], doc["source_witness"]
    recovery = {
        "previous_pair_id": pair["pair_id"],
        "expected_pair_generation": pair["pair_generation"],
        "reason": reason,
        "retain_absent_keys": True,
    }
    validate("RecoveryBinding", recovery)
    file_identity(state.database)
    namespace = mirror_lite_namespace()
    if namespace != witness["identity"]["namespace"]:
        refuse("PAIR_MISMATCH", "rebind cannot silently change source namespace")
    candidate = witness["pending_identity"]
    # A pending checkpoint is not a new incarnation. Replace it explicitly.
    if (
        candidate is None
        or candidate["incarnation"] == witness["identity"]["incarnation"]
    ):
        candidate = new_identity(namespace, store_id=witness["identity"]["store_id"])
        witness["pending_identity"] = candidate
        state._save(doc)

    def retrieve():
        current = state.read()
        ids = [
            r["unit"]["operation_id"]
            for r in current["pending"]
            if r["state"] == "sent_uncertain"
        ]
        if not ids:
            return
        entries = receipt_lookup(deepcopy(pair), ids)
        if len(entries) != len(ids) or {e["operation_id"] for e in entries} != set(ids):
            refuse("UNCERTAIN", "receipt lookup omitted old operations")
        for entry in entries:
            state.query_result(entry)

    retrieve()
    bindings = []
    for partition in ("data", "ontology"):
        binding = fence_partition(
            deepcopy(pair), deepcopy(recovery), deepcopy(candidate), partition
        )
        if binding is None:
            refuse("RECOVERY_REQUIRED", f"{partition} generation fence unavailable")
        validate("PairBinding", binding)
        if (
            any(
                binding[k] != pair[k]
                for k in (
                    "pair_id",
                    "tenant_id",
                    "workspace_id",
                    "server_id",
                    "server_epoch",
                )
            )
            or binding["pair_generation"] != pair["pair_generation"] + 1
            or binding["source_checkpoint"] != candidate
            or binding["state"] != "active"
            or binding["recovery_mode"] != "retain_absent_keys"
        ):
            refuse("PAIR_MISMATCH", "invalid rebind fence response")
        bindings.append(binding)
    if bindings[0] != bindings[1]:
        refuse("RECOVERY_REQUIRED", "partition fences disagree")
    retrieve()
    doc = state.read()
    if any(r["state"] == "sent_uncertain" for r in doc["pending"]):
        refuse("UNCERTAIN", "old sent operations need their committed/fenced outcomes")
    for row in doc["pending"]:
        if row["state"] != "terminal":
            state._close_row(
                state._snapshot(doc, row["manifest_id"]), row, "cancelled_by_rebind"
            )
    for baseline in doc["baselines"]:
        baseline["deletion_eligible_incarnation"] = None
    # If interrupted after this CAS, the exact candidate remains in sidecar.
    with FileLock(state.data_dir / ".write.lock", timeout=state.timeout):
        current = read_mirror_source_identity(state.database)
        if current != candidate:
            compare_and_set_mirror_source_identity(state.database, current, candidate)
    doc["pair"] = bindings[0]
    doc["source_witness"] = {
        "identity": candidate,
        "resolved_database_path": str(state.database),
        "os_file_identity": file_identity(state.database),
        "state": "active",
        "pending_identity": None,
        "pair_generation": bindings[0]["pair_generation"],
    }
    state._save(doc)
    return deepcopy(bindings[0])
