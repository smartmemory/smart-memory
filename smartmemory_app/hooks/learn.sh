#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Learn phase — PostToolUseFailure hook (async)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
# Capture stdin before backgrounding: non-interactive async jobs inherit /dev/null.
if ! mkdir -p "$HOOK_DATA_DIR/tmp"; then
    echo "WARNING: lifecycle learn payload lost: cannot create hook temp directory" >&2
    exit 0
fi
# Purge only stale files created by these three async hooks.
PURGED_COUNT=$(find "$HOOK_DATA_DIR/tmp" -maxdepth 1 -type f \
    \( -name 'observe.??????' -o -name 'learn.??????' -o -name 'distill.??????' \) \
    -mmin +60 -delete -print 2>>"$HOOK_DATA_DIR/hooks.log" | wc -l | tr -d '[:space:]')
if [ "$PURGED_COUNT" -gt 0 ]; then
    echo "WARNING: purged $PURGED_COUNT stale lifecycle hook payload file(s)" >>"$HOOK_DATA_DIR/hooks.log"
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
    smartmemory lifecycle learn <"$PAYLOAD_FILE"
    rm -f "$PAYLOAD_FILE"
} >/dev/null 2>>"$HOOK_DATA_DIR/hooks.log" &
disown
exit 0
