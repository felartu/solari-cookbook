# userguard

`userguard` is a userspace WireGuard TCP forwarding runtime that does **not** create or use `/dev/net/tun` and does not create a kernel WireGuard interface.

It reuses upstream `wireguard-go` for the WireGuard protocol/cryptography and its `tun/netstack` backend (gVisor netstack) for userspace IPv4/IPv6 TCP/IP. The host kernel only owns the physical UDP socket used by WireGuard and any explicitly configured native localhost/listener sockets.

```text
application
    |
    | native TCP, e.g. 127.0.0.1:3390
    v
userguard
    |
    | gVisor userspace TCP/IP
    v
wireguard-go device
    |
    | encrypted WireGuard UDP
    v
peer endpoint
    |
    v
protected TCP service
```

The reverse direction is also supported: a TCP listener can exist inside the userspace tunnel address space and proxy to a native host service.

## Design goals

- No TUN device.
- No kernel WireGuard interface.
- No host routes required for protected prefixes.
- No custom WireGuard cryptography.
- Standard WireGuard private/public/preshared keys.
- Concurrent TCP sessions.
- Generic local TCP mappings, not RDP-specific.

## Quick start

Install build tools on Debian/Ubuntu-like systems:

```bash
sudo ./scripts/install-deps-debian.sh
```

Build:

```bash
sudo PREFIX=/opt/userguard ./scripts/build.sh
```

Generate a standard WireGuard key pair:

```bash
./scripts/genkey.sh /run/secrets/userguard.key /tmp/userguard.pub
```

Example client environment:

```bash
export WG_ADDRESS=10.77.0.2/32
export WG_PRIVATE_KEY_FILE=/run/secrets/userguard.key
export WG_PEER_PUBLIC_KEY='BASE64_PEER_PUBLIC_KEY'
export WG_ENDPOINT=198.51.100.10:51820
export WG_ALLOWED_IPS=10.20.30.0/24
export WG_PERSISTENT_KEEPALIVE=25
export FORWARDS='127.0.0.1:3390=10.20.30.40:3389'
```

Start:

```bash
sudo -E ./scripts/run-userguard.sh
```

The runtime is ready when it prints:

```text
USERGUARD_RUNTIME_READY
```

For multiple mappings:

```bash
export FORWARDS='127.0.0.1:3390=10.20.30.40:3389;127.0.0.1:4450=10.20.30.40:445'
```

## No-TUN integration test

The repository includes a real encrypted two-peer test:

```bash
make selftest
```

It starts two userguard instances on loopback UDP, creates a WireGuard handshake, sends TCP through both gVisor netstacks, proxies to a native echo backend, validates the returned payload, and checks that neither process opened `/dev/net/tun`.

Expected:

```text
USERGUARD_SELFTEST_OK payload_roundtrip=1
```

See `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/BUILDING.md`, `docs/BINARIES.md`, and `docs/SECURITY.md`.
