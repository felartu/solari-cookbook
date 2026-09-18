# Architecture

## Components

`userguard` deliberately reuses upstream components instead of implementing VPN cryptography:

- `golang.zx2c4.com/wireguard/device` — WireGuard protocol state machine, Noise handshake, key rotation, packet encryption/decryption, replay protection.
- `golang.zx2c4.com/wireguard/conn` — native UDP transport to the physical WireGuard peer.
- `golang.zx2c4.com/wireguard/tun/netstack` — in-memory TUN-compatible device backed by gVisor's userspace network stack.
- userguard relay code — native/listener TCP sockets, session accounting, and stream copying.

No packet cryptography, Noise handshake logic, or WireGuard packet format code is reimplemented here.

## Outbound forward path

For a mapping such as:

```text
127.0.0.1:3390 = 10.20.30.40:3389
```

traffic flows as:

```text
native application
    -> kernel TCP 127.0.0.1:3390
    -> userguard accepts connection
    -> gVisor netstack DialContext("tcp", "10.20.30.40:3389")
    -> inner IPv4/IPv6 TCP packets
    -> wireguard-go device
    -> encrypted WireGuard UDP
    -> peer endpoint
```

Each accepted native TCP connection gets an independent gVisor TCP endpoint and source port. The WireGuard device is shared by all sessions.

## Reverse path

A reverse mapping such as:

```text
10.77.0.2:8080 = 127.0.0.1:19090
```

creates a TCP listener inside the userspace tunnel namespace:

```text
WireGuard peer
    -> encrypted UDP
    -> wireguard-go decrypt
    -> gVisor TCP listener 10.77.0.2:8080
    -> userguard session
    -> native TCP dial 127.0.0.1:19090
```

This is useful for tests and for exposing a native local service to a WireGuard peer without adding a kernel WireGuard interface.

## Why no TUN is needed

`netstack.CreateNetTUN()` returns an object that satisfies wireguard-go's `tun.Device` interface, but packet reads/writes are backed by an in-memory gVisor network stack instead of `/dev/net/tun`. WireGuard therefore sees a normal packet-oriented TUN abstraction while Linux never receives a TUN interface.

## Host-kernel responsibilities

The Linux kernel still owns:

- the physical UDP socket used to reach the WireGuard endpoint;
- native TCP listeners used by `--forward`;
- native TCP connections used as reverse-mapping backends.

It does not own protected-prefix routing or the userspace TCP endpoints carried through WireGuard.

## Concurrency

All mappings share one WireGuard device and one gVisor stack. Each accepted TCP connection is handled in its own relay goroutine and gets its own userspace TCP endpoint. `--max-sessions` bounds the number of concurrent active relay sessions.

## Current scope

- One WireGuard peer per process.
- TCP forwarding only.
- IPv4 and IPv6 tunnel addresses are accepted by the underlying netstack.
- Multiple outbound and reverse TCP mappings per process.
- No SOCKS/HTTP proxy protocol.
- No kernel route or interface integration.
- No UDP application forwarding yet.
