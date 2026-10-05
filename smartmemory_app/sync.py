import os


def push_if_configured() -> None:
    """Push local memories to SmartMemory cloud.

    No-op if SMARTMEMORY_SYNC_TOKEN is not set.
    Raises NotImplementedError if token is set — sync not yet implemented.
    """
    if not os.environ.get("SMARTMEMORY_SYNC_TOKEN"):
        return
    raise NotImplementedError(
        "Backend sync not yet implemented. Coming in DIST-PLUGIN-2. "
        "Unset SMARTMEMORY_SYNC_TOKEN to suppress this error."
    )


def pull_if_configured() -> None:
    """Pull memories from SmartMemory cloud.

    No-op if SMARTMEMORY_SYNC_TOKEN is not set.
    """
    if not os.environ.get("SMARTMEMORY_SYNC_TOKEN"):
        return
    raise NotImplementedError(
        "Backend sync not yet implemented. Coming in DIST-PLUGIN-2. "
        "Unset SMARTMEMORY_SYNC_TOKEN to suppress this error."
    )


class DeletionInvocation:
    """One approved frozen deletion plan per actual invocation (U7 adapter).

    This object is never persisted. Receipt lookup is always allowed. A later
    snapshot cannot reset D/N or consume a second approval during resume.
    """

    def __init__(
        self,
        *,
        allow_deletes=False,
        no_delete=False,
        confirmed_delete_count=None,
        interactive_consent=False,
    ):
        self.allow_deletes = allow_deletes
        self.no_delete = no_delete
        self.confirmed_delete_count = confirmed_delete_count
        self.interactive_consent = interactive_consent
        self.plan_hash = None

    def send(self, state, operation_id, hosted_pair):
        from smartmemory_app.mirror_integrity import inferred_removal, refuse

        doc = state.read()
        row = state._row(doc, operation_id)
        snapshot = state._snapshot(doc, row["manifest_id"])
        is_destructive = inferred_removal(snapshot, row["unit"])
        plan_hash = None
        if is_destructive:
            context = snapshot["deletion_context"]
            plan_hash = context["plan"]["plan_hash"] if context else None
            if self.plan_hash is not None and self.plan_hash != plan_hash:
                refuse(
                    "DELETE_APPROVAL_REQUIRED",
                    "another frozen deletion plan already used this invocation",
                )
        wire = state.send(
            operation_id,
            hosted_pair,
            allow_deletes=self.allow_deletes,
            no_delete=self.no_delete,
            confirmed_delete_count=self.confirmed_delete_count,
            interactive_consent=self.interactive_consent,
        )
        if is_destructive:
            self.plan_hash = plan_hash
        return wire
