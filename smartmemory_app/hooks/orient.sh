#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Orient phase — SessionStart hook
# Reads hook JSON from stdin, outputs context to stdout (blocking)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
mkdir -p "$HOOK_DATA_DIR"
cat | smartmemory lifecycle orient 2>>"$HOOK_DATA_DIR/hooks.log"
exit 0
