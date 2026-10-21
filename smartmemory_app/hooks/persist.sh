#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Persist phase — SessionEnd hook (durable enqueue)
# Acknowledge the local ledger before exit; only the importer runs detached.
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
HOOK_MARKER_DIR="$HOME/.smartmemory"
mkdir -p "$HOOK_DATA_DIR" "$HOOK_MARKER_DIR"
cat | smartmemory lifecycle persist 2>>"$HOOK_DATA_DIR/hooks.log"
    HOOK_EXIT=$?
    if [ "$HOOK_EXIT" -ne 0 ]; then
        printf '%s\t%s\t%s\n' 'persist' "$HOOK_EXIT" "${EPOCHSECONDS:-0}" >>"$HOOK_MARKER_DIR/hook-failures.tsv"
    fi
exit 0
