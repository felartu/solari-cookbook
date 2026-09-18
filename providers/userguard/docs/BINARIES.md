# Binary and instruction-set notes

## `userguard`

Default scripted install path:

```text
/opt/userguard/bin/userguard
```

Primary role:

```text
WireGuard-go device + gVisor userspace TCP/IP + TCP relay mappings
```

Typical direct invocation:

```bash
/opt/userguard/bin/userguard \
  --address 10.77.0.2/32 \
  --private-key-file /run/secrets/userguard.key \
  --peer-public-key BASE64_PEER_PUBLIC_KEY \
  --peer-endpoint 198.51.100.10:51820 \
  --allowed-ip 10.20.30.0/24 \
  --persistent-keepalive 25 \
  --forward 127.0.0.1:3390=10.20.30.40:3389
```

Important log markers:

```text
FORWARD_READY
REVERSE_READY
USERGUARD_READY
SESSION_CONNECTED
SESSION_CONNECT_FAIL
SESSION_COPY_FAIL
SESSION_CLOSED
USERGUARD_STOPPING
USERGUARD_STOPPED
```

`USERGUARD_READY` means the WireGuard device and configured relay listeners are active. WireGuard itself is demand-driven, so peer reachability is proven by actual tunnel traffic or by verbose handshake logs, not by `USERGUARD_READY` alone.

## CPU instruction set

The project is pure Go when built with `CGO_ENABLED=0`. There is no userguard-specific assembly.

For Linux amd64, the repository check explicitly builds with:

```bash
GOOS=linux GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0
```

`GOAMD64=v1` is the conservative x86-64 baseline and avoids requiring newer AVX-class CPUs.

For Linux arm64:

```bash
GOOS=linux GOARCH=arm64 CGO_ENABLED=0
```

Go's standard ARM64 baseline is used unless you intentionally add architecture-specific toolchain settings.

Inspect a binary with:

```bash
file /opt/userguard/bin/userguard
readelf -h /opt/userguard/bin/userguard
```

Because `CGO_ENABLED=0` is used by the reference build, `ldd` will normally report that the binary is not dynamically linked.

## Cross-compilation examples

AMD64 baseline:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1 \
  go build -trimpath -buildvcs=false -o userguard-linux-amd64 ./cmd/userguard
```

ARM64:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
  go build -trimpath -buildvcs=false -o userguard-linux-arm64 ./cmd/userguard
```

Do not copy an amd64 binary to an arm64 host or vice versa.
