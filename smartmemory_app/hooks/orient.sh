#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Orient phase — SessionStart hook
# Reads hook JSON from stdin, outputs context to stdout (blocking)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
mkdir -p "$HOOK_DATA_DIR"
cat | smartmemory lifecycle orient 2>>"$HOOK_DATA_DIR/hooks.log"
exit 0
