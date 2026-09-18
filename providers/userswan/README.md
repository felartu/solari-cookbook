# userswan

`userswan` is a Linux userspace IPsec TCP-forwarding proof of concept built around strongSwan's existing IKEv2, authentication, CHILD_SA, ESP, replay-protection and NAT-T implementations.

The key change is a small patch to the strongSwan `kernel-libipsec` plugin: in **no-TUN mode**, cleartext inner IPv4 packets are exchanged with a userspace process over an `AF_UNIX/SOCK_DGRAM` packet socket instead of `/dev/net/tun`. A companion lwIP-based forwarder exposes a normal localhost TCP listener and implements the remote TCP leg entirely in userspace.

```text
application
    |
    | TCP to 127.0.0.1:LOCAL_PORT
    v
userswan-forwarder
    |                       Linux kernel owns only the localhost leg
    | lwIP remote TCP
    v
complete inner IPv4/TCP packets
    |
    | AF_UNIX datagrams
    v
patched strongSwan kernel-libipsec
    |
    | ESP + NAT-T
    v
VPN gateway : UDP/4500
    |
    v
protected remote TCP service
```

## What this does not do

It does **not** implement IKE, ESP cryptography, key derivation, replay protection, or NAT traversal itself. Those remain strongSwan responsibilities. It also does not use Linux XFRM for the protected data path and does not require a TUN device for packet injection/extraction.

The current forwarder is deliberately small: IPv4 only and one fixed TCP mapping per process. It supports concurrent localhost sessions by allocating one independent lwIP TCP socket/PCB and remote ephemeral source port per accepted connection, while sharing the single userswan netif and strongSwan packet socket. Treat it as a reference/PoC, not as a production multi-tenant proxy.

## Repository layout

- `patches/strongswan-6.0.7/` — patch adding `kernel-libipsec.no_tun` and the Unix packet interface.
- `src/userswan-forwarder.c` — lwIP TCP proxy and packet adapter.
- `scripts/build-strongswan.sh` — pinned strongSwan download, hash verification, patch, test and install.
- `scripts/build-forwarder.sh` — compile/install the lwIP forwarder.
- `scripts/render-swanctl.py` — safely renders a mode-0600 PSK swanctl config.
- `scripts/run-userswan.sh` — starts charon, establishes the CHILD_SA, discovers the assigned VIP and starts the localhost listener.
- `scripts/status-userswan.sh`, `scripts/stop-userswan.sh` — runtime operations.
- `examples/` — configuration and RDP test example.
- `docs/` — internals, build details, binary/ISA notes and security model.

## Quick start

On Debian/Ubuntu-like systems:

```bash
sudo ./scripts/install-deps-debian.sh
sudo PREFIX=/opt/userswan/strongswan ./scripts/build-strongswan.sh
sudo PREFIX=/opt/userswan ./scripts/build-forwarder.sh
```

Create a PSK file **outside** the repository. strongSwan accepts `0s...` base64 or `0x...` hexadecimal encoded secrets:

```bash
sudo install -d -m 0700 /run/secrets
sudo sh -c 'printf "%s\n" "0sREPLACE_WITH_YOUR_BASE64_SECRET" > /run/secrets/userswan.psk'
sudo chmod 0600 /run/secrets/userswan.psk
```

Export your VPN and forwarding parameters. The values below are documentation-only examples:

```bash
export VPN_GATEWAY=198.51.100.10
export LOCAL_ID=userswan-client
export REMOTE_ID=198.51.100.10
export REMOTE_TS=10.20.30.40/32
export TARGET_IP=10.20.30.40
export TARGET_PORT=3389
export LOCAL_IP=127.0.0.1
export LOCAL_PORT=3390
export PSK_FILE=/run/secrets/userswan.psk
```

Then start the userspace IPsec forwarder:

```bash
sudo -E ./scripts/run-userswan.sh
```

A successful startup prints:

```text
USERSWAN_READY vip=<assigned-vip> listen=127.0.0.1:3390 target=10.20.30.40:3389 gateway=198.51.100.10
```

Test the localhost listener with the client appropriate to your service. For RDP:

```bash
python3 examples/rdp-local-probe.py --host 127.0.0.1 --port 3390 --timeout 20
```

To validate Remmina-style parallel TCP setup, run three simultaneous negotiations:

```bash
python3 tests/concurrent-rdp-probe.py \
  --host 127.0.0.1 --port 3390 --connections 3 --hold 20
```

All sessions should report `OK`, and the forwarder log should show distinct lwIP ephemeral source ports for each `REMOTE_CONNECT_OK session=...` line.

See `docs/BUILDING.md`, `docs/ARCHITECTURE.md`, `docs/BINARIES.md`, `docs/SECURITY.md`, `docs/REKEYING.md`, and `docs/TROUBLESHOOTING.md` before adapting this code.
