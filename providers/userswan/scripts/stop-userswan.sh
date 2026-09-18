#!/bin/sh
set -eu
RUNDIR=${RUNDIR:-/run/userswan}

proc_live() {
    p=$1
    [ -r "/proc/$p/stat" ] || return 1
    state=$(awk '{print $3}' "/proc/$p/stat" 2>/dev/null || true)
    [ -n "$state" ] && [ "$state" != "Z" ]
}

for name in forwarder charon; do
    f="$RUNDIR/$name.pid"
    if [ -f "$f" ]; then
        p=$(cat "$f" 2>/dev/null || true)
        if [ -n "$p" ] && proc_live "$p"; then
            kill "$p" 2>/dev/null || true
            for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
                proc_live "$p" || break
                sleep 0.1
            done
            if proc_live "$p"; then
                kill -9 "$p" 2>/dev/null || true
            fi
        fi
        rm -f "$f"
    fi
done
rm -f "$RUNDIR/forwarder.sock" "$RUNDIR/notun.sock" "$RUNDIR/charon.vici"
echo USERSWAN_STOPPED
