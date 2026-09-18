# Configuration

`userguard` can be invoked directly or through `scripts/run-userguard.sh`.

## Direct binary options

Required core options:

```text
--address PREFIX                  repeatable local tunnel address/prefix
--private-key-file PATH          mode-0600 local WireGuard private key
--peer-public-key KEY            peer public key, standard base64 or 64 hex chars
--peer-endpoint HOST:PORT        physical WireGuard UDP endpoint
--allowed-ip PREFIX              repeatable peer AllowedIPs
```

At least one relay mapping is required:

```text
--forward LISTEN=TARGET
--reverse TUNNEL_LISTEN=TARGET
```

Optional:

```text
--peer-preshared-key-file PATH
--listen-port PORT               default 0 (ephemeral)
--persistent-keepalive SECONDS   default 25
--dns IP                         repeatable userspace DNS server
--mtu MTU                        default 1420
--max-sessions N                 default 64
--verbose
```

## Forward mapping

```bash
--forward 127.0.0.1:3390=10.20.30.40:3389
```

The left side is a native host listener. The right side is dialed through the gVisor/WireGuard userspace stack.

Multiple `--forward` flags may be supplied.

## Reverse mapping

```bash
--reverse 10.77.0.2:8080=127.0.0.1:19090
```

The left side is a listener inside the userspace WireGuard address space. The right side is a native host TCP destination.

## Environment wrapper

`scripts/run-userguard.sh` accepts:

```text
WG_ADDRESS=10.77.0.2/32[,IPv6-prefix]
WG_PRIVATE_KEY_FILE=/run/secrets/userguard.key
WG_PEER_PUBLIC_KEY=<base64-public-key>
WG_PRESHARED_KEY_FILE=/run/secrets/userguard.psk   # optional
WG_ENDPOINT=198.51.100.10:51820
WG_ALLOWED_IPS=10.20.30.0/24[,more-prefixes]
WG_DNS=10.20.30.53[,more-addresses]                 # optional
WG_LISTEN_PORT=0
WG_PERSISTENT_KEEPALIVE=25
WG_MTU=1420
MAX_SESSIONS=64
FORWARDS='127.0.0.1:3390=10.20.30.40:3389;...'
REVERSES='10.77.0.2:8080=127.0.0.1:19090;...'
RUNDIR=/run/userguard
PREFIX=/opt/userguard
```

`FORWARDS` and `REVERSES` use semicolons between mappings. Do not put secrets directly in these environment variables.

## WireGuard key formats

Private and preshared key files accept the standard 32-byte WireGuard base64 format produced by `wg genkey`/`wg genpsk`, or a 64-character hexadecimal representation.

The peer public key is not secret and may be passed directly on the command line.

## AllowedIPs behavior

The userspace netstack has generic IP routes internally, but wireguard-go only associates/encrypts packets for the configured peer's `AllowedIPs`. Configure the narrowest prefixes appropriate for the protected services.
