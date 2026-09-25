#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Distill phase — Stop hook (async)
# Pairs last_assistant_message with stored prompt
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
# Capture stdin before backgrounding: non-interactive async jobs inherit /dev/null.
if ! mkdir -p "$HOOK_DATA_DIR/tmp"; then
    echo "WARNING: lifecycle distill payload lost: cannot create hook temp directory" >&2
    exit 0
fi
PAYLOAD_FILE=$(mktemp "$HOOK_DATA_DIR/tmp/distill.XXXXXX" 2>>"$HOOK_DATA_DIR/hooks.log") || {
    echo "WARNING: lifecycle distill payload lost: cannot create payload file" >>"$HOOK_DATA_DIR/hooks.log"
    exit 0
}
if ! cat >"$PAYLOAD_FILE" 2>>"$HOOK_DATA_DIR/hooks.log"; then
    echo "WARNING: lifecycle distill payload lost: cannot capture stdin" >>"$HOOK_DATA_DIR/hooks.log"
    rm -f "$PAYLOAD_FILE"
    exit 0
fi
{
    trap 'rm -f "$PAYLOAD_FILE"' EXIT
    smartmemory lifecycle distill <"$PAYLOAD_FILE"
} >/dev/null 2>>"$HOOK_DATA_DIR/hooks.log" &
disown
exit 0
