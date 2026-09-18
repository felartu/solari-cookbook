#!/bin/sh
set -eu
RUNDIR=${RUNDIR:-/run/userguard}
printf '%s\n' '=== process ==='
if [ -f "$RUNDIR/userguard.pid" ]; then
    p=$(cat "$RUNDIR/userguard.pid" 2>/dev/null || true)
    if [ -n "$p" ] && [ -r "/proc/$p/stat" ]; then
        state=$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null || true)
        if [ "$state" != "Z" ]; then echo "running pid=$p state=$state"; else echo "zombie pid=$p"; fi
    else
        echo "stale pidfile"
    fi
else
    echo "not running"
fi
printf '%s\n' '=== native sockets ==='
command -v ss >/dev/null 2>&1 && ss -lntup 2>/dev/null | grep userguard || true
printf '%s\n' '=== log tail ==='
tail -60 "$RUNDIR/userguard.log" 2>/dev/null || true
