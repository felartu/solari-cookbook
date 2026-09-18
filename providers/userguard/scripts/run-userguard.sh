#!/bin/bash
set -euo pipefail
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${PREFIX:-/opt/userguard}
BINARY=${BINARY:-$PREFIX/bin/userguard}
RUNDIR=${RUNDIR:-/run/userguard}
WG_PRIVATE_KEY_FILE=${WG_PRIVATE_KEY_FILE:-/run/secrets/userguard.key}
WG_LISTEN_PORT=${WG_LISTEN_PORT:-0}
WG_PERSISTENT_KEEPALIVE=${WG_PERSISTENT_KEEPALIVE:-25}
WG_MTU=${WG_MTU:-1420}
MAX_SESSIONS=${MAX_SESSIONS:-64}
VERBOSE=${VERBOSE:-0}

require() { local n=$1; [[ -n ${!n:-} ]] || { echo "missing required environment variable: $n" >&2; exit 2; }; }
require WG_ADDRESS
require WG_PEER_PUBLIC_KEY
require WG_ENDPOINT
require WG_ALLOWED_IPS
if [[ -z ${FORWARDS:-} && -z ${REVERSES:-} ]]; then
    echo 'at least one of FORWARDS or REVERSES is required' >&2
    exit 2
fi

mkdir -p "$RUNDIR"
chmod 0700 "$RUNDIR"
RUNDIR="$RUNDIR" "$HERE/scripts/stop-userguard.sh" >/dev/null 2>&1 || true
rm -f "$RUNDIR/userguard.log" "$RUNDIR/userguard.pid"

args=(
  --private-key-file "$WG_PRIVATE_KEY_FILE"
  --peer-public-key "$WG_PEER_PUBLIC_KEY"
  --peer-endpoint "$WG_ENDPOINT"
  --listen-port "$WG_LISTEN_PORT"
  --persistent-keepalive "$WG_PERSISTENT_KEEPALIVE"
  --mtu "$WG_MTU"
  --max-sessions "$MAX_SESSIONS"
)

IFS=',' read -r -a addr_items <<< "$WG_ADDRESS"
for v in "${addr_items[@]}"; do [[ -n $v ]] && args+=(--address "$v"); done
IFS=',' read -r -a allowed_items <<< "$WG_ALLOWED_IPS"
for v in "${allowed_items[@]}"; do [[ -n $v ]] && args+=(--allowed-ip "$v"); done
if [[ -n ${WG_DNS:-} ]]; then
  IFS=',' read -r -a dns_items <<< "$WG_DNS"
  for v in "${dns_items[@]}"; do [[ -n $v ]] && args+=(--dns "$v"); done
fi
if [[ -n ${WG_PRESHARED_KEY_FILE:-} ]]; then
  args+=(--peer-preshared-key-file "$WG_PRESHARED_KEY_FILE")
fi
if [[ -n ${FORWARDS:-} ]]; then
  IFS=';' read -r -a fwd_items <<< "$FORWARDS"
  for v in "${fwd_items[@]}"; do [[ -n $v ]] && args+=(--forward "$v"); done
fi
if [[ -n ${REVERSES:-} ]]; then
  IFS=';' read -r -a rev_items <<< "$REVERSES"
  for v in "${rev_items[@]}"; do [[ -n $v ]] && args+=(--reverse "$v"); done
fi
if [[ $VERBOSE == 1 || $VERBOSE == true ]]; then args+=(--verbose); fi

"$BINARY" "${args[@]}" >"$RUNDIR/userguard.log" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$RUNDIR/userguard.pid"

for _ in $(seq 1 100); do
    if grep -q 'USERGUARD_READY' "$RUNDIR/userguard.log" 2>/dev/null; then
        echo "USERGUARD_RUNTIME_READY pid=$pid"
        exit 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        echo USERGUARD_RUNTIME_FAILED >&2
        tail -100 "$RUNDIR/userguard.log" >&2 || true
        exit 10
    fi
    sleep 0.1
done

echo USERGUARD_READY_TIMEOUT >&2
tail -100 "$RUNDIR/userguard.log" >&2 || true
exit 11
