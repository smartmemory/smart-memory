#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Learn phase — PostToolUseFailure hook (async)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
# Capture stdin before backgrounding: non-interactive async jobs inherit /dev/null.
if ! mkdir -p "$HOOK_DATA_DIR/tmp"; then
    echo "WARNING: lifecycle learn payload lost: cannot create hook temp directory" >&2
    exit 0
fi
PAYLOAD_FILE=$(mktemp "$HOOK_DATA_DIR/tmp/learn.XXXXXX" 2>>"$HOOK_DATA_DIR/hooks.log") || {
    echo "WARNING: lifecycle learn payload lost: cannot create payload file" >>"$HOOK_DATA_DIR/hooks.log"
    exit 0
}
if ! cat >"$PAYLOAD_FILE" 2>>"$HOOK_DATA_DIR/hooks.log"; then
    echo "WARNING: lifecycle learn payload lost: cannot capture stdin" >>"$HOOK_DATA_DIR/hooks.log"
    rm -f "$PAYLOAD_FILE"
    exit 0
fi
{
    trap 'rm -f "$PAYLOAD_FILE"' EXIT
    smartmemory lifecycle learn <"$PAYLOAD_FILE"
} >/dev/null 2>>"$HOOK_DATA_DIR/hooks.log" &
disown
exit 0
