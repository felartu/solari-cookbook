# Security

## Private keys

Never pass the local private key or optional preshared key directly on the command line or in environment variables. userguard accepts them by file path and requires each secret file to be mode `0600` or stricter.

Recommended:

```text
/run/secrets/userguard.key
/run/secrets/userguard.psk
```

The runtime never intentionally logs secret key material.

## Peer public key

A WireGuard peer public key is not secret. It may be supplied directly as `--peer-public-key` or through `WG_PEER_PUBLIC_KEY`.

## Local listeners

A forward such as:

```text
127.0.0.1:3390=10.20.30.40:3389
```

is loopback-only. Changing the left side to `0.0.0.0`, `[::]`, or a non-loopback address exposes the protected service according to normal host network/firewall reachability.

## Reverse listeners

A reverse listener exists only inside the gVisor/WireGuard userspace address space. Its backend target is a native host TCP destination. Treat reverse mappings as an explicit service-exposure mechanism.

## AllowedIPs

Use narrow `AllowedIPs`. They are both WireGuard peer routing/association policy and a security boundary for which protected destination addresses are sent to that peer.

## Privileges

userguard does not require `/dev/net/tun` or creation of a kernel WireGuard interface. With a non-privileged WireGuard UDP listen port and non-privileged native TCP listeners, it can generally run without `CAP_NET_ADMIN`.

Host/container policy may still restrict ordinary UDP/TCP sockets, filesystem access, or low numbered ports.

## Cryptography

Do not replace wireguard-go's Noise/WireGuard protocol implementation with custom cryptography. userguard's purpose is to compose upstream WireGuard protocol code with a userspace network stack and relay layer.

## Verbose logging

`--verbose` enables wireguard-go protocol diagnostics and may expose peer public-key abbreviations, endpoint information, timings, and networking metadata. It should not expose private keys, but verbose logs should still be handled as operationally sensitive.
