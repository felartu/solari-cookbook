#!/bin/sh
set -eu
PREFIX=${STRONGSWAN_PREFIX:-/opt/userswan/strongswan}
RUNDIR=${RUNDIR:-/run/userswan}
export STRONGSWAN_CONF="$RUNDIR/strongswan.conf"
export SWANCTL_DIR="$RUNDIR/swanctl"
printf '%s\n' '=== processes ==='
for f in "$RUNDIR/charon.pid" "$RUNDIR/forwarder.pid"; do
    if [ -f "$f" ]; then
        p=$(cat "$f" 2>/dev/null || true)
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo "running pid=$p file=$f"; else echo "stale file=$f pid=$p"; fi
    fi
done
printf '%s\n' '=== listener ==='
command -v ss >/dev/null && ss -ltnp 2>/dev/null | grep userswan || true
printf '%s\n' '=== IKE/CHILD SAs ==='
"$PREFIX/sbin/swanctl" --list-sas 2>/dev/null || true
printf '%s\n' '=== forwarder tail ==='
tail -40 "$RUNDIR/forwarder.log" 2>/dev/null || true
