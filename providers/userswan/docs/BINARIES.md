# Binaries, invocation and CPU instruction-set notes

## `charon`

Path with the default build:

```text
/opt/userswan/strongswan/libexec/ipsec/charon
```

Role: IKEv2 daemon plus libipsec ESP/NAT-T processing. It is started directly by `scripts/run-userswan.sh` with `STRONGSWAN_CONF` pointing at a generated runtime config.

Manual launch shape:

```bash
STRONGSWAN_CONF=/run/userswan/strongswan.conf \
  /opt/userswan/strongswan/libexec/ipsec/charon
```

## `swanctl`

Path:

```text
/opt/userswan/strongswan/sbin/swanctl
```

Role: VICI control client. Runtime commands used by this project:

```bash
export STRONGSWAN_CONF=/run/userswan/strongswan.conf
export SWANCTL_DIR=/run/userswan/swanctl
/opt/userswan/strongswan/sbin/swanctl --load-all
/opt/userswan/strongswan/sbin/swanctl --initiate --child userswan-child --timeout 30
/opt/userswan/strongswan/sbin/swanctl --list-sas
/opt/userswan/strongswan/sbin/swanctl --list-sas --raw
```

## `userswan-forwarder`

Default path:

```text
/opt/userswan/bin/userswan-forwarder
```

Role: kernel localhost listener + lwIP remote TCP endpoint + Unix packet adapter.

Required arguments:

```text
--vip IP
--packet-socket PATH
--remote-ip IP
--remote-port PORT
```

Optional arguments:

```text
--bind-socket PATH
--listen-ip IP        default 127.0.0.1
--listen-port PORT    default 3390
--max-sessions N      default 32
```

Example:

```bash
/opt/userswan/bin/userswan-forwarder \
  --vip 10.254.250.2 \
  --packet-socket /run/userswan/notun.sock \
  --bind-socket /run/userswan/forwarder.sock \
  --listen-ip 127.0.0.1 \
  --listen-port 3390 \
  --remote-ip 10.20.30.40 \
  --remote-port 3389 \
  --max-sessions 32
```

Important log markers:

```text
LISTENER_READY
LOCAL_ACCEPT
REMOTE_CONNECT_START
TX_INNER
RX_INNER
REMOTE_CONNECT_OK
PROXY local_to_remote
PROXY remote_to_local
LOCAL_SESSION_CLOSED
```

`REMOTE_CONNECT_OK` implies lwIP completed the TCP three-way handshake through the IPsec tunnel. It includes `session=<id>` plus the allocated lwIP `local=<VIP>:<ephemeral-port>`, which is useful for proving concurrent tuple separation. `RX_INNER` proves decrypted plaintext IP packets returned from libipsec to lwIP.

## Python utilities

`scripts/render-swanctl.py` creates the swanctl configuration while enforcing mode-0600 PSK permissions and never prints the secret.

`examples/rdp-local-probe.py` is an RDP-specific acceptance client. It connects only to the localhost listener, sends a TPKT/X.224 RDP negotiation request and requires a TPKT response.

## CPU architecture / instruction set

The project source contains no hand-written assembly and the forwarder uses no architecture-specific SIMD intrinsics. The binary ISA is therefore determined by the compiler and flags used on the target host.

Tested reference environment: Linux `x86_64`/AMD64.

Expected portable build targets include `aarch64`/ARM64 when strongSwan, lwIP and the required development libraries are available for that target. Build natively or with a correctly configured cross toolchain; do not copy an x86_64 binary to ARM64 or vice versa.

Inspect a built binary with:

```bash
file /opt/userswan/bin/userswan-forwarder
readelf -h /opt/userswan/bin/userswan-forwarder
ldd /opt/userswan/bin/userswan-forwarder
```

For conservative portable builds, do not add `-march=native`. If you intentionally use architecture tuning (for example `-march=x86-64-v3`), document that deployment requirement because the resulting binary may not execute on older CPUs.

strongSwan's crypto backend may itself use optimized CPU-specific code supplied by OpenSSL or other linked crypto libraries. That optimization is independent of the userswan packet-forwarding code.
