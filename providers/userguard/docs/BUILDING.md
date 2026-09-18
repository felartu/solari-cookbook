# Building

## Pinned upstream dependency

The repository pins:

```text
golang.zx2c4.com/wireguard v0.0.0-20250521234502-f333402bd9cb
```

This corresponds to the upstream `wireguard-go` 0.0.20250522 snapshot line. The build also pins the transitive gVisor revision in `go.sum`/`go.mod` resolution.

## Toolchain

Reference build environment:

```text
Go 1.24.x
Linux x86_64/amd64
CGO disabled
```

Install dependencies on Debian/Ubuntu-like systems:

```bash
sudo ./scripts/install-deps-debian.sh
```

`wireguard-tools` is used for key generation and the integration self-test. The `userguard` runtime binary itself does not invoke `wg`.

## Standard build

```bash
PREFIX=/opt/userguard ./scripts/build.sh
```

Equivalent manual build:

```bash
CGO_ENABLED=0 go mod verify
CGO_ENABLED=0 go build \
  -trimpath \
  -buildvcs=false \
  -ldflags '-s -w' \
  -o userguard \
  ./cmd/userguard
```

## Makefile

```bash
make
sudo make install PREFIX=/usr/local
```

## Verification

```bash
make check
```

The repository check performs:

- `gofmt` verification;
- `go mod verify`;
- `go vet ./...`;
- `go test ./...`;
- Linux amd64/GOAMD64=v1 cross-build;
- Linux arm64 cross-build;
- shell syntax checks;
- secret/runtime filename scan.

For a real encrypted no-TUN data-path test:

```bash
make selftest
```

## Reproducibility

Keep `go.mod` and `go.sum` committed. Do not run `go get ...@latest` as part of production image builds. Update the pinned WireGuard dependency intentionally, run `make check` and `make selftest`, and review upstream changes before committing the updated module files.
