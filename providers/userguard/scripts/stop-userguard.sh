#!/bin/sh
set -eu
RUNDIR=${RUNDIR:-/run/userguard}
PIDFILE="$RUNDIR/userguard.pid"
proc_live() {
    p=$1
    [ -r "/proc/$p/stat" ] || return 1
    state=$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null || true)
    [ -n "$state" ] && [ "$state" != "Z" ]
}
if [ -f "$PIDFILE" ]; then
    p=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$p" ] && proc_live "$p"; then
        kill "$p" 2>/dev/null || true
        for _ in $(seq 1 50); do proc_live "$p" || break; sleep 0.1; done
        if proc_live "$p"; then kill -9 "$p" 2>/dev/null || true; fi
    fi
    rm -f "$PIDFILE"
fi
echo USERGUARD_STOPPED
