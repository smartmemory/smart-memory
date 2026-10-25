#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Learn phase — PostToolUseFailure hook
PHASE=learn
# HOOK-WRITE-QUEUE-1: append the payload to the durable hook queue and return. No store
# write happens here and no process is started (local mode): the daemon is the one consumer
# (smartmemory_app/hook_consumer.py) and writes jobs in order per session, with retries and a
# dead/ folder. While the daemon is down jobs wait; past SMARTMEMORY_HOOK_QUEUE_MAX queued jobs
# (default 2000, counted only while no consumer is running) the new payload is shed with a WARNING in hooks.log. The other loss path is a
# queue that cannot be written ("payload lost"). Pure bash on purpose: a Python start alone
# costs more than the hook's 50 ms budget. The three write hooks share this body.
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
HOOK_MARKER_DIR="$HOME/.smartmemory"
QUEUE_DIR="$HOOK_DATA_DIR/hook-queue"
HOOK_LOG="$HOOK_DATA_DIR/hooks.log"
JOB_FILES=()
lost() {
    echo "WARNING: lifecycle $PHASE payload lost: $1 (HOOK-WRITE-QUEUE-1)" >>"$HOOK_LOG" 2>/dev/null ||
        echo "WARNING: lifecycle $PHASE payload lost: $1 (HOOK-WRITE-QUEUE-1)" >&2
    [ "${#JOB_FILES[@]}" -gt 0 ] && rm -f "${JOB_FILES[@]}"
    exit 0
}
json_str() { # json_str VAR VALUE: VAR becomes VALUE as a JSON string, without a subshell
    local value=${2//[[:cntrl:]]/}
    value=${value//\\/\\\\}
    printf -v "$1" '"%s"' "${value//\"/\\\"}"
}
if [ ! -d "$QUEUE_DIR/queue" ] || [ ! -d "$QUEUE_DIR/payloads" ] || [ ! -d "$HOOK_MARKER_DIR" ]; then
    mkdir -p "$HOOK_DATA_DIR" "$HOOK_MARKER_DIR" "$QUEUE_DIR/queue" "$QUEUE_DIR/payloads" 2>/dev/null ||
        lost "cannot create $QUEUE_DIR"
fi
# WORKER_PID becomes the pid in worker.pid while it is alive AND is a smartmemory consumer
# (the daemon or the remote drainer), else empty, so a pid reused after a daemon crash does
# not count (one ps, ~3 ms).
live_consumer() {
    WORKER_PID=
    [ -f "$QUEUE_DIR/worker.pid" ] && IFS= read -r WORKER_PID <"$QUEUE_DIR/worker.pid"
    if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
        WORKER_CMD=$(ps -p "$WORKER_PID" -o command= 2>/dev/null)
        case $WORKER_CMD in
        *smartmemory_app.viewer_server* | *" lifecycle drain-hooks"*) ;;
        *) WORKER_PID= ;;
        esac
    else
        WORKER_PID=
    fi
}
# Depth cap, checked only when no live consumer holds worker.pid (the daemon writes it): a
# glob over a deep queue costs ~200 ms at 5000 jobs, so the steady state skips it.
live_consumer
if [ -z "$WORKER_PID" ]; then
    QUEUE_MAX="${SMARTMEMORY_HOOK_QUEUE_MAX:-2000}"
    LC_ALL=C
    shopt -s nullglob
    QUEUED=("$QUEUE_DIR"/queue/*.json)
    shopt -u nullglob
    unset LC_ALL
    if [ "${#QUEUED[@]}" -ge "$QUEUE_MAX" ] 2>/dev/null; then
        # Name what is lost: session and tool from the head of the payload, then drain stdin.
        HEAD=
        IFS= read -r -d '' -n 4096 HEAD
        cat >/dev/null
        SHED_SESSION=-
        SHED_TOOL=-
        re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-]+)"'
        [[ $HEAD =~ $re ]] && SHED_SESSION=${BASH_REMATCH[1]}
        re='"tool_name"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_.:-]+)"'
        [[ $HEAD =~ $re ]] && SHED_TOOL=${BASH_REMATCH[1]}
        echo "WARNING: lifecycle $PHASE payload shed: session $SHED_SESSION tool $SHED_TOOL; hook queue holds ${#QUEUED[@]} jobs (cap $QUEUE_MAX) and no consumer is running; is the daemon up? (HOOK-WRITE-QUEUE-1)" >>"$HOOK_LOG" 2>/dev/null
        exit 0
    fi
    QUEUED=()
fi
set -o noclobber
for _ in 1 2 3; do
    JOB_ID="$$-$RANDOM$RANDOM"
    PAYLOAD_FILE="$QUEUE_DIR/payloads/$JOB_ID.$PHASE.json"
    { : >"$PAYLOAD_FILE"; } 2>/dev/null && break
    PAYLOAD_FILE=
done
[ -n "$PAYLOAD_FILE" ] || lost "cannot create a payload file in $QUEUE_DIR/payloads"
JOB_FILES=("$PAYLOAD_FILE")
cat >>"$PAYLOAD_FILE" 2>>"$HOOK_LOG" || lost "cannot capture stdin"
# Session id and cwd from the head of the payload, by bash regex (the drainer reads the payload itself).
IFS= read -r -d '' -n 4096 HEAD <"$PAYLOAD_FILE"
SESSION_ID=
SESSION_JSON=null
CWD_JSON=null
WORKSPACE_JSON=null
STATE_JSON=null
re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-]+)"'
[[ $HEAD =~ $re ]] && SESSION_ID=${BASH_REMATCH[1]} && SESSION_JSON="\"$SESSION_ID\""
re='"cwd"[[:space:]]*:[[:space:]]*"([^"\\]*)"'
[[ $HEAD =~ $re ]] && json_str CWD_JSON "${BASH_REMATCH[1]}"
[ -n "${SMARTMEMORY_WORKSPACE_ID:-}" ] && json_str WORKSPACE_JSON "$SMARTMEMORY_WORKSPACE_ID"
if [ "$PHASE" = distill ] && [ -n "$SESSION_ID" ] && [ -f "$HOOK_DATA_DIR/sessions/$SESSION_ID.json" ]; then
    # Snapshot the prompt this response answers before a later prompt replaces it.
    STATE_FILE="$QUEUE_DIR/payloads/$JOB_ID.state.json"
    if IFS= read -r -d '' STATE_BODY <"$HOOK_DATA_DIR/sessions/$SESSION_ID.json"; [ -n "$STATE_BODY" ] &&
        printf '%s' "$STATE_BODY" >"$STATE_FILE" 2>>"$HOOK_LOG"; then
        JOB_FILES=("${JOB_FILES[@]}" "$STATE_FILE")
        json_str STATE_JSON "$STATE_FILE"
    else
        echo "WARNING: lifecycle distill prompt pairing lost: cannot snapshot session state (HOOK-WRITE-QUEUE-1)" >>"$HOOK_LOG"
    fi
fi
json_str PAYLOAD_JSON "$PAYLOAD_FILE"
RECORD_TMP="$QUEUE_DIR/queue/.$JOB_ID.$PHASE.tmp"
JOB_FILES=("${JOB_FILES[@]}" "$RECORD_TMP")
printf '{"v":1,"phase":"%s","session_id":%s,"cwd":%s,"workspace_id":%s,"payload_path":%s,"state_snapshot":%s}\n' \
    "$PHASE" "$SESSION_JSON" "$CWD_JSON" "$WORKSPACE_JSON" "$PAYLOAD_JSON" "$STATE_JSON" >"$RECORD_TMP" 2>>"$HOOK_LOG" ||
    lost "cannot write the queue record"
mv -f "$RECORD_TMP" "$QUEUE_DIR/queue/$JOB_ID.$PHASE.json" 2>>"$HOOK_LOG" || lost "cannot publish the queue record"
# Published. Local mode: the daemon picks it up, nothing to start. Remote mode has no daemon:
# start the remote drainer only when worker.pid names no live consumer. Read it again here,
# after the publish, never the value from the cap check: the drainer clears worker.pid, then
# rescans, then exits, and it may have done all three while this hook waited on stdin. A live
# pid read after the publish means that rescan is still ahead and sees this record; an empty
# one means start a drainer (HOOK-WRITE-QUEUE-REMOTE-EXIT-FU-1).
if [ -f "$QUEUE_DIR/remote-drain" ]; then
    live_consumer
    if [ -z "$WORKER_PID" ]; then
        {
            smartmemory lifecycle drain-hooks --dir "$QUEUE_DIR"
            HOOK_EXIT=$?
            if [ "$HOOK_EXIT" -ne 0 ]; then
                printf '%s\t%s\t%s\n' "$PHASE" "$HOOK_EXIT" "${EPOCHSECONDS:-0}" >>"$HOOK_MARKER_DIR/hook-failures.tsv"
            fi
        } </dev/null >/dev/null 2>>"$HOOK_LOG" &
        disown
    fi
fi
exit 0
