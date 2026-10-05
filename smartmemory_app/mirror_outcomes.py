"""Atomic local outcome, baseline and frozen-context lifecycle for MirrorState."""

from copy import deepcopy
from smartmemory_app.mirror_integrity import key, refuse, validate


class MirrorOutcomes:
    """Outcome operations sharing MirrorState's one locked SQLite transaction."""

    def query_result(self, entry: dict) -> None:
        """A not_observed read intentionally changes nothing."""
        self._require_lock()
        validate("ReceiptQueryEntry", entry)
        if entry["state"] == "not_observed":
            return
        outcome = (
            entry["receipt"] if entry["state"] == "committed" else entry["disposition"]
        )
        if outcome["operation_id"] != entry["operation_id"]:
            refuse("IDEMPOTENCY_MISMATCH", "receipt query operation mismatch")
        self.acknowledge(outcome)

    def acknowledge(self, outcome: dict) -> None:
        """Save immutable outcome and eligible per-key baselines atomically."""
        self._require_lock()
        validate("OperationOutcome", outcome)
        state = self.read()
        old = next(
            (
                o
                for o in state["outcomes"]
                if o["operation_id"] == outcome["operation_id"]
            ),
            None,
        )
        if old:
            if old != outcome:
                refuse("IDEMPOTENCY_MISMATCH", "immutable operation outcome changed")
            return
        row = self._row(state, outcome["operation_id"])
        unit = row["unit"]
        snapshot = self._snapshot(state, row["manifest_id"])
        if (
            outcome["pair_id"] != snapshot["pair_id"]
            or outcome["operation_hash"] != unit["operation_hash"]
        ):
            refuse("IDEMPOTENCY_MISMATCH", "outcome does not bind frozen operation")
        if row["state"] == "terminal":
            refuse(
                "IDEMPOTENCY_MISMATCH",
                "locally closed operation cannot acquire an outcome",
            )
        if outcome["state"] == "committed":
            if any(
                o.get("receipt_id") == outcome["receipt_id"] for o in state["outcomes"]
            ):
                refuse(
                    "IDEMPOTENCY_MISMATCH",
                    "receipt ID already belongs to another operation",
                )
            if (
                outcome["server_epoch"] != snapshot["server_epoch"]
                or outcome["options"] != unit["options"]
                or outcome["restore_context"] != unit["restore_context"]
                or outcome["delete_approval"] != unit["delete_approval"]
            ):
                refuse("IDEMPOTENCY_MISMATCH", "receipt context differs")
            if len(outcome["members"]) != len(unit["members"]) or {
                key(m) for m in outcome["members"]
            } != {key(m) for m in unit["members"]}:
                refuse("IDEMPOTENCY_MISMATCH", "receipt members differ")
            members = {key(m): m for m in unit["members"]}
            for receipt in outcome["members"]:
                member = members[key(receipt)]
                if receipt["applied_source_hash"] != member["source_hash"]:
                    refuse("HASH_MISMATCH", "receipt applied source differs")
                if receipt["baseline_eligible"]:
                    if receipt["source_hash"] != member["source_hash"]:
                        refuse("HASH_MISMATCH", "receipt final source differs")
                    # Old receipts recovered during rebind do not regain delete authority.
                    incarnation = snapshot["manifest"]["header"]["source_checkpoint"][
                        "incarnation"
                    ]
                    active = (
                        state["source_witness"]["state"] == "active"
                        and state["source_witness"]["identity"]["incarnation"]
                        == incarnation
                    )
                    baseline = {
                        k: receipt[k]
                        for k in (
                            "artifact",
                            "source_hash",
                            "hosted_hash",
                            "hosted_presence",
                            "ownership",
                            "destination_id",
                        )
                    }
                    baseline.update(
                        receipt_id=outcome["receipt_id"],
                        acknowledged_manifest_id=row["manifest_id"],
                        deletion_eligible_incarnation=incarnation
                        if active and member["action"] in ("put", "check")
                        else None,
                    )
                    state["baselines"] = [
                        b for b in state["baselines"] if key(b) != key(receipt)
                    ] + [baseline]
            if (
                state["source_witness"]["state"] == "active"
                and snapshot["manifest"]["header"]["source_checkpoint"]
                == state["source_witness"]["identity"]
            ):
                state["pair"]["source_checkpoint"] = deepcopy(
                    state["source_witness"]["identity"]
                )
            terminal = "committed"
        else:
            terminal = {"blocked": "rejected", "failed": "rejected"}.get(
                outcome["state"], outcome["state"]
            )
        state["outcomes"].append(deepcopy(outcome))
        self._close_row(snapshot, row, terminal)
        self._save(state)

    @staticmethod
    def _close_row(snapshot, row, terminal):
        row.update(state="terminal", terminal_state=terminal)
        if row["unit"]["operation_id"] not in snapshot["terminal_operation_ids"]:
            snapshot["terminal_operation_ids"].append(row["unit"]["operation_id"])

    def close_unsent(self, operation_id: str) -> None:
        self._require_lock()
        state = self.read()
        row = self._row(state, operation_id)
        if row["state"] != "staged":
            refuse(
                "UNCERTAIN",
                "sent operation needs a committed outcome or hosted non-commit fence",
            )
        self._close_row(
            self._snapshot(state, row["manifest_id"]), row, "cancelled_unsent"
        )
        self._save(state)

    def verified(self, operation_id: str) -> None:
        """Close a successful read-only check, never forge a content receipt."""
        self._require_lock()
        state = self.read()
        row = self._row(state, operation_id)
        if row["state"] == "terminal" or any(
            m["action"] != "check" for m in row["unit"]["members"]
        ):
            refuse("INVALID_REQUEST", "verified requires an open check-only unit")
        self._close_row(self._snapshot(state, row["manifest_id"]), row, "verified")
        self._save(state)

    def update_workflow(self, workflow: dict) -> None:
        self._require_lock()
        validate("RestoreWorkflow", workflow)
        state = self.read()
        old = next(
            (
                w
                for w in state["restore_workflows"]
                if w["workflow_id"] == workflow["workflow_id"]
            ),
            None,
        )
        if old is None or any(
            old[k] != workflow[k]
            for k in old
            if k not in ("state", "stage_receipt_ids", "phase_operation_ids")
        ):
            refuse("IDEMPOTENCY_MISMATCH", "restore immutable context changed")
        outcomes = {
            o.get("receipt_id"): o
            for o in state["outcomes"]
            if o["state"] == "committed"
        }
        if not set(workflow["stage_receipt_ids"]) <= outcomes.keys():
            refuse("RESTORE_INCOMPLETE", "restore stage receipts missing")
        if not set(old["phase_operation_ids"]) <= set(
            workflow["phase_operation_ids"]
        ) or not set(old["stage_receipt_ids"]) <= set(workflow["stage_receipt_ids"]):
            refuse(
                "IDEMPOTENCY_MISMATCH",
                "restore phase indexes cannot discard completed siblings",
            )
        if workflow["state"] == "complete":
            baselines = {key(b): b for b in state["baselines"]}
            for dependent in workflow["dependents"]:
                baseline = baselines.get(key(dependent))
                if (
                    not baseline
                    or baseline["hosted_presence"] != "present"
                    or baseline["source_hash"] != dependent["expected_source_hash"]
                ):
                    refuse("RESTORE_INCOMPLETE", "dependent final baseline missing")
            final = [
                o
                for o in outcomes.values()
                if o["operation_id"] in workflow["phase_operation_ids"]
                and any(
                    key(m) == key(workflow["declaration"])
                    and m["baseline_eligible"]
                    and m["source_hash"] == workflow["final_source_hash"]
                    for m in o["members"]
                )
            ]
            if not final:
                refuse("RESTORE_INCOMPLETE", "no final declaration ACK")
        old.update(deepcopy(workflow))
        self._save(state)

    def cleanup(self, manifest_id: str) -> bool:
        """Release frozen context only after all attempts AND restore phases close.

        Complete root requires reconciliation of selected entries and previously
        owned removals, not merely successful disposal of attempted operations.
        """
        self._require_lock()
        state = self.read()
        snapshot = self._snapshot(state, manifest_id)
        rows = [r for r in state["pending"] if r["manifest_id"] == manifest_id]
        if any(r["state"] != "terminal" for r in rows):
            return False
        workflows = [
            w for w in state["restore_workflows"] if w["manifest_id"] == manifest_id
        ]
        if any(w["state"] != "complete" for w in workflows):
            snapshot["state"] = "retained_for_restore"
            self._save(state)
            return False
        entries = {key(e): e for e in snapshot["manifest"]["entries"]}
        baselines = {key(b): b for b in state["baselines"]}
        covered = {
            key(m)
            for r in rows
            if r["terminal_state"] in ("committed", "verified")
            for m in r["unit"]["members"]
        }
        reconciled = all(r["terminal_state"] in ("committed", "verified") for r in rows)
        reconciled &= all(e["membership"] != "blocked" for e in entries.values())
        for k, e in entries.items():
            if e["membership"] == "selected":
                b = baselines.get(k)
                reconciled &= bool(
                    k in covered
                    and b
                    and b["source_hash"] == e["source_hash"]
                    and b["hosted_presence"] == "present"
                )
        for k, b in baselines.items():
            if (
                b["ownership"] == "owned"
                and b["hosted_presence"] == "present"
                and (k not in entries or entries[k]["membership"] != "selected")
            ):
                reconciled = False
        if reconciled:
            state["last_complete_manifest_root"] = snapshot["manifest"]["header"][
                "manifest_root"
            ]
        state["snapshots"].remove(snapshot)
        state["pending"] = [
            r for r in state["pending"] if r["manifest_id"] != manifest_id
        ]
        state["restore_workflows"] = [
            w for w in state["restore_workflows"] if w["manifest_id"] != manifest_id
        ]
        self._save(state)
        return True
