#!/usr/bin/env bash
# DIST-AGENT-HOOKS-1: Distill phase — Stop hook (async)
# Pairs last_assistant_message with stored prompt
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
mkdir -p "$HOOK_DATA_DIR"
{ cat | smartmemory lifecycle distill 2>>"$HOOK_DATA_DIR/hooks.log"; } &
disown
exit 0
