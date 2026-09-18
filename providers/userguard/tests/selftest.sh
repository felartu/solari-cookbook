#!/bin/bash
set -euo pipefail
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d /tmp/userguard-selftest.XXXXXX)
cleanup() {
  set +e
  for f in a.pid b.pid backend.pid; do
    [[ -f "$TMP/$f" ]] || continue
    kill "$(cat "$TMP/$f")" 2>/dev/null || true
  done
  rm -rf "$TMP"
}
trap cleanup EXIT

command -v wg >/dev/null || { echo 'selftest requires wireguard-tools (wg)' >&2; exit 2; }
command -v python3 >/dev/null || { echo 'selftest requires python3' >&2; exit 2; }

BIN=${BINARY:-$TMP/userguard}
if [[ ! -x "$BIN" ]]; then
  (cd "$HERE" && CGO_ENABLED=0 go build -trimpath -buildvcs=false -o "$BIN" ./cmd/userguard)
fi

cd "$TMP"
umask 077
wg genkey > a.key
wg genkey > b.key
wg pubkey < a.key > a.pub
wg pubkey < b.key > b.pub

cat > echo_server.py <<'PY'
import socket
s=socket.socket()
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',19090))
s.listen()
while True:
    c,_=s.accept()
    with c:
        while True:
            d=c.recv(65536)
            if not d:
                break
            c.sendall(d)
PY
python3 echo_server.py >backend.log 2>&1 & echo $! > backend.pid

A_PUB=$(cat a.pub)
B_PUB=$(cat b.pub)
"$BIN" \
  --address 10.77.0.2/32 \
  --private-key-file "$TMP/b.key" \
  --peer-public-key "$A_PUB" \
  --peer-endpoint 127.0.0.1:51820 \
  --listen-port 51821 \
  --allowed-ip 10.77.0.1/32 \
  --persistent-keepalive 1 \
  --reverse 10.77.0.2:8080=127.0.0.1:19090 \
  >b.log 2>&1 & echo $! > b.pid
"$BIN" \
  --address 10.77.0.1/32 \
  --private-key-file "$TMP/a.key" \
  --peer-public-key "$B_PUB" \
  --peer-endpoint 127.0.0.1:51821 \
  --listen-port 51820 \
  --allowed-ip 10.77.0.2/32 \
  --persistent-keepalive 1 \
  --forward 127.0.0.1:18080=10.77.0.2:8080 \
  >a.log 2>&1 & echo $! > a.pid

for _ in $(seq 1 100); do
  grep -q USERGUARD_READY a.log 2>/dev/null && grep -q USERGUARD_READY b.log 2>/dev/null && break
  sleep 0.1
done
grep -q USERGUARD_READY a.log
grep -q USERGUARD_READY b.log

# The in-memory netstack must not open /dev/net/tun.
for p in "$(cat a.pid)" "$(cat b.pid)"; do
  if find "/proc/$p/fd" -maxdepth 1 -type l -exec readlink {} \; 2>/dev/null | grep -q '^/dev/net/tun$'; then
    echo "SELFTEST_FAIL pid=$p opened /dev/net/tun" >&2
    exit 20
  fi
done

python3 - <<'PY'
import socket,time,sys
payload=b'userguard-no-tun-selftest\n'
last=None
for _ in range(30):
    try:
        s=socket.create_connection(('127.0.0.1',18080),timeout=2)
        s.settimeout(2)
        s.sendall(payload)
        data=s.recv(4096)
        s.close()
        if data == payload:
            print('USERGUARD_SELFTEST_OK payload_roundtrip=1')
            sys.exit(0)
        last=RuntimeError(f'unexpected payload {data!r}')
    except Exception as e:
        last=e
    time.sleep(.2)
raise SystemExit(f'USERGUARD_SELFTEST_FAIL: {last}')
PY

printf '%s\n' '=== peer A evidence ==='
grep -E 'USERGUARD_READY|SESSION_CONNECTED|SESSION_CLOSED' a.log | tail -20
printf '%s\n' '=== peer B evidence ==='
grep -E 'USERGUARD_READY|SESSION_CONNECTED|SESSION_CLOSED' b.log | tail -20
