#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Recall phase — UserPromptSubmit hook
# Always captures prompt for distill pairing; optionally injects context (blocking)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
mkdir -p "$HOOK_DATA_DIR"
cat | smartmemory lifecycle recall 2>>"$HOOK_DATA_DIR/hooks.log"
exit 0
