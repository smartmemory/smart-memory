#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Recall phase — UserPromptSubmit hook
# Always captures prompt for distill pairing; optionally injects context (blocking)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
HOOK_MARKER_DIR="$HOME/.smartmemory"
mkdir -p "$HOOK_DATA_DIR" "$HOOK_MARKER_DIR"
# HOOK-LOAD-CAP-1: skip (no context injected, never blocks the prompt) when load1 is over SMARTMEMORY_HOOK_LOAD_MAX. 0 = no skip.
LOAD_MAX="${SMARTMEMORY_HOOK_LOAD_MAX:-48}"
if [ "$LOAD_MAX" -gt 0 ] 2>/dev/null; then
    LOAD1=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
    if [ -n "$LOAD1" ] && awk -v l="$LOAD1" -v m="$LOAD_MAX" 'BEGIN{exit !(l+0>m+0)}'; then
        echo "WARNING: lifecycle recall skipped: load1 $LOAD1 over $LOAD_MAX (SMARTMEMORY_HOOK_LOAD_MAX, HOOK-LOAD-CAP-1)" >>"$HOOK_DATA_DIR/hooks.log"
        cat >/dev/null
        exit 0
    fi
fi
cat | smartmemory lifecycle recall 2>>"$HOOK_DATA_DIR/hooks.log"
    HOOK_EXIT=$?
    if [ "$HOOK_EXIT" -ne 0 ]; then
        printf '%s\t%s\t%s\n' 'recall' "$HOOK_EXIT" "${EPOCHSECONDS:-0}" >>"$HOOK_MARKER_DIR/hook-failures.tsv"
    fi
exit 0
