#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

PREFIX=${STRONGSWAN_PREFIX:-/opt/userswan/strongswan}
FORWARDER=${FORWARDER:-/opt/userswan/bin/userswan-forwarder}
RUNDIR=${RUNDIR:-/run/userswan}
PSK_FILE=${PSK_FILE:-/run/secrets/userswan.psk}
CONNECTION=${CONNECTION:-userswan}
CHILD=${CHILD:-userswan-child}
LOCAL_IP=${LOCAL_IP:-127.0.0.1}
LOCAL_PORT=${LOCAL_PORT:-3390}
IKE_PORT=${IKE_PORT:-15000}
NATT_PORT=${NATT_PORT:-14500}
CHILD_REKEY_TIME=${CHILD_REKEY_TIME:-1h}
CHILD_LIFE_TIME=${CHILD_LIFE_TIME:-70m}
CHILD_RAND_TIME=${CHILD_RAND_TIME:-5m}
CHILDLESS=${CHILDLESS:-allow}
MAX_SESSIONS=${MAX_SESSIONS:-32}

require() { eval "v=\${$1:-}"; [ -n "$v" ] || { echo "missing required environment variable: $1" >&2; exit 2; }; }
require VPN_GATEWAY
require LOCAL_ID
require REMOTE_ID
require REMOTE_TS
require TARGET_IP
require TARGET_PORT

mkdir -p "$RUNDIR" "$RUNDIR/swanctl"
chmod 700 "$RUNDIR"

cat > "$RUNDIR/strongswan.conf" <<EOF
charon {
    load_modular = yes
    port = $IKE_PORT
    port_nat_t = $NATT_PORT
    routing_table = 0
    install_routes = no
    install_virtual_ip = no
    plugins {
        include $PREFIX/etc/strongswan.d/charon/*.conf
        kernel-libipsec {
            load = 2
            no_tun = yes
            packet_socket = $RUNDIR/notun.sock
        }
        kernel-netlink {
            load = 1
        }
        vici {
            socket = unix://$RUNDIR/charon.vici
        }
    }
}
swanctl {
    socket = unix://$RUNDIR/charon.vici
}
EOF
chmod 600 "$RUNDIR/strongswan.conf"

python3 "$HERE/scripts/render-swanctl.py" \
    --psk-file "$PSK_FILE" \
    --output "$RUNDIR/swanctl/swanctl.conf" \
    --gateway "$VPN_GATEWAY" \
    --local-id "$LOCAL_ID" \
    --remote-id "$REMOTE_ID" \
    --remote-ts "$REMOTE_TS" \
    --connection "$CONNECTION" \
    --child "$CHILD" \
    --rekey-time "$CHILD_REKEY_TIME" \
    --life-time "$CHILD_LIFE_TIME" \
    --rand-time "$CHILD_RAND_TIME" \
    --childless "$CHILDLESS"

for d in x509 x509ca x509ocsp x509aa x509ac x509crl pubkey private rsa ecdsa pkcs8 pkcs12; do
    mkdir -p "$RUNDIR/swanctl/$d"
done

RUNDIR="$RUNDIR" "$HERE/scripts/stop-userswan.sh" >/dev/null 2>&1 || true
rm -f "$RUNDIR/charon.vici" "$RUNDIR/notun.sock" "$RUNDIR/forwarder.sock" "$RUNDIR/charon.log" "$RUNDIR/forwarder.log"

STRONGSWAN_CONF="$RUNDIR/strongswan.conf" "$PREFIX/libexec/ipsec/charon" >"$RUNDIR/charon.log" 2>&1 &
charon_pid=$!
printf '%s\n' "$charon_pid" > "$RUNDIR/charon.pid"
for i in $(seq 1 100); do
    [ -S "$RUNDIR/charon.vici" ] && [ -S "$RUNDIR/notun.sock" ] && break
    kill -0 "$charon_pid" 2>/dev/null || { echo CHARON_FAILED; tail -100 "$RUNDIR/charon.log"; exit 10; }
    sleep 0.1
done
[ -S "$RUNDIR/charon.vici" ] || { echo VICI_SOCKET_MISSING; exit 11; }
[ -S "$RUNDIR/notun.sock" ] || { echo PACKET_SOCKET_MISSING; exit 12; }

export STRONGSWAN_CONF="$RUNDIR/strongswan.conf"
export SWANCTL_DIR="$RUNDIR/swanctl"
SW="$PREFIX/sbin/swanctl"
"$SW" --load-all
"$SW" --initiate --child "$CHILD" --timeout 30
raw=$($SW --list-sas --raw)
vip=$(printf '%s\n' "$raw" | sed -n 's/.*local-vips=\[\([0-9][0-9.]*\)\].*/\1/p' | head -1)
[ -n "$vip" ] || { echo VIP_NOT_FOUND; printf '%s\n' "$raw"; exit 13; }

"$FORWARDER" \
    --vip "$vip" \
    --packet-socket "$RUNDIR/notun.sock" \
    --bind-socket "$RUNDIR/forwarder.sock" \
    --listen-ip "$LOCAL_IP" \
    --listen-port "$LOCAL_PORT" \
    --remote-ip "$TARGET_IP" \
    --remote-port "$TARGET_PORT" \
    --max-sessions "$MAX_SESSIONS" \
    >"$RUNDIR/forwarder.log" 2>&1 &
forwarder_pid=$!
printf '%s\n' "$forwarder_pid" > "$RUNDIR/forwarder.pid"
for i in $(seq 1 100); do
    grep -q '^LISTENER_READY ' "$RUNDIR/forwarder.log" 2>/dev/null && break
    kill -0 "$forwarder_pid" 2>/dev/null || { echo FORWARDER_FAILED; tail -100 "$RUNDIR/forwarder.log"; exit 14; }
    sleep 0.1
done
grep -q '^LISTENER_READY ' "$RUNDIR/forwarder.log" || { echo LISTENER_NOT_READY; exit 15; }

printf 'USERSWAN_READY vip=%s listen=%s:%s target=%s:%s gateway=%s\n' \
    "$vip" "$LOCAL_IP" "$LOCAL_PORT" "$TARGET_IP" "$TARGET_PORT" "$VPN_GATEWAY"
printf 'status: %s/scripts/status-userswan.sh\n' "$HERE"
printf 'stop:   %s/scripts/stop-userswan.sh\n' "$HERE"
