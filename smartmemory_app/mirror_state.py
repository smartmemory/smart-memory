"""Durable local Lite mirror state, without transport or CLI policy prompts.

Open ``apply()`` for a complete push/recovery invocation. It owns .mirror.lock,
never a source transaction across callbacks. Reset takes mirror, write, worker
locks in that order. ``read()`` and ``continuity()`` are strictly read-only.

The SQLite singleton is the exact LocalReceiptStore document. One FULL-sync
transaction replaces it, so outcomes/baselines/indexes cannot diverge. Payloads
occur once per intrinsic hash in each retained snapshot, never in pending puts.
"""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from filelock import FileLock, Timeout

from smartmemory.graph.backends.sqlite import (
    compare_and_set_mirror_source_identity,
    read_mirror_source_identity,
)
from smartmemory.okf.resource import mirror_lite_namespace
from smartmemory_app.mirror_outcomes import MirrorOutcomes
from smartmemory_app.mirror_integrity import (
    MirrorStateError,
    canonical,
    destructive,
    inferred_removal,
    digest,
    expand,
    freeze_unit,
    key,
    refuse,
    validate,
    validate_snapshot,
)

CREDENTIAL_REFERENCES = ("env:SMARTMEMORY_API_KEY", "keyring:smartmemory/api_key")


def file_identity(path: Path) -> str:
    """OS file identity, never a claim to detect an identical in-place restore.

    Python's Windows st_dev/st_ino expose volume/file identity. st_birthtime is
    included where supplied. Native Windows acceptance remains a separate gate.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        raise MirrorStateError("STORE_REPLACED", "source database missing") from exc
    if not stat.st_ino:
        refuse("SOURCE_IDENTITY_UNVERIFIED", "OS file identity unavailable")
    return f"{stat.st_dev}:{stat.st_ino}:{getattr(stat, 'st_birthtime_ns', '')}"


def source_identity(path: Path, *, read_only: bool = False) -> dict:
    try:
        identity = read_mirror_source_identity(path, read_only=read_only)
    except sqlite3.OperationalError as exc:
        if str(exc).startswith("STORE_BUSY") or getattr(
            exc, "sqlite_errorcode", None
        ) in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_READONLY_RECOVERY,
            sqlite3.SQLITE_READONLY_CANTINIT,
            sqlite3.SQLITE_READONLY_ROLLBACK,
        ):
            raise MirrorStateError(
                "STORE_BUSY",
                "read-only identity inspection cannot read a consistent snapshot",
            ) from exc
        raise MirrorStateError(
            "SOURCE_IDENTITY_UNVERIFIED", "cannot read source identity"
        ) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise MirrorStateError(
            "SOURCE_IDENTITY_UNVERIFIED", "cannot read source identity"
        ) from exc
    if identity is None:
        refuse(
            "STORE_REPLACED", "source has no identity, explicit pairing/rebind required"
        )
    validate("SourceIdentity", identity)
    return identity


def new_identity(namespace: str, *, store_id: str | None = None) -> dict:
    return {
        "store_id": store_id or str(uuid4()),
        "incarnation": str(uuid4()),
        "namespace": namespace,
        "checkpoint_generation": 0,
        "checkpoint_nonce": str(uuid4()),
    }


class MirrorState(MirrorOutcomes):
    """Resolved-data-root state. Construction never creates a file or lock."""

    def __init__(self, data_dir: str | Path, *, timeout: float = 5):
        if not math.isfinite(timeout) or not 0 <= timeout <= 300:
            raise ValueError("mirror lock timeout must be between 0 and 300 seconds")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.database = self.data_dir / "memory.db"
        self.path = self.data_dir / "mirror.sqlite3"
        self.timeout = timeout
        self._locked = False

    @contextmanager
    def apply(self):
        """One process per canonical database root, including maintenance."""
        if self._locked:
            raise RuntimeError("nested mirror apply is unsupported")
        # Do not create an empty source/data root as a side effect of a push.
        if not self.data_dir.is_dir():
            refuse("PAIR_REQUIRED", "Lite data directory missing")
        try:
            with FileLock(self.data_dir / ".mirror.lock", timeout=self.timeout):
                self._locked = True
                try:
                    yield self
                finally:
                    self._locked = False
        except Timeout as exc:
            raise MirrorStateError(
                "STORE_BUSY", "another mirror or maintenance process is active"
            ) from exc

    def _require_lock(self):
        if not self._locked:
            raise RuntimeError("mutation requires MirrorState.apply()")

    def read(self) -> dict:
        """Read existing compact state, without initialization or SQL migration."""
        if not self.path.exists():
            refuse("PAIR_REQUIRED", "no local mirror binding")
        if Path(str(self.path) + "-journal").exists():
            refuse(
                "DURABILITY_UNAVAILABLE", "mirror database requires writable recovery"
            )
        with self._connection(readonly=True) as conn:
            row = conn.execute(
                "SELECT body, checksum FROM mirror_state WHERE singleton=1"
            ).fetchone()
        if not row or sha256(row[0]).hexdigest() != row[1]:
            refuse("HASH_MISMATCH", "local state checksum")
        try:
            state = json.loads(row[0])
        except ValueError as exc:
            raise MirrorStateError(
                "SNAPSHOT_CONTEXT_MISSING", "invalid local state"
            ) from exc
        validate("LocalReceiptStore", state)
        for snapshot in state["snapshots"]:
            rows = [
                r
                for r in state["pending"]
                if r["manifest_id"] == snapshot["manifest"]["header"]["manifest_id"]
            ]
            validate_snapshot(snapshot, rows)
        return state

    @contextmanager
    def _connection(self, *, readonly=False):
        uri = self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
        conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        try:
            if not readonly:
                # DELETE journal avoids read-only opens creating WAL companions.
                mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA fullfsync=ON")
                if (
                    mode != "delete"
                    or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
                ):
                    refuse("DURABILITY_UNAVAILABLE", "local SQLite durability settings")
            yield conn
        except sqlite3.Error as exc:
            raise MirrorStateError(
                "DURABILITY_UNAVAILABLE", "local SQLite transaction failed"
            ) from exc
        finally:
            conn.close()

    def recover_local_transaction(self) -> None:
        """Explicit apply-only SQLite hot-journal recovery. Dry run never calls it."""
        self._require_lock()
        if self.path.exists():
            with self._connection() as conn:
                conn.execute("SELECT body FROM mirror_state").fetchone()

    def _save(self, state: dict) -> None:
        self._require_lock()
        validate("LocalReceiptStore", state)
        raw = canonical(state)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mirror_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body BLOB NOT NULL, checksum TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO mirror_state VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET body=excluded.body, checksum=excluded.checksum",
                (raw, sha256(raw).hexdigest()),
            )
            conn.commit()
        if os.name != "nt":
            fd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def prepare_pair(self) -> dict:
        """Explicit initial pairing only, before obtaining a hosted binding."""
        self._require_lock()
        if self.path.exists():
            refuse("PAIR_MISMATCH", "existing destination requires rebind")
        with FileLock(self.data_dir / ".write.lock", timeout=self.timeout):
            # File must exist BEFORE creating even a namespace.
            file_identity(self.database)
            namespace = mirror_lite_namespace(create=True)
            current = read_mirror_source_identity(self.database)
            if current is not None:
                validate("SourceIdentity", current)
                if current["namespace"] != namespace:
                    refuse("PAIR_MISMATCH", "source namespace changed")
                return current
            identity = new_identity(namespace)
            compare_and_set_mirror_source_identity(self.database, None, identity)
            return identity

    def bind(self, pair: dict, endpoint: str, credential_reference: str) -> None:
        self._require_lock()
        if self.path.exists():
            refuse("PAIR_MISMATCH", "one destination per Lite database")
        validate("PairBinding", pair)
        if credential_reference not in CREDENTIAL_REFERENCES:
            refuse(
                "INVALID_REQUEST",
                "use a supported credential reference, never credential bytes",
            )
        from urllib.parse import urlsplit

        url = urlsplit(endpoint)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            refuse(
                "INVALID_REQUEST",
                "endpoint must not contain credentials or query parameters",
            )
        identity = source_identity(self.database)
        if (
            pair["source_checkpoint"] != identity
            or pair["state"] != "active"
            or mirror_lite_namespace() != identity["namespace"]
        ):
            refuse("PAIR_MISMATCH", "pair differs from actual source")
        self._save(
            {
                "store_schema_version": "1",
                "pair": deepcopy(pair),
                "endpoint": endpoint,
                "credential_reference": credential_reference,
                "last_complete_manifest_root": None,
                "baselines": [],
                "pending": [],
                "snapshots": [],
                "restore_workflows": [],
                "outcomes": [],
                "source_witness": {
                    "identity": identity,
                    "resolved_database_path": str(self.database),
                    "os_file_identity": file_identity(self.database),
                    "state": "active",
                    "pending_identity": None,
                    "pair_generation": pair["pair_generation"],
                },
            }
        )

    def continuity(self, hosted_pair: dict) -> dict:
        """Read-only check before absence planning AND before uncertain resend."""
        state = self.read()
        validate("PairBinding", hosted_pair)
        witness, pair = state["source_witness"], state["pair"]
        if witness["state"] != "active":
            refuse("RECOVERY_REQUIRED", "source witness is not active")
        identity = source_identity(self.database, read_only=not self._locked)
        if (
            identity != witness["identity"]
            or witness["resolved_database_path"] != str(self.database)
            or witness["os_file_identity"] != file_identity(self.database)
        ):
            refuse("STORE_REPLACED", "database/checkpoint/path/file witness mismatch")
        if mirror_lite_namespace() != identity["namespace"]:
            refuse("PAIR_MISMATCH", "namespace changed")
        fields = (
            "pair_id",
            "tenant_id",
            "workspace_id",
            "server_id",
            "server_epoch",
            "pair_generation",
        )
        if (
            any(pair[k] != hosted_pair[k] for k in fields)
            or hosted_pair["state"] != "active"
        ):
            refuse("PAIR_MISMATCH", "hosted binding or generation changed")
        # The host may still have the previous checkpoint until the first batch.
        allowed = [pair["source_checkpoint"], identity]
        if hosted_pair["source_checkpoint"] not in allowed:
            refuse("STORE_REPLACED", "unexplained hosted checkpoint mismatch")
        return identity

    def checkpoint(self, hosted_pair: dict) -> dict:
        """Durable old/new handshake, no network while holding the writer lock."""
        self._require_lock()
        identity = self.continuity(hosted_pair)
        state = self.read()
        if any(r["state"] != "terminal" for r in state["pending"]):
            refuse("UNCERTAIN", "settle previous operations before a later checkpoint")
        pending = {
            **identity,
            "checkpoint_generation": identity["checkpoint_generation"] + 1,
            "checkpoint_nonce": str(uuid4()),
        }
        state["source_witness"].update(
            state="checkpoint_pending", pending_identity=pending
        )
        self._save(state)
        with FileLock(self.data_dir / ".write.lock", timeout=self.timeout):
            compare_and_set_mirror_source_identity(self.database, identity, pending)
        # stage() completes the handshake with its complete frozen snapshot.
        return pending

    def resume_checkpoint(self) -> dict:
        """Only repair the exact persisted old/new handshake, never infer one."""
        self._require_lock()
        state = self.read()
        witness = state["source_witness"]
        if witness["state"] != "checkpoint_pending" or not witness["pending_identity"]:
            refuse("RECOVERY_REQUIRED", "no known interrupted checkpoint")
        if witness["resolved_database_path"] != str(self.database) or witness[
            "os_file_identity"
        ] != file_identity(self.database):
            refuse("STORE_REPLACED", "checkpoint file was replaced")
        with FileLock(self.data_dir / ".write.lock", timeout=self.timeout):
            identity = source_identity(self.database)
            if identity == witness["identity"]:
                compare_and_set_mirror_source_identity(
                    self.database, identity, witness["pending_identity"]
                )
            elif identity != witness["pending_identity"]:
                refuse("STORE_REPLACED", "unexplained checkpoint disagreement")
        return deepcopy(witness["pending_identity"])

    def mark_recovery_required(self) -> None:
        """Must commit before reset/restore touches any source file."""
        self._require_lock()
        if self.path.exists():
            self.recover_local_transaction()
            state = self.read()
            state["source_witness"]["state"] = "recovery_required"
            self._save(state)

    def stage(
        self, snapshot: dict, wire_units: list[dict], workflows: list[dict] = ()
    ) -> None:
        """Persist complete context and all immutable units before any send."""
        self._require_lock()
        state = self.read()
        snapshot = deepcopy(snapshot)
        h = snapshot["manifest"]["header"]
        witness = state["source_witness"]
        identity = (
            witness["pending_identity"]
            if witness["state"] == "checkpoint_pending"
            else witness["identity"]
        )
        if (
            witness["state"] == "recovery_required"
            or source_identity(self.database) != identity
        ):
            refuse("RECOVERY_REQUIRED", "snapshot cannot complete source handshake")
        expected_witness = {
            **witness,
            "identity": identity,
            "pending_identity": None,
            "state": "active",
        }
        if (
            snapshot["source_witness"] != expected_witness
            or h["source_checkpoint"] != identity
        ):
            refuse("PAIR_MISMATCH", "snapshot must use current checkpoint witness")
        if (
            witness["os_file_identity"] != file_identity(self.database)
            or mirror_lite_namespace() != identity["namespace"]
        ):
            refuse("STORE_REPLACED", "source changed before staging")
        if any(snapshot[k] != state["pair"][k] for k in ("pair_id", "server_epoch")):
            refuse("PAIR_MISMATCH", "snapshot destination")
        if h["pair_generation"] != state["pair"]["pair_generation"]:
            refuse("PAIR_MISMATCH", "snapshot generation")
        if any(
            s["manifest"]["header"]["manifest_id"] == h["manifest_id"]
            for s in state["snapshots"]
        ):
            refuse("IDEMPOTENCY_MISMATCH", "snapshot already frozen, use extend()")
        rows = [
            {
                "manifest_id": h["manifest_id"],
                "unit": freeze_unit(u),
                "state": "staged",
                "last_error": None,
                "snapshot_root": h["manifest_root"],
                "terminal_state": None,
            }
            for u in wire_units
        ]
        self._check_new_units(state, rows)
        validate_snapshot(snapshot, rows)
        for row, wire in zip(rows, wire_units):
            if expand(snapshot, row["unit"]) != wire:
                refuse("HASH_MISMATCH", "wire payload differs from frozen payload")
        self._validate_owned_membership(state, snapshot)
        for workflow in workflows:
            validate("RestoreWorkflow", workflow)
            if workflow["manifest_id"] != h["manifest_id"] or not any(
                b["source_hash"] == workflow["final_source_hash"]
                and b["artifact"] == workflow["declaration"]
                for b in snapshot["frozen_payloads"]
            ):
                refuse("SNAPSHOT_CONTEXT_MISSING", "restore final payload missing")
        state["source_witness"] = expected_witness
        state["snapshots"].append(snapshot)
        state["pending"].extend(rows)
        state["restore_workflows"].extend(deepcopy(workflows))
        self._save(state)

    @staticmethod
    def _check_new_units(state, rows):
        ids = {o["operation_id"] for o in state["outcomes"]} | {
            r["unit"]["operation_id"] for r in state["pending"]
        }
        busy = {
            key(m)
            for r in state["pending"]
            if r["state"] != "terminal"
            for m in r["unit"]["members"]
        }
        for row in rows:
            unit = row["unit"]
            if unit["operation_id"] in ids:
                refuse("IDEMPOTENCY_MISMATCH", "operation ID already used")
            ids.add(unit["operation_id"])
            if any(key(m) in busy for m in unit["members"]):
                refuse("UNCERTAIN", "settle old same-key operation first")
            busy.update(key(m) for m in unit["members"])

    @staticmethod
    def _validate_owned_membership(state, snapshot):
        context = snapshot["deletion_context"]
        if not context:
            return
        owned = [
            b
            for b in state["baselines"]
            if b["ownership"] == "owned"
            and b["hosted_presence"] == "present"
            and b["artifact"]["kind"] in ("memory", "decision", "record")
        ]
        members = [
            {
                "artifact": b["artifact"],
                "base": {
                    k: b[k]
                    for k in (
                        "receipt_id",
                        "source_hash",
                        "hosted_hash",
                        "hosted_presence",
                        "ownership",
                    )
                },
            }
            for b in sorted(owned, key=key)
        ]
        plan = context["plan"]
        if plan["owned_item_count"] != len(owned) or plan[
            "owned_membership_hash"
        ] != digest({"domain": "lite-mirror/owned-membership/v1", "members": members}):
            refuse("BASE_RECEIPT_MISMATCH", "whole-push owned membership")

    def extend(self, manifest_id: str, wire_units: list[dict]) -> None:
        """Append fresh resolved/restore units without changing frozen bytes."""
        self._require_lock()
        state = self.read()
        snapshot = self._snapshot(state, manifest_id)
        rows = [
            {
                "manifest_id": manifest_id,
                "unit": freeze_unit(u),
                "state": "staged",
                "last_error": None,
                "snapshot_root": snapshot["manifest"]["header"]["manifest_root"],
                "terminal_state": None,
            }
            for u in wire_units
        ]
        self._check_new_units(state, rows)
        snapshot["operation_ids"].extend(u["operation_id"] for u in wire_units)
        snapshot["state"] = "pending"
        state["pending"].extend(rows)
        validate_snapshot(
            snapshot, [r for r in state["pending"] if r["manifest_id"] == manifest_id]
        )
        for row, wire in zip(rows, wire_units):
            if expand(snapshot, row["unit"]) != wire:
                refuse("HASH_MISMATCH", "extended payload changed")
        self._save(state)

    @staticmethod
    def _snapshot(state, manifest_id):
        for snapshot in state["snapshots"]:
            if snapshot["manifest"]["header"]["manifest_id"] == manifest_id:
                return snapshot
        refuse("SNAPSHOT_CONTEXT_MISSING", "frozen manifest unavailable")

    @staticmethod
    def _row(state, operation_id):
        for row in state["pending"]:
            if row["unit"]["operation_id"] == operation_id:
                return row
        refuse("SNAPSHOT_CONTEXT_MISSING", "operation unavailable")

    def send(
        self,
        operation_id: str,
        hosted_pair: dict,
        *,
        allow_deletes=False,
        no_delete=False,
        confirmed_delete_count: int | None = None,
        interactive_consent=False,
    ) -> dict:
        """Durably mark sent BEFORE caller invokes HTTP, return exact wire unit.

        Current-invocation consent is never read from yesterday's frozen policy.
        Use a single DeletionInvocation via sync.py across resumed/new snapshots.
        """
        self._require_lock()
        identity = self.continuity(hosted_pair)
        state = self.read()
        row = self._row(state, operation_id)
        if row["state"] == "terminal":
            refuse("INVALID_REQUEST", "terminal operation cannot be sent")
        snapshot = self._snapshot(state, row["manifest_id"])
        if snapshot["manifest"]["header"]["source_checkpoint"] != identity:
            refuse("RECOVERY_REQUIRED", "pending checkpoint differs from actual source")
        unit = expand(snapshot, row["unit"])
        member_keys = {key(m) for m in unit["members"]}
        for earlier in state["pending"]:
            if earlier["unit"]["operation_id"] == operation_id:
                break
            if earlier["state"] != "terminal" and any(
                key(m) in member_keys for m in earlier["unit"]["members"]
            ):
                refuse("UNCERTAIN", "settle earlier same-key operation before send")
        dependencies = unit["depends_on_operations"]
        terminal = {
            r["unit"]["operation_id"]
            for r in state["pending"]
            if r["terminal_state"] in ("committed", "verified")
        }
        terminal |= {
            o["operation_id"] for o in state["outcomes"] if o["state"] == "committed"
        }
        if not set(dependencies) <= terminal:
            refuse("DEPENDENCY_BLOCKED", "predecessor not reconciled")
        if destructive(snapshot, unit):
            if no_delete or unit["options"]["no_delete"]:
                refuse("NO_DELETE_DEPENDENCY", "current no-delete wins")
        if inferred_removal(snapshot, unit):
            self._authorize_delete(
                state,
                snapshot,
                unit,
                identity,
                allow_deletes,
                interactive_consent,
                confirmed_delete_count,
            )
        row["state"] = "sent_uncertain"
        self._save(state)
        return unit

    @staticmethod
    def _authorize_delete(state, snapshot, unit, identity, allow, interactive, count):
        if (
            snapshot["manifest"]["header"]["absence_delete_authority"]
            != "verified_continuity"
        ):
            refuse("RECOVERY_REQUIRED", "snapshot has no absence authority")
        context = snapshot["deletion_context"]
        if (
            not context
            or not (allow or interactive)
            or context["approval"]["inferred"] == "held"
        ):
            refuse("DELETE_APPROVAL_REQUIRED", "current invocation lacks bound consent")
        plan = context["plan"]
        d, n = plan["item_delete_count"], plan["owned_item_count"]
        if (d > 20 or 10 * d > n) and (
            count != d or context["approval"]["confirmed_delete_count"] != d
        ):
            refuse(
                "DELETE_COUNT_CONFIRMATION_REQUIRED",
                "exact whole-push delete count required",
            )
        baselines = {key(b): b for b in state["baselines"]}
        for member in unit["members"]:
            if member["action"] != "delete":
                continue
            b = baselines.get(key(member))
            if not b or b["deletion_eligible_incarnation"] != identity["incarnation"]:
                refuse(
                    "RECOVERY_REQUIRED",
                    "retained key not present-and-ACKed in new incarnation",
                )
