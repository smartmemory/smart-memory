#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Observe phase — PostToolUse hook (async)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
HOOK_MARKER_DIR="$HOME/.smartmemory"
mkdir -p "$HOOK_DATA_DIR" "$HOOK_MARKER_DIR"
# Capture stdin before backgrounding: non-interactive async jobs inherit /dev/null.
if ! mkdir -p "$HOOK_DATA_DIR/tmp"; then
    echo "WARNING: lifecycle observe payload lost: cannot create hook temp directory" >&2
    exit 0
fi
# Purge only stale files created by these three async hooks.
PURGED_COUNT=$(find "$HOOK_DATA_DIR/tmp" -maxdepth 1 -type f \
    \( -name 'observe.??????' -o -name 'learn.??????' -o -name 'distill.??????' \) \
    -mmin +60 -delete -print 2>>"$HOOK_DATA_DIR/hooks.log" | wc -l | tr -d '[:space:]')
if [ "$PURGED_COUNT" -gt 0 ]; then
    echo "WARNING: purged $PURGED_COUNT stale lifecycle hook payload file(s)" >>"$HOOK_DATA_DIR/hooks.log"
fi
PAYLOAD_FILE=$(mktemp "$HOOK_DATA_DIR/tmp/observe.XXXXXX" 2>>"$HOOK_DATA_DIR/hooks.log") || {
    echo "WARNING: lifecycle observe payload lost: cannot create payload file" >>"$HOOK_DATA_DIR/hooks.log"
    exit 0
}
if ! cat >"$PAYLOAD_FILE" 2>>"$HOOK_DATA_DIR/hooks.log"; then
    echo "WARNING: lifecycle observe payload lost: cannot capture stdin" >>"$HOOK_DATA_DIR/hooks.log"
    rm -f "$PAYLOAD_FILE"
    exit 0
fi
{
    smartmemory lifecycle observe <"$PAYLOAD_FILE"
    HOOK_EXIT=$?
    if [ "$HOOK_EXIT" -ne 0 ]; then
        printf '%s\t%s\t%s\n' 'observe' "$HOOK_EXIT" "${EPOCHSECONDS:-0}" >>"$HOOK_MARKER_DIR/hook-failures.tsv"
    fi
    rm -f "$PAYLOAD_FILE"
} >/dev/null 2>>"$HOOK_DATA_DIR/hooks.log" &
disown
exit 0
