"""U2 real SQLite/filesystem lifecycle proofs. No store/filesystem mocks.

Fixtures are normalized contract payloads, not an implementation of U1 selection
or U3 hosted apply. Hosted outcome inputs exercise the local consumer only.
"""

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from smartmemory.graph.backends.sqlite import SQLiteBackend, read_mirror_source_identity
from smartmemory.okf.resource import mirror_lite_namespace
from smartmemory_app.mirror_integrity import (
    MirrorStateError,
    canonical,
    digest,
    group_hash,
    manifest_root,
    operation_hash,
    source_hash,
    validate,
)
from smartmemory_app.mirror_state import MirrorState

NOW = "2026-10-05T00:00:00Z"
EMPTY_BASE = {
    "receipt_id": None,
    "source_hash": None,
    "hosted_hash": None,
    "hosted_presence": "unbound",
    "ownership": "unbound",
}


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    for name in (
        "HOME",
        "USERPROFILE",
        "SMARTMEMORY_CONFIG_DIR",
        "SMARTMEMORY_DATA_DIR",
    ):
        target = tmp_path / name.lower()
        target.mkdir()
        monkeypatch.setenv(name, str(target))
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "RESEND_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SMARTMEMORY_TEST_FLUSH", "0")
    monkeypatch.setenv("SMARTMEMORY_CRASH_REPORTS", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    assert Path(os.environ["SMARTMEMORY_CONFIG_DIR"]).is_relative_to(tmp_path)
    assert Path(os.environ["SMARTMEMORY_DATA_DIR"]).is_relative_to(tmp_path)
    assert os.environ["SMARTMEMORY_CONFIG_DIR"] != os.environ["SMARTMEMORY_DATA_DIR"]


def pair_for(identity):
    return {
        "pair_id": str(uuid4()),
        "tenant_id": "tenant",
        "workspace_id": "workspace",
        "server_id": "server",
        "server_epoch": "epoch",
        "projection": "authored-v1",
        "publication_origin": "user:sync",
        "state": "active",
        "created_at": NOW,
        "pair_generation": 0,
        "source_checkpoint": identity,
        "recovery_mode": "normal",
    }


def setup_state():
    root = Path(os.environ["SMARTMEMORY_DATA_DIR"])
    backend = SQLiteBackend(str(root / "memory.db"))
    backend.add_node(
        "source-row", {"content": "live local value", "memory_type": "semantic"}
    )
    backend.close()
    state = MirrorState(root)
    with state.apply():
        identity = state.prepare_pair()
        pair = pair_for(identity)
        state.bind(pair, "https://host.example/api", "env:SMARTMEMORY_API_KEY")
    return state, pair


def payload(identity, name):
    resource = f"smartmemory://{identity['namespace']}/{name}"
    return {
        "artifact": {"kind": "memory", "source_key": resource},
        "payload": {
            "kind": "memory",
            "document": {
                "okf_version": "0.2",
                "type": "semantic",
                "title": None,
                "description": None,
                "resource": resource,
                "tags": [],
                "timestamp": None,
                "body": f"frozen {name} 漢字",
                "smartmemory": {
                    "origin": "cli:add",
                    "temporal": {
                        "valid_start": None,
                        "valid_end": None,
                        "transaction_time": None,
                    },
                    "reference": False,
                    "preserved": {},
                    "derived_from": None,
                    "superseded_by": None,
                },
            },
            "references": [],
        },
    }


def context(state, names=("a", "b"), *, identity=None):
    doc = state.read()
    identity = identity or doc["source_witness"]["identity"]
    witness = {
        **doc["source_witness"],
        "identity": identity,
        "pending_identity": None,
        "state": "active",
    }
    blobs = []
    entries = []
    for name in names:
        blob = payload(identity, name)
        blob["source_hash"] = source_hash(blob)
        blobs.append(blob)
        entries.append(
            {
                "artifact": blob["artifact"],
                "membership": "selected",
                "reason": "selected_authored",
                "source_hash": blob["source_hash"],
                "dependencies": [],
                "error": None,
            }
        )
    manifest = {
        "schema_version": "1",
        "header": {
            "manifest_id": str(uuid4()),
            "projection": "authored-v1",
            "manifest_root": "sha256:" + "0" * 64,
            "presence_complete": True,
            "artifact_count": len(entries),
            "captured_at": NOW,
            "source_checkpoint": identity,
            "pair_generation": doc["pair"]["pair_generation"],
            "absence_delete_authority": "verified_continuity",
        },
        "entries": entries,
        "previous_complete_root": doc["last_complete_manifest_root"],
    }
    manifest["header"]["manifest_root"] = manifest_root(manifest)
    snapshot = {
        "manifest": manifest,
        "pair_id": doc["pair"]["pair_id"],
        "server_epoch": doc["pair"]["server_epoch"],
        "source_witness": witness,
        "policy": {
            "requested": None,
            "effective": "skip",
            "source": "noninteractive_default",
            "interactive": False,
        },
        "frozen_payloads": blobs,
        "operation_ids": [],
        "terminal_operation_ids": [],
        "state": "pending",
        "deletion_context": None,
    }
    units = []
    baselines = {b["artifact"]["source_key"]: b for b in doc["baselines"]}
    for blob in blobs:
        base = baselines.get(blob["artifact"]["source_key"], EMPTY_BASE)
        member = {
            "action": "put",
            "artifact": blob["artifact"],
            "base": {k: base[k] for k in EMPTY_BASE},
            "source_hash": blob["source_hash"],
            "payload": blob["payload"],
            "dependencies": [],
        }
        units.append(make_unit(snapshot, member))
    snapshot["operation_ids"] = [u["operation_id"] for u in units]
    return snapshot, units


def make_unit(snapshot, member):
    unit = {
        "operation_id": str(uuid4()),
        "operation_hash": "sha256:" + "0" * 64,
        "options": {"overwrite": False, "no_delete": False, "chosen_action": "none"},
        "members": [member],
        "depends_on_operations": [],
        "target_partition": "data",
        "group_hash": "",
        "reviewed_state": [],
        "restore_context": None,
        "delete_approval": None,
    }
    unit["group_hash"] = group_hash(unit)
    unit["operation_hash"] = operation_hash(snapshot, unit)
    return unit


def receipt(snapshot, unit):
    result = {
        "receipt_id": str(uuid4()),
        "pair_id": snapshot["pair_id"],
        "server_epoch": snapshot["server_epoch"],
        "operation_id": unit["operation_id"],
        "operation_hash": unit["operation_hash"],
        "predecessor_receipts": [],
        "state": "committed",
        "committed_at": NOW,
        "members": [],
        "options": unit["options"],
        "reviewed_state_digest": None,
        "restore_context": unit["restore_context"],
        "delete_approval": unit["delete_approval"],
    }
    for m in unit["members"]:
        deleted = m["action"] == "delete"
        result["members"].append(
            {
                "artifact": m["artifact"],
                "destination_id": "destination-"
                + m["artifact"]["source_key"].rsplit("/", 1)[-1],
                "ownership": "owned",
                "outcome": "deleted" if deleted else "updated",
                "source_hash": m["source_hash"],
                "hosted_hash": None
                if deleted
                else digest({"domain": "hosted-test", "source_hash": m["source_hash"]}),
                "hosted_presence": "absent" if deleted else "present",
                "creation_proven": False,
                "readiness": {
                    "embedding": "pending",
                    "derivation": "pending",
                    "input_hosted_hash": None,
                    "warning": None,
                },
                "quota_units_assessed": 0,
                "applied_source_hash": m["source_hash"],
                "baseline_eligible": True,
                "stage": None,
                "association_cleanup": {
                    "machine_count": 0,
                    "machine_set_hash": None,
                    "overwritten_links": [],
                },
            }
        )
    validate("Receipt", result)
    return result


def stage(state, names=("a", "b")):
    snapshot, units = context(state, names)
    with state.apply():
        state.stage(snapshot, units)
    return snapshot, units


def finish(state, snapshot, units):
    with state.apply():
        for unit in units:
            state.acknowledge(receipt(snapshot, unit))
        assert state.cleanup(snapshot["manifest"]["header"]["manifest_id"])


def fresh_read(root):
    code = "from smartmemory_app.mirror_state import MirrorState; import json,sys; print(json.dumps(MirrorState(sys.argv[1]).read()))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def killed_at(tmp_path, body):
    ready = tmp_path / ("ready-" + uuid4().hex)
    script = (
        "import os,sys,time,json\nfrom pathlib import Path\n"
        + body
        + "\nPath(sys.argv[1]).write_text('ready')\nwhile True: time.sleep(1)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"child exited {process.returncode}: {out} {err}")
            time.sleep(0.02)
        assert ready.exists(), "child never reached named crash point"
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.communicate()


# Checkbox 1


def test_identity_in_actual_db_split_roots_and_host_generation():
    state, pair = setup_state()
    assert read_mirror_source_identity(state.database) == pair["source_checkpoint"]
    assert state.continuity(pair) == pair["source_checkpoint"]
    with closing(sqlite3.connect(state.database)) as conn:
        assert conn.execute(
            "SELECT count(*) FROM mirror_source_identity"
        ).fetchone() == (1,)
    assert state.path.parent != Path(os.environ["SMARTMEMORY_CONFIG_DIR"])
    changed = {**pair, "pair_generation": 1}
    with pytest.raises(MirrorStateError, match="PAIR_MISMATCH"):
        state.continuity(changed)


@pytest.mark.parametrize(
    "change",
    [
        "missing_identity",
        "old_checkpoint",
        "file_replacement",
        "namespace",
        "missing_file",
    ],
)
def test_identity_mismatch_refuses_before_resend(change, tmp_path):
    state, pair = setup_state()
    snapshot, units = stage(state)
    if change == "missing_identity":
        with sqlite3.connect(state.database) as conn:
            conn.execute("DROP TABLE mirror_source_identity")
    elif change == "old_checkpoint":
        with sqlite3.connect(state.database) as conn:
            old = {**pair["source_checkpoint"], "checkpoint_nonce": "old-backup"}
            conn.execute(
                "UPDATE mirror_source_identity SET identity=?", (json.dumps(old),)
            )
    elif change == "file_replacement":
        other = tmp_path / "copy.db"
        other.write_bytes(state.database.read_bytes())
        os.replace(other, state.database)
    elif change == "namespace":
        (Path(os.environ["SMARTMEMORY_CONFIG_DIR"]) / "okf_namespace.json").write_text(
            '{"namespace":"local-other"}'
        )
    else:
        state.database.unlink()
    with state.apply(), pytest.raises((MirrorStateError, ValueError)):
        state.send(units[0]["operation_id"], pair)
    assert state.read()["pending"][0]["state"] == "staged"


def test_known_checkpoint_handshake_recovered_without_deletion_authority():
    state, pair = setup_state()
    with state.apply():
        pending = state.checkpoint(pair)
    with pytest.raises(MirrorStateError, match="RECOVERY_REQUIRED"):
        state.continuity(pair)
    with state.apply():
        assert state.resume_checkpoint() == pending
        snapshot, units = context(state, identity=pending)
        state.stage(snapshot, units)
        assert state.send(units[0]["operation_id"], pair) == units[0]
    assert state.continuity(pair) == pending


# Checkbox 2


def test_credential_bytes_never_persisted():
    state, pair = setup_state()
    assert state.read()["credential_reference"] == "env:SMARTMEMORY_API_KEY"
    state.path.unlink()
    with state.apply(), pytest.raises(MirrorStateError, match="credential reference"):
        state.bind(pair, "https://host.example", "sk_test_secret")
    assert not state.path.exists()


@pytest.mark.parametrize("operation", ["reset", "restore"])
def test_reset_restore_preserves_receipts_and_marks_recovery(operation, tmp_path):
    from smartmemory_app.store_reset import remove_store_files, restore_store_database

    state, pair = setup_state()
    snapshot, units = stage(state)
    finish(state, snapshot, units)
    old = state.read()
    backup = tmp_path / "backup.db"
    backup.write_bytes(state.database.read_bytes())
    if operation == "reset":
        remove_store_files(state.data_dir)
        assert not state.database.exists()
    else:
        restore_store_database(state.data_dir, backup)
        assert state.database.exists()
    new = fresh_read(state.data_dir)
    assert new["source_witness"]["state"] == "recovery_required"
    assert new["outcomes"] == old["outcomes"]
    assert new["baselines"] == old["baselines"]
    assert mirror_lite_namespace() == pair["source_checkpoint"]["namespace"]
    with pytest.raises(MirrorStateError, match="RECOVERY_REQUIRED"):
        state.continuity(pair)


@pytest.mark.parametrize("operation", ["reset", "restore"])
def test_sigkill_before_unlink_preserves_recovery_marker(operation, tmp_path):
    state, pair = setup_state()
    # Trace is an interruption harness, not a mocked filesystem/store. Pause at
    # the actual production boundary immediately before unlink/replace begins.
    backup = tmp_path / "backup.db"
    backup.write_bytes(state.database.read_bytes())
    ready = tmp_path / "before-unlink"
    code = f"""from smartmemory_app.store_reset import remove_store_files, restore_store_database
import inspect
import smartmemory_app.store_reset as module
root=Path({str(state.data_dir)!r})
def trace(frame,event,arg):
    if event == 'line' and frame.f_code.co_filename == module.__file__:
        line=Path(module.__file__).read_text().splitlines()[frame.f_lineno-1].strip()
        boundary = line == 'files = _store_files(data_path)' and frame.f_code.co_name == 'remove_store_files'
        boundary |= line.startswith('with _exclusive_files(files):') and frame.f_code.co_name == 'restore_store_database'
        if boundary:
            Path({str(ready)!r}).write_text('ready')
            while True: time.sleep(1)
    return trace
sys.settrace(trace)
"""
    call = (
        "remove_store_files(root)"
        if operation == "reset"
        else f"restore_store_database(root,Path({str(backup)!r}))"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time\nfrom pathlib import Path\n" + code + call,
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()[1]
            time.sleep(0.02)
        assert ready.exists()
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.communicate()
    assert state.database.exists()
    assert fresh_read(state.data_dir)["source_witness"]["state"] == "recovery_required"


# Checkbox 3


def test_full_frozen_snapshot_exact_reload_and_partial_ack():
    state, pair = setup_state()
    snapshot, units = stage(state)
    persisted = fresh_read(state.data_dir)
    assert persisted["snapshots"] == [snapshot]
    assert all("payload" not in r["unit"]["members"][0] for r in persisted["pending"])
    with state.apply():
        assert state.send(units[0]["operation_id"], pair) == units[0]
        state.acknowledge(receipt(snapshot, units[0]))
    with sqlite3.connect(state.database) as conn:
        conn.execute("UPDATE nodes SET properties='{}'")
    persisted = fresh_read(state.data_dir)
    assert persisted["snapshots"][0]["manifest"] == snapshot["manifest"]
    assert persisted["snapshots"][0]["frozen_payloads"] == snapshot["frozen_payloads"]
    with state.apply():
        assert canonical(state.send(units[1]["operation_id"], pair)) == canonical(
            units[1]
        )


@pytest.mark.parametrize("point", ["before_http", "after_partial_ack"])
def test_sigkill_frozen_retry_new_process(point, tmp_path):
    state, pair = setup_state()
    snapshot, units = stage(state)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps({"pair": pair, "receipt": receipt(snapshot, units[0])})
    )
    body = f"""from smartmemory_app.mirror_state import MirrorState
state=MirrorState({str(state.data_dir)!r})
inputs=json.loads(Path({str(inputs)!r}).read_text())
with state.apply():
    state.send({units[0]["operation_id"]!r},inputs['pair'])
"""
    if point == "after_partial_ack":
        body += "    state.acknowledge(inputs['receipt'])\n"
    killed_at(tmp_path, body)
    with sqlite3.connect(state.database) as conn:
        conn.execute("UPDATE nodes SET properties='{}'")
    loaded = fresh_read(state.data_dir)
    assert loaded["snapshots"][0]["manifest"] == snapshot["manifest"]
    assert len(loaded["baselines"]) == (1 if point == "after_partial_ack" else 0)
    index = 1 if point == "after_partial_ack" else 0
    with state.apply():
        assert state.send(units[index]["operation_id"], pair) == units[index]


@pytest.mark.parametrize(
    "target", ["blob", "root", "reference", "count", "policy_checksum"]
)
def test_corruption_refuses_reload(target):
    state, pair = setup_state()
    snapshot, units = stage(state)
    doc = state.read()
    if target == "blob":
        doc["snapshots"][0]["frozen_payloads"][0]["payload"]["document"]["body"] = (
            "corrupt"
        )
    elif target == "root":
        doc["snapshots"][0]["manifest"]["header"]["manifest_root"] = (
            "sha256:" + "0" * 64
        )
    elif target == "count":
        doc["snapshots"][0]["manifest"]["header"]["artifact_count"] = 1
    elif target == "reference":
        doc["pending"][0]["unit"]["members"][0]["payload_ref"] = "sha256:" + "0" * 64
    else:
        doc["snapshots"][0]["policy"]["effective"] = "overwrite"
    raw = canonical(doc)
    checksum = "bad" if target == "policy_checksum" else sha256(raw).hexdigest()
    with sqlite3.connect(state.path) as conn:
        conn.execute("UPDATE mirror_state SET body=?,checksum=?", (raw, checksum))
    with pytest.raises(MirrorStateError):
        state.read()


# Checkbox 4


def test_rebind_receipts_then_both_fences_retains_absent_keys():
    from smartmemory_app.mirror_recovery import rebind
    from smartmemory_app.store_reset import remove_store_files

    state, pair = setup_state()
    first, units = stage(state)
    finish(state, first, units)
    pending, new_units = stage(state, ("c",))
    with state.apply():
        state.send(new_units[0]["operation_id"], pair)
    remove_store_files(state.data_dir)
    backend = SQLiteBackend(str(state.database))
    backend.close()
    events = []

    def lookup(binding, ids):
        events.append("receipts")
        if "ontology" not in events:
            return [
                {
                    "operation_id": oid,
                    "state": "not_observed",
                    "receipt": None,
                    "disposition": None,
                }
                for oid in ids
            ]
        unit = new_units[0]
        return [
            {
                "operation_id": ids[0],
                "state": "terminal_without_apply",
                "receipt": None,
                "disposition": disposition(pending, unit, "cancelled_by_rebind"),
            }
        ]

    def fence(binding, recovery, identity, partition):
        events.append(partition)
        assert recovery["retain_absent_keys"] is True
        assert state.read()["source_witness"]["state"] == "recovery_required"
        return {
            **binding,
            "pair_generation": binding["pair_generation"] + 1,
            "source_checkpoint": identity,
            "recovery_mode": "retain_absent_keys",
        }

    with state.apply():
        rebound = rebind(
            state, reason="reset", receipt_lookup=lookup, fence_partition=fence
        )
    assert events == ["receipts", "data", "ontology", "receipts"]
    assert (
        rebound["source_checkpoint"]["incarnation"]
        != pair["source_checkpoint"]["incarnation"]
    )
    assert all(
        b["deletion_eligible_incarnation"] is None for b in state.read()["baselines"]
    )
    assert len(state.read()["outcomes"]) == 3
    assert state.continuity(rebound) == rebound["source_checkpoint"]
    with state.apply():
        state.cleanup(pending["manifest"]["header"]["manifest_id"])
    snapshot, units = stage(state, ("a",))
    with state.apply():
        state.acknowledge(receipt(snapshot, units[0]))
    rows = {
        b["artifact"]["source_key"].rsplit("/", 1)[-1]: b
        for b in state.read()["baselines"]
    }
    assert (
        rows["a"]["deletion_eligible_incarnation"]
        == rebound["source_checkpoint"]["incarnation"]
    )
    assert rows["b"]["deletion_eligible_incarnation"] is None


def test_rebind_unavailable_partition_preserves_candidate_and_old_work():
    from smartmemory_app.mirror_recovery import rebind

    state, pair = setup_state()
    snapshot, units = stage(state)
    with state.apply():
        state.send(units[0]["operation_id"], pair)
    candidates = []

    def lookup(pair, ids):
        return [
            {
                "operation_id": i,
                "state": "not_observed",
                "receipt": None,
                "disposition": None,
            }
            for i in ids
        ]

    def fence(binding, recovery, identity, partition):
        candidates.append(identity)
        return None

    for _ in range(2):
        with state.apply(), pytest.raises(MirrorStateError, match="fence unavailable"):
            rebind(
                state,
                reason="replacement",
                receipt_lookup=lookup,
                fence_partition=fence,
            )
    assert candidates[0] == candidates[1]
    assert state.read()["pending"][0]["state"] == "sent_uncertain"
    assert state.read()["source_witness"]["state"] == "recovery_required"
    with state.apply(), pytest.raises(MirrorStateError, match="RECOVERY_REQUIRED"):
        state.send(units[0]["operation_id"], pair)


# Checkbox 5


def disposition(snapshot, unit, kind):
    result = {
        "pair_id": snapshot["pair_id"],
        "operation_id": unit["operation_id"],
        "operation_hash": unit["operation_hash"],
        "state": kind,
        "recorded_at": NOW,
        "reason": None,
    }
    validate("TerminalDisposition", result)
    return result


@pytest.mark.parametrize(
    "kind",
    [
        "collision_skipped",
        "retained_no_delete",
        "blocked",
        "failed",
        "schema_deferred",
        "held_delete",
    ],
)
def test_terminal_fences_keep_baseline_and_share_outcome_store(kind):
    state, pair = setup_state()
    snapshot, units = stage(state, ("a",))
    finish(state, snapshot, units)
    baseline = state.read()["baselines"]
    root = state.read()["last_complete_manifest_root"]
    snapshot, units = stage(state, ("a",))
    with state.apply():
        state.send(units[0]["operation_id"], pair)
        value = disposition(snapshot, units[0], kind)
        state.acknowledge(value)
        state.acknowledge(value)
        with pytest.raises(MirrorStateError, match="IDEMPOTENCY_MISMATCH"):
            state.acknowledge(receipt(snapshot, units[0]))
        assert state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    doc = fresh_read(state.data_dir)
    assert doc["baselines"] == baseline
    assert doc["last_complete_manifest_root"] == root
    assert len(doc["outcomes"]) == 2
    assert not doc["snapshots"]


def test_unsent_cancel_separate_from_sent_uncertainty_and_verified_check():
    state, pair = setup_state()
    snapshot, units = stage(state)
    with state.apply():
        state.close_unsent(units[0]["operation_id"])
        state.send(units[1]["operation_id"], pair)
        with pytest.raises(MirrorStateError, match="UNCERTAIN"):
            state.close_unsent(units[1]["operation_id"])
        assert not state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
        state.acknowledge(disposition(snapshot, units[1], "failed"))
        assert state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    snapshot, units = context(state, ("a",))
    units[0]["members"][0].pop("payload")
    units[0]["members"][0]["action"] = "check"
    units[0]["operation_hash"] = operation_hash(snapshot, units[0])
    with state.apply():
        state.stage(snapshot, units)
        state.verified(units[0]["operation_id"])
        state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    assert not state.read()["baselines"]
    assert state.read()["last_complete_manifest_root"] is None


def test_restore_workflow_blocks_blob_cleanup():
    state, pair = setup_state()
    snapshot, _ = context(state, ())
    declaration = {"kind": "type_declaration", "source_key": "decl:" + "1" * 64}
    final = {
        "artifact": declaration,
        "payload": {
            "kind": "type_declaration",
            "layer": "private",
            "references": [],
            "declaration": {
                "name": "Invoice",
                "iri": None,
                "display_name": None,
                "definition": None,
                "description": None,
                "aliases": [],
                "examples": [],
                "tier": "retired",
                "properties_schema": {},
                "kind": "record",
                "wikidata_qid": None,
                "required_properties": [],
                "storage_strategy": None,
                "storage_searchable": None,
                "parent_types": [],
                "superseded_by": None,
            },
        },
    }
    final["source_hash"] = source_hash(final)
    temporary = deepcopy(final)
    temporary["payload"]["declaration"]["tier"] = "confirmed"
    temporary["source_hash"] = source_hash(temporary)
    snapshot["frozen_payloads"] = [final, temporary]
    snapshot["manifest"]["entries"] = [
        {
            "artifact": declaration,
            "membership": "selected",
            "reason": "selected_authored",
            "source_hash": final["source_hash"],
            "dependencies": [],
            "error": None,
        }
    ]
    snapshot["manifest"]["header"]["artifact_count"] = 1
    snapshot["manifest"]["header"]["manifest_root"] = manifest_root(
        snapshot["manifest"]
    )
    member = {
        "action": "put",
        "artifact": declaration,
        "base": EMPTY_BASE,
        "source_hash": temporary["source_hash"],
        "payload": temporary["payload"],
        "dependencies": [],
    }
    prep = make_unit(snapshot, member)
    prep["target_partition"] = "ontology"
    prep["restore_context"] = {
        "workflow_id": "restore",
        "phase": "prepare",
        "declaration": declaration,
        "final_declaration_hash": final["source_hash"],
        "effective_schema_hash": digest({}),
        "preparation_receipt_id": None,
        "dependent_set_hash": digest([]),
        "record_storage_receipts": [],
    }
    prep["operation_hash"] = operation_hash(snapshot, prep)
    snapshot["operation_ids"] = [prep["operation_id"]]
    workflow = {
        "workflow_id": "restore",
        "manifest_id": snapshot["manifest"]["header"]["manifest_id"],
        "declaration": declaration,
        "final_source_hash": final["source_hash"],
        "effective_schema_hash": digest({}),
        "dependents": [],
        "dependent_set_hash": digest([]),
        "phase_operation_ids": [prep["operation_id"]],
        "stage_receipt_ids": [],
        "state": "planned",
        "destination_initial_tier": None,
    }
    with state.apply():
        state.stage(snapshot, [prep], [workflow])
        prepared = receipt(snapshot, prep)
        prepared["members"][0].update(
            outcome="prepared",
            baseline_eligible=False,
            stage="prepare",
            source_hash=final["source_hash"],
        )
        state.acknowledge(prepared)
        assert state.read()["baselines"] == []
        assert not state.cleanup(workflow["manifest_id"])
        assert state.read()["snapshots"][0]["state"] == "retained_for_restore"
        with pytest.raises(MirrorStateError, match="RESTORE_INCOMPLETE"):
            state.update_workflow({**workflow, "state": "complete"})
        finishing = make_unit(
            snapshot,
            {
                **member,
                "source_hash": final["source_hash"],
                "payload": final["payload"],
            },
        )
        finishing["target_partition"] = "ontology"
        finishing["restore_context"] = {
            **prep["restore_context"],
            "phase": "finalize",
            "preparation_receipt_id": prepared["receipt_id"],
        }
        finishing["operation_hash"] = operation_hash(snapshot, finishing)
        state.extend(workflow["manifest_id"], [finishing])
        with pytest.raises(MirrorStateError, match="NO_DELETE_DEPENDENCY"):
            state.send(finishing["operation_id"], pair, no_delete=True)
        # Explicit source retirement requires no inferred-delete approval.
        state.send(finishing["operation_id"], pair)
        finished = receipt(snapshot, finishing)
        finished["members"][0].update(outcome="retired", stage="finalize")
        state.acknowledge(finished)
        state.update_workflow(
            {
                **workflow,
                "state": "complete",
                "stage_receipt_ids": [prepared["receipt_id"], finished["receipt_id"]],
                "phase_operation_ids": [
                    prep["operation_id"],
                    finishing["operation_id"],
                ],
            }
        )
        assert state.cleanup(workflow["manifest_id"])
    assert state.read()["baselines"][0]["source_hash"] == final["source_hash"]
    assert not state.read()["snapshots"]


# Checkbox 6


def test_single_apply_cross_process_and_reset_lock_order(tmp_path):
    state, pair = setup_state()
    from smartmemory_app.store_reset import remove_store_files

    code = f"""from smartmemory_app.mirror_state import MirrorState, MirrorStateError
s=MirrorState({str(state.data_dir)!r},timeout=0)
try:
    with s.apply(): pass
except MirrorStateError as e:
    assert e.code == 'STORE_BUSY'
else: raise AssertionError('second apply entered')
"""
    with state.apply():
        subprocess.run([sys.executable, "-c", code], check=True)
        with pytest.raises(MirrorStateError, match="STORE_BUSY"):
            remove_store_files(state.data_dir)
    assert state.database.exists()


# Checkbox 7


def test_sqlite_disk_full_rolls_back_only_new_ack(tmp_path):
    state, pair = setup_state()
    snapshot, units = stage(state)
    with state.apply():
        state.acknowledge(receipt(snapshot, units[0]))
    before = state.read()
    # Real SQLite disk-full mechanism. Cap pages in the actual writing connection
    # with a trace breakpoint. No mocked store/filesystem or size-limited seam.
    target = receipt(snapshot, units[1])
    target["members"][0]["readiness"]["warning"] = "x" * 200000
    import smartmemory_app.mirror_state as module

    armed = False

    def trace(frame, event, arg):
        nonlocal armed
        if (
            not armed
            and event == "line"
            and frame.f_code.co_filename == module.__file__
            and frame.f_code.co_name == "_save"
            and "conn" in frame.f_locals
        ):
            conn = frame.f_locals["conn"]
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count={pages}")
            armed = True
        return trace

    try:
        sys.settrace(trace)
        with (
            state.apply(),
            pytest.raises(MirrorStateError, match="DURABILITY_UNAVAILABLE"),
        ):
            state.acknowledge(target)
    finally:
        sys.settrace(None)
    assert armed
    assert fresh_read(state.data_dir) == before
    with state.apply():
        state.acknowledge(receipt(snapshot, units[1]))
        assert state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    assert len(state.read()["baselines"]) == 2
    assert (
        state.read()["last_complete_manifest_root"]
        == snapshot["manifest"]["header"]["manifest_root"]
    )


# Checkbox 8


def test_not_observed_exact_retry_blocks_new_same_key():
    state, pair = setup_state()
    snapshot, units = stage(state, ("a",))
    with state.apply():
        wire = state.send(units[0]["operation_id"], pair)
        before = state.path.read_bytes()
        state.query_result(
            {
                "operation_id": units[0]["operation_id"],
                "state": "not_observed",
                "receipt": None,
                "disposition": None,
            }
        )
        assert state.path.read_bytes() == before
        later, later_units = context(state, ("a",))
        with pytest.raises(MirrorStateError, match="UNCERTAIN"):
            state.stage(later, later_units)
        assert state.send(units[0]["operation_id"], pair) == wire


def deletion_context(state, snapshot, units, *, consent="allow_deletes", confirm=None):
    doc = state.read()
    owned = sorted(
        doc["baselines"],
        key=lambda b: (b["artifact"]["kind"], b["artifact"]["source_key"]),
    )
    removals = []
    for unit in units:
        m = unit["members"][0]
        reviewed = {
            "artifact": m["artifact"],
            "current_presence": "present",
            "current_hosted_hash": m["base"]["hosted_hash"],
            "mirror_instance_id": "instance",
            "base_receipt_id": m["base"]["receipt_id"],
            "incident_reference_hash": digest([]),
        }
        removals.append(
            {
                "artifact": m["artifact"],
                "reason": "source_absent",
                "effect": "delete",
                "base": m["base"],
                "group_hash": unit["group_hash"],
                "reviewed_state": reviewed,
            }
        )
    header = snapshot["manifest"]["header"]
    plan = {
        **{
            k: header[k]
            for k in (
                "pair_generation",
                "source_checkpoint",
                "manifest_id",
                "manifest_root",
            )
        },
        "pair_id": snapshot["pair_id"],
        "server_epoch": snapshot["server_epoch"],
        "owned_item_count": len(owned),
        "owned_membership_hash": digest(
            {
                "domain": "lite-mirror/owned-membership/v1",
                "members": [
                    {"artifact": b["artifact"], "base": {k: b[k] for k in EMPTY_BASE}}
                    for b in owned
                ],
            }
        ),
        "removals": removals,
        "item_delete_count": len(removals),
        "deletion_set_hash": digest(
            {
                "domain": "lite-mirror/deletions/v1",
                "removals": sorted(
                    removals,
                    key=lambda r: (r["artifact"]["kind"], r["artifact"]["source_key"]),
                ),
            }
        ),
    }
    plan["plan_hash"] = digest({"domain": "lite-mirror/delete-plan/v1", **plan})
    approval = {
        "plan_hash": plan["plan_hash"],
        "inferred": consent,
        "confirmed_delete_count": confirm,
    }
    snapshot["deletion_context"] = {"plan": plan, "approval": approval}
    for unit in units:
        unit["delete_approval"] = approval
        unit["operation_hash"] = operation_hash(snapshot, unit)


def deletes(state, count, confirm=None):
    snapshot, _ = context(state, ())
    units = []
    for baseline in state.read()["baselines"][:count]:
        member = {
            "action": "delete",
            "artifact": baseline["artifact"],
            "base": {k: baseline[k] for k in EMPTY_BASE},
            "reason": "source_absent",
            "source_hash": None,
            "dependencies": [],
        }
        units.append(make_unit(snapshot, member))
    snapshot["operation_ids"] = [u["operation_id"] for u in units]
    deletion_context(state, snapshot, units, confirm=confirm)
    return snapshot, units


def test_resume_delete_requires_current_consent_and_fence_before_release():
    state, pair = setup_state()
    original, units = stage(state)
    finish(state, original, units)
    snapshot, units = deletes(state, 2, confirm=2)
    with state.apply():
        state.stage(snapshot, units)
        state.send(
            units[0]["operation_id"], pair, allow_deletes=True, confirmed_delete_count=2
        )
        state.acknowledge(receipt(snapshot, units[0]))
        state.send(
            units[1]["operation_id"], pair, allow_deletes=True, confirmed_delete_count=2
        )
        with pytest.raises(MirrorStateError, match="DELETE_APPROVAL_REQUIRED"):
            state.send(units[1]["operation_id"], pair)
        with pytest.raises(MirrorStateError, match="NO_DELETE_DEPENDENCY"):
            state.send(
                units[1]["operation_id"],
                pair,
                allow_deletes=True,
                no_delete=True,
                confirmed_delete_count=2,
            )
        state.query_result(
            {
                "operation_id": units[1]["operation_id"],
                "state": "not_observed",
                "receipt": None,
                "disposition": None,
            }
        )
        with pytest.raises(MirrorStateError, match="UNCERTAIN"):
            state.close_unsent(units[1]["operation_id"])
        assert (
            state.read()["snapshots"][0]["deletion_context"]
            == snapshot["deletion_context"]
        )
        state.acknowledge(disposition(snapshot, units[1], "held_delete"))
        assert state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    assert len(state.read()["baselines"]) == 2
    assert (
        state.read()["last_complete_manifest_root"]
        == original["manifest"]["header"]["manifest_root"]
    )


@pytest.mark.parametrize(
    "n,d,needs_count",
    [(200, 20, False), (210, 21, True), (20, 2, False), (20, 3, True)],
)
def test_whole_push_bulk_boundaries(n, d, needs_count):
    state, pair = setup_state()
    snapshot, members = context(state, tuple(f"key{i}" for i in range(n)))
    units = []
    for offset in range(0, n, 100):
        unit = deepcopy(members[offset])
        unit["members"] = [u["members"][0] for u in members[offset : offset + 100]]
        unit["group_hash"] = group_hash(unit)
        unit["operation_hash"] = operation_hash(snapshot, unit)
        units.append(unit)
    snapshot["operation_ids"] = [u["operation_id"] for u in units]
    with state.apply():
        state.stage(snapshot, units)
    finish(state, snapshot, units)
    snapshot, units = deletes(state, d, confirm=d if needs_count else None)
    with state.apply():
        state.stage(snapshot, units)
        if needs_count:
            with pytest.raises(
                MirrorStateError, match="DELETE_COUNT_CONFIRMATION_REQUIRED"
            ):
                state.send(units[0]["operation_id"], pair, allow_deletes=True)
        state.send(
            units[0]["operation_id"],
            pair,
            allow_deletes=True,
            confirmed_delete_count=d if needs_count else None,
        )
    assert (
        state.read()["snapshots"][0]["deletion_context"]["plan"]["owned_item_count"]
        == n
    )


# Checkbox 9


def tree(root):
    result = {}
    for p in root.rglob("*"):
        result[str(p.relative_to(root))] = p.read_bytes() if p.is_file() else None
    return result


def sql_snapshot(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize("paired", [False, True])
def test_dry_run_directory_and_database_identical(paired, tmp_path):
    if paired:
        state, pair = setup_state()
    else:
        root = Path(os.environ["SMARTMEMORY_DATA_DIR"])
        backend = SQLiteBackend(str(root / "memory.db"))
        backend.close()
        state = MirrorState(root)
    before_tree = tree(tmp_path)
    before_sql = sql_snapshot(state.database)
    if paired:
        assert state.continuity(pair)
    else:
        with pytest.raises(MirrorStateError, match="PAIR_REQUIRED"):
            state.read()
        with pytest.raises(ValueError, match="SOURCE_IDENTITY_UNVERIFIED"):
            mirror_lite_namespace()
    assert tree(tmp_path) == before_tree
    assert sql_snapshot(state.database) == before_sql


def test_namespace_corruption_never_rotates_mirror_identity():
    state, pair = setup_state()
    namespace = Path(os.environ["SMARTMEMORY_CONFIG_DIR"]) / "okf_namespace.json"
    namespace.write_bytes(b"broken")
    before = namespace.read_bytes()
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_UNVERIFIED"):
        mirror_lite_namespace(create=True)
    assert namespace.read_bytes() == before
    assert read_mirror_source_identity(state.database) == pair["source_checkpoint"]


def test_dry_run_with_live_wal_is_identical(tmp_path):
    state, pair = setup_state()
    backend = SQLiteBackend(str(state.database))
    try:
        backend.add_node(
            "wal-row", {"content": "WAL content", "memory_type": "semantic"}
        )
        assert state.continuity(pair) == pair["source_checkpoint"]
        # Put the authoritative nonce only in committed WAL too: an immutable
        # main-file reader would return the wrong identity.
        changed = {**pair["source_checkpoint"], "checkpoint_nonce": "live-wal"}
        with closing(sqlite3.connect(state.database)) as writer:
            writer.execute(
                "UPDATE mirror_source_identity SET identity=?", (json.dumps(changed),)
            )
            writer.commit()
        before = tree(tmp_path)
        before_sql = sql_snapshot(state.database)
        with closing(
            sqlite3.connect(state.database.as_uri() + "?mode=ro", uri=True)
        ) as reader:
            logical_before = list(reader.iterdump())
            assert reader.execute(
                "SELECT item_id FROM nodes WHERE item_id='wal-row'"
            ).fetchone() == ("wal-row",)
        assert read_mirror_source_identity(state.database, read_only=True) == changed
        with pytest.raises(MirrorStateError, match="STORE_REPLACED"):
            state.continuity(pair)
        after = tree(tmp_path)
        companions = {
            str(state.database.relative_to(tmp_path)) + suffix
            for suffix in ("-wal", "-shm")
        }
        assert {k: v for k, v in after.items() if k not in companions} == {
            k: v for k, v in before.items() if k not in companions
        }
        assert sql_snapshot(state.database) == before_sql
        with closing(
            sqlite3.connect(state.database.as_uri() + "?mode=ro", uri=True)
        ) as reader:
            assert list(reader.iterdump()) == logical_before
    finally:
        backend.close()


def test_snapshot_without_operations_cannot_advance_complete_root():
    state, pair = setup_state()
    snapshot, units = stage(state, ("a",))
    finish(state, snapshot, units)
    prior = state.read()["last_complete_manifest_root"]
    with state.apply():
        identity = state.checkpoint(pair)
        snapshot, _ = context(state, ("a",), identity=identity)
        snapshot["operation_ids"] = []
        state.stage(snapshot, [])
        state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
    assert state.read()["last_complete_manifest_root"] == prior


def test_actual_resend_expanded_in_fresh_process(tmp_path):
    state, pair = setup_state()
    snapshot, units = stage(state)
    inputs = tmp_path / "pair.json"
    inputs.write_text(json.dumps(pair))
    script = f"""import json
from pathlib import Path
from smartmemory_app.mirror_state import MirrorState
s=MirrorState({str(state.data_dir)!r})
with s.apply():
    wire=s.send({units[1]["operation_id"]!r},json.loads(Path({str(inputs)!r}).read_text()))
    print(json.dumps(wire,ensure_ascii=False))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert canonical(json.loads(result.stdout)) == canonical(units[1])


def test_restore_missing_backup_never_mutates_state(tmp_path):
    from smartmemory_app.store_reset import restore_store_database

    state, pair = setup_state()
    before = tree(tmp_path)
    with pytest.raises(FileNotFoundError):
        restore_store_database(state.data_dir, tmp_path / "absent.db")
    assert tree(tmp_path) == before


def test_resolver_uses_data_override_without_initializing_store(tmp_path):
    from smartmemory_app.storage import get_mirror_state

    before = tree(tmp_path)
    state = get_mirror_state(str(tmp_path / "explicit"))
    assert state.data_dir == tmp_path / "explicit"
    assert tree(tmp_path) == before


def test_invocation_cannot_authorize_second_frozen_deletion_plan():
    from smartmemory_app.sync import DeletionInvocation

    state, pair = setup_state()
    snapshot, units = stage(state, ("t", "u", "v"))
    finish(state, snapshot, units)
    invocation = DeletionInvocation(allow_deletes=True, confirmed_delete_count=1)
    snapshot, units = deletes(state, 1, confirm=1)
    with state.apply():
        state.stage(snapshot, units)
        invocation.send(state, units[0]["operation_id"], pair)
        state.acknowledge(receipt(snapshot, units[0]))
        state.cleanup(snapshot["manifest"]["header"]["manifest_id"])
        # Select the next still-present owned key and keep the original invocation.
        newer, _ = context(state, ())
        baseline = next(
            b for b in state.read()["baselines"] if b["hosted_presence"] == "present"
        )
        member = {
            "action": "delete",
            "artifact": baseline["artifact"],
            "base": {k: baseline[k] for k in EMPTY_BASE},
            "reason": "source_absent",
            "source_hash": None,
            "dependencies": [],
        }
        unit = make_unit(newer, member)
        newer["operation_ids"] = [unit["operation_id"]]
        deletion_context(state, newer, [unit], confirm=1)
        # Membership excludes already ACKed tombstones, as the contract requires.
        plan = newer["deletion_context"]["plan"]
        owned = sorted(
            (b for b in state.read()["baselines"] if b["hosted_presence"] == "present"),
            key=lambda b: b["artifact"]["source_key"],
        )
        plan["owned_item_count"] = len(owned)
        plan["owned_membership_hash"] = digest(
            {
                "domain": "lite-mirror/owned-membership/v1",
                "members": [
                    {"artifact": b["artifact"], "base": {k: b[k] for k in EMPTY_BASE}}
                    for b in owned
                ],
            }
        )
        plan["plan_hash"] = digest(
            {
                "domain": "lite-mirror/delete-plan/v1",
                **{k: v for k, v in plan.items() if k != "plan_hash"},
            }
        )
        newer["deletion_context"]["approval"]["plan_hash"] = plan["plan_hash"]
        unit["operation_hash"] = operation_hash(newer, unit)
        state.stage(newer, [unit])
        with pytest.raises(MirrorStateError, match="another frozen deletion plan"):
            invocation.send(state, unit["operation_id"], pair)
        assert state.read()["pending"][0]["state"] == "staged"


def test_rebind_absent_key_never_regains_delete_authority():
    from smartmemory_app.mirror_recovery import rebind

    state, pair = setup_state()
    snapshot, units = stage(state)
    finish(state, snapshot, units)

    def fence(binding, recovery, identity, partition):
        return {
            **binding,
            "pair_generation": binding["pair_generation"] + 1,
            "source_checkpoint": identity,
            "recovery_mode": "retain_absent_keys",
        }

    with state.apply():
        rebound = rebind(
            state,
            reason="replacement",
            receipt_lookup=lambda p, ids: [],
            fence_partition=fence,
        )
        snapshot, units = deletes(state, 2, confirm=2)
        state.stage(snapshot, units)
        with pytest.raises(MirrorStateError, match="not present-and-ACKed"):
            state.send(
                units[0]["operation_id"],
                rebound,
                allow_deletes=True,
                confirmed_delete_count=2,
            )
    assert all(
        b["deletion_eligible_incarnation"] is None for b in state.read()["baselines"]
    )


def test_sigkill_releases_local_apply_lock(tmp_path):
    state, pair = setup_state()
    body = f"""from smartmemory_app.mirror_state import MirrorState
state=MirrorState({str(state.data_dir)!r})
held=state.apply()
held.__enter__()
"""
    killed_at(tmp_path, body)
    with state.apply():
        assert state.continuity(pair)


def test_receipt_id_cannot_be_reused_for_another_operation():
    state, pair = setup_state()
    snapshot, units = stage(state)
    with state.apply():
        first = receipt(snapshot, units[0])
        state.acknowledge(first)
        second = receipt(snapshot, units[1])
        second["receipt_id"] = first["receipt_id"]
        before = state.read()
        with pytest.raises(MirrorStateError, match="receipt ID already belongs"):
            state.acknowledge(second)
        assert state.read() == before


def test_reset_failure_preserves_same_root_namespace_and_state(monkeypatch):
    from smartmemory_app.store_reset import remove_store_files

    monkeypatch.setenv("SMARTMEMORY_CONFIG_DIR", os.environ["SMARTMEMORY_DATA_DIR"])
    state, pair = setup_state()
    snapshot, units = stage(state)
    with state.apply():
        state.acknowledge(receipt(snapshot, units[0]))
    before = state.read()
    # A real non-file at a store filename makes all-file preflight fail before
    # any unlink. No monkeypatch of the filesystem or storage methods.
    (state.data_dir / "blocked.db").mkdir()
    with pytest.raises(RuntimeError, match="Store reset refused"):
        remove_store_files(state.data_dir)
    assert state.database.exists()
    assert mirror_lite_namespace() == pair["source_checkpoint"]["namespace"]
    after = state.read()
    assert after["source_witness"]["state"] == "recovery_required"
    assert after["outcomes"] == before["outcomes"]
    assert after["baselines"] == before["baselines"]
    assert after["snapshots"] == before["snapshots"]


def test_dry_run_after_writer_crash_refuses_without_recovery_or_stale_read(tmp_path):
    from smartmemory_app.mirror_state import source_identity

    state, pair = setup_state()
    changed = {**pair["source_checkpoint"], "checkpoint_nonce": "committed-in-wal"}
    body = f"""import sqlite3
conn=sqlite3.connect({str(state.database)!r})
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('UPDATE mirror_source_identity SET identity=?',(json.dumps({changed!r}),))
conn.commit()
"""
    killed_at(tmp_path, body)
    # An exclusive recovery handle prevents another read-only handle from
    # obtaining a consistent snapshot of the crash-left WAL. Once it closes,
    # the committed WAL must be readable, never replaced by stale main bytes.
    with closing(sqlite3.connect(state.database)) as recovery:
        recovery.execute("PRAGMA locking_mode=EXCLUSIVE")
        recovery.execute("BEGIN EXCLUSIVE")
        before = tree(tmp_path)
        with pytest.raises(MirrorStateError, match="STORE_BUSY"):
            source_identity(state.database, read_only=True)
        assert tree(tmp_path) == before
        recovery.rollback()
    assert source_identity(state.database, read_only=True) == changed
    # An actual apply may use SQLite's WAL reader, and must observe the new
    # nonce instead of falsely trusting the older main-database checkpoint.
    with state.apply():
        assert source_identity(state.database) == changed
        with pytest.raises(MirrorStateError, match="STORE_REPLACED"):
            state.continuity(pair)


@pytest.mark.parametrize("mismatch", ["group_hash", "artifact", "effect", "base"])
def test_rebind_delete_requires_exact_removal_coverage_at_stage_and_send(mismatch):
    from smartmemory_app.mirror_integrity import freeze_unit
    from smartmemory_app.mirror_recovery import rebind

    state, _ = setup_state()
    original, units = stage(state, ("a",))
    finish(state, original, units)

    def fence(binding, recovery, identity, partition):
        return {
            **binding,
            "pair_generation": binding["pair_generation"] + 1,
            "source_checkpoint": identity,
            "recovery_mode": "retain_absent_keys",
        }

    with state.apply():
        pair = rebind(
            state,
            reason="replacement",
            receipt_lookup=lambda p, ids: [],
            fence_partition=fence,
        )
        snapshot, units = deletes(state, 1, confirm=1)
        plan = snapshot["deletion_context"]["plan"]
        removal = plan["removals"][0]
        if mismatch == "group_hash":
            removal[mismatch] = digest("wrong group")
        elif mismatch == "artifact":
            removal[mismatch] = {**removal[mismatch], "source_key": "wrong-key"}
        elif mismatch == "effect":
            removal[mismatch] = "retire"
            plan["item_delete_count"] = 0
        else:
            removal[mismatch] = {**removal[mismatch], "receipt_id": "wrong-receipt"}
        plan["deletion_set_hash"] = digest(
            {"domain": "lite-mirror/deletions/v1", "removals": plan["removals"]}
        )
        plan["plan_hash"] = digest(
            {
                "domain": "lite-mirror/delete-plan/v1",
                **{k: v for k, v in plan.items() if k != "plan_hash"},
            }
        )
        snapshot["deletion_context"]["approval"]["plan_hash"] = plan["plan_hash"]
        units[0]["operation_hash"] = operation_hash(snapshot, units[0])
        before = state.path.read_bytes()
        with pytest.raises(MirrorStateError, match="exact removal coverage"):
            state.stage(snapshot, units)
        assert state.path.read_bytes() == before
        # Simulate a schema-valid legacy checkpoint, with all hashes recomputed.
        doc = state.read()
        assert doc["baselines"][0]["deletion_eligible_incarnation"] is None
        doc["snapshots"].append(snapshot)
        doc["pending"].append(
            {
                "manifest_id": snapshot["manifest"]["header"]["manifest_id"],
                "unit": freeze_unit(units[0]),
                "state": "staged",
                "last_error": None,
                "snapshot_root": snapshot["manifest"]["header"]["manifest_root"],
                "terminal_state": None,
            }
        )
        state._save(doc)
        before = state.path.read_bytes()
        with pytest.raises(MirrorStateError, match="exact removal coverage"):
            state.send(
                units[0]["operation_id"],
                pair,
                allow_deletes=True,
                confirmed_delete_count=1,
            )
        assert state.path.read_bytes() == before


@pytest.mark.parametrize("method", ["stage", "extend"])
def test_same_call_same_key_refused_and_legacy_send_waits_after_not_observed(method):
    from smartmemory_app.mirror_integrity import freeze_unit

    state, pair = setup_state()
    snapshot, units = context(state, ("a",))
    duplicate = deepcopy(units[0])
    duplicate["operation_id"] = str(uuid4())
    duplicate["operation_hash"] = operation_hash(snapshot, duplicate)
    units.append(duplicate)
    with state.apply():
        if method == "stage":
            snapshot["operation_ids"].append(duplicate["operation_id"])
        else:
            snapshot["operation_ids"] = []
            state.stage(snapshot, [])
        before = state.path.read_bytes()
        with pytest.raises(MirrorStateError, match="UNCERTAIN"):
            if method == "stage":
                state.stage(snapshot, units)
            else:
                state.extend(snapshot["manifest"]["header"]["manifest_id"], units)
        assert state.path.read_bytes() == before
        # Real persisted legacy state exercises send's independent ordering gate.
        doc = state.read()
        if method == "stage":
            doc["snapshots"].append(snapshot)
        else:
            doc["snapshots"][0]["operation_ids"] = [u["operation_id"] for u in units]
        for unit in units:
            doc["pending"].append(
                {
                    "manifest_id": snapshot["manifest"]["header"]["manifest_id"],
                    "unit": freeze_unit(unit),
                    "state": "staged",
                    "last_error": None,
                    "snapshot_root": snapshot["manifest"]["header"]["manifest_root"],
                    "terminal_state": None,
                }
            )
        state._save(doc)
        wire = state.send(units[0]["operation_id"], pair)
        state.query_result(
            {
                "operation_id": units[0]["operation_id"],
                "state": "not_observed",
                "receipt": None,
                "disposition": None,
            }
        )
        before = state.path.read_bytes()
        with pytest.raises(MirrorStateError, match="earlier same-key"):
            state.send(duplicate["operation_id"], pair)
        assert state.path.read_bytes() == before
        assert state.send(units[0]["operation_id"], pair) == wire
        state.acknowledge(disposition(snapshot, units[0], "blocked"))
        assert state.send(duplicate["operation_id"], pair) == duplicate


@pytest.mark.parametrize(
    "field", ["depends_on_operations", "dependencies", "required_properties"]
)
@pytest.mark.parametrize("values", [["b", "a"], ["a", "a"]])
def test_non_normalized_contract_sets_refuse(field, values):
    from smartmemory_app.mirror_integrity import normalized_sets

    state, _ = setup_state()
    snapshot, units = context(state, ("a",))
    unit = units[0]
    if field == "required_properties":
        # Reuse the exact declaration shape used by the restore-flow fixture.
        payload_value = {
            "kind": "type_declaration",
            "layer": "private",
            "declaration": {
                "name": "Example",
                "iri": None,
                "display_name": None,
                "definition": None,
                "description": None,
                "aliases": [],
                "examples": [],
                "tier": "confirmed",
                "properties_schema": {},
                "kind": "record",
                "wikidata_qid": None,
                "required_properties": values,
                "storage_strategy": None,
                "storage_searchable": None,
                "parent_types": [],
                "superseded_by": None,
            },
            "references": [],
        }
        with pytest.raises(MirrorStateError, match="INVALID_REQUEST"):
            validate("TypeDeclarationPayload", payload_value)
        with pytest.raises(MirrorStateError, match="non-normalized set"):
            source_hash(
                {
                    "artifact": {
                        "kind": "type_declaration",
                        "source_key": "decl:example",
                    },
                    "payload": payload_value,
                }
            )
        # Identical names inside arbitrary authored JSON are not contract sets.
        normalized_sets({"payload": {"document": {"preserved": {field: values}}}})
        return
    if field == "depends_on_operations":
        unit[field] = values
    else:
        unit["members"][0][field] = [
            {
                "artifact": {"kind": "memory", "source_key": name},
                "expected_source_hash": digest(name),
                "required": True,
            }
            for name in values
        ]
    # Compute a valid digest of the non-normalized wire form independently of
    # the guarded helper. Validation must refuse even correctly rehashed input.
    header = snapshot["manifest"]["header"]
    unit["operation_hash"] = digest(
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
    with state.apply():
        before = state.path.read_bytes()
        with pytest.raises(MirrorStateError, match="INVALID_REQUEST"):
            state.stage(snapshot, units)
        assert state.path.read_bytes() == before
    with pytest.raises(MirrorStateError, match="non-normalized set"):
        operation_hash(snapshot, unit)
