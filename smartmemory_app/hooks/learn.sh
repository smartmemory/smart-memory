#!/usr/bin/env bash
# Hook JSON and memory context must survive Windows consoles and pipes.
export PYTHONUTF8=1
# DIST-AGENT-HOOKS-1: Learn phase — PostToolUseFailure hook (async)
HOOK_DATA_DIR="${SMARTMEMORY_DATA_DIR:-$HOME/.smartmemory}"
HOOK_MARKER_DIR="$HOME/.smartmemory"
mkdir -p "$HOOK_DATA_DIR" "$HOOK_MARKER_DIR"
# HOOK-SPILL-INTERIM-1: over-cap payloads are spilled to disk for HOOK-WRITE-QUEUE-1's drain instead of discarded.
# One JSON file per payload: {"v":1,"phase","ts","pid","payload"}; written to hook-spill/.tmp-* then mv'd (atomic).
# Caps: SMARTMEMORY_HOOK_SPILL_MAX_FILES (20000) / _MAX_BYTES (500 MB); over cap = dropped with a WARNING. SMARTMEMORY_HOOK_SPILL=0 = old discard.
# One perl process does count, bytes, envelope and write (one fork; bash+ls+du cost ~15 ms more). Bytes are summed by stat on every call
# below 1000 files and every 25th call above, so a near-full folder overshoots the byte cap by < 25 payloads.
spill_payload() {
    local phase="$1" dir="$HOOK_DATA_DIR/hook-spill" log="$HOOK_DATA_DIR/hooks.log" out rc
    local max_files="${SMARTMEMORY_HOOK_SPILL_MAX_FILES:-20000}" max_bytes="${SMARTMEMORY_HOOK_SPILL_MAX_BYTES:-524288000}"
    local tmp="$dir/.tmp-$$-$phase"
    if ! out=$(mkdir -p "$dir" 2>&1); then
        echo "WARNING: lifecycle $phase spill failed: cannot create $dir: $out (HOOK-SPILL-INTERIM-1)" >>"$log"
        cat >/dev/null
        return 0
    fi
    out=$(perl -MJSON::PP -MTime::HiRes=gettimeofday -e '
        my ($phase, $dir, $tmp, $pid, $maxf, $maxb) = @ARGV;
        local $/; binmode STDIN; my $p = <STDIN>; $p = "" unless defined $p;
        opendir(my $d, $dir) or do { print "cannot read $dir: $!"; exit 2 };
        my @f = grep { /\.json\z/ } readdir($d); closedir($d);
        my $n = @f; my $b = 0;
        if ($n < 1000 || $n % 25 == 0) { for (@f) { my $s = (stat("$dir/$_"))[7]; $b += $s if defined $s } }
        if ($n >= $maxf || $b >= $maxb) { print "files=$n bytes=$b, cap files=$maxf bytes=$maxb"; exit 3 }
        my ($s, $u) = gettimeofday; my @t = gmtime($s);
        my $stem = sprintf("%04d%02d%02dT%02d%02d%02d%06dZ", $t[5]+1900, $t[4]+1, $t[3], $t[2], $t[1], $t[0], $u);
        my $iso = sprintf("%04d-%02d-%02dT%02d:%02d:%02d.%06dZ", $t[5]+1900, $t[4]+1, $t[3], $t[2], $t[1], $t[0], $u);
        (my $raw = $p) =~ s/\s+\z//;
        my $ok = eval { JSON::PP->new->utf8->allow_nonref->decode($raw); 1 };
        if (!$ok || $raw eq "") { my $c = $p; utf8::decode($c); $raw = JSON::PP->new->ascii->allow_nonref->encode($c); }
        open(my $o, ">", $tmp) or do { print "open $tmp: $!"; exit 2 };
        binmode $o;
        print $o "{\"v\":1,\"phase\":\"$phase\",\"ts\":\"$iso\",\"pid\":$pid,\"payload\":$raw}\n" or do { print "write $tmp: $!"; exit 2 };
        close($o) or do { print "close $tmp: $!"; exit 2 };
        print $stem;
    ' "$phase" "$dir" "$tmp" "$$" "$max_files" "$max_bytes" 2>&1)
    rc=$?
    if [ "$rc" -eq 3 ]; then
        echo "WARNING: lifecycle $phase spill full: dropped ($out) (HOOK-SPILL-INTERIM-1)" >>"$log"
    elif [ "$rc" -ne 0 ]; then
        echo "WARNING: lifecycle $phase spill failed: $out (HOOK-SPILL-INTERIM-1)" >>"$log"
        rm -f "$tmp"
    elif ! out=$(mv "$tmp" "$dir/$out-$$-$phase.json" 2>&1); then
        echo "WARNING: lifecycle $phase spill failed: rename: $out (HOOK-SPILL-INTERIM-1)" >>"$log"
        rm -f "$tmp"
    fi
    return 0
}
# HOOK-LOAD-CAP-1: at most SMARTMEMORY_LEARN_MAX learn runs machine-wide; extra calls drop their payload with one log line. 0 = no cap.
LEARN_MAX="${SMARTMEMORY_LEARN_MAX:-2}"
if [ "$LEARN_MAX" -gt 0 ] 2>/dev/null; then
    RUNNING=$(pgrep -f 'smartmemory lifecycle learn' 2>/dev/null | wc -l | tr -d '[:space:]')
    if [ "${RUNNING:-0}" -ge "$LEARN_MAX" ]; then
        if [ "${SMARTMEMORY_HOOK_SPILL:-1}" != "0" ]; then
            spill_payload learn
        else
            echo "WARNING: lifecycle learn skipped: $RUNNING already running (cap $LEARN_MAX, HOOK-LOAD-CAP-1)" >>"$HOOK_DATA_DIR/hooks.log"
            cat >/dev/null
        fi
        exit 0
    fi
fi
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
    HOOK_EXIT=$?
    if [ "$HOOK_EXIT" -ne 0 ]; then
        printf '%s\t%s\t%s\n' 'learn' "$HOOK_EXIT" "${EPOCHSECONDS:-0}" >>"$HOOK_MARKER_DIR/hook-failures.tsv"
    fi
    rm -f "$PAYLOAD_FILE"
} >/dev/null 2>>"$HOOK_DATA_DIR/hooks.log" &
disown
exit 0
