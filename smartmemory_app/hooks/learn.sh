#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Learn phase — PostToolUseFailure hook (async)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
mkdir -p "$HOOK_DATA_DIR"
{ cat | smartmemory lifecycle learn 2>>"$HOOK_DATA_DIR/hooks.log"; } &
disown
exit 0
