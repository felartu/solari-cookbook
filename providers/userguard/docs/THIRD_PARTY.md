# Third-party software

userguard depends on upstream WireGuard and gVisor components through Go modules.

Primary dependency:

```text
golang.zx2c4.com/wireguard
```

wireguard-go is published by WireGuard LLC under the MIT license.

Its `tun/netstack` backend depends on gVisor components. Review the exact versions in `go.mod` and `go.sum` and comply with their applicable licenses when distributing source or binaries.

The repository's original userguard code is licensed under the MIT license in `LICENSE`. That license does not replace or modify third-party license obligations.

For reproducible releases, archive or otherwise record the exact `go.mod`, `go.sum`, Go toolchain version, `GOOS`, `GOARCH`, and `GOAMD64` used to produce each binary.
