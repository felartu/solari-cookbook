#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${PREFIX:-/opt/userguard}
GOOS=${GOOS:-linux}
GOARCH=${GOARCH:-$(go env GOARCH)}
CGO_ENABLED=${CGO_ENABLED:-0}
export GOOS GOARCH CGO_ENABLED

case "$GOARCH" in
  amd64) export GOAMD64=${GOAMD64:-v1} ;;
  arm64) : ;;
  *) : ;;
esac

mkdir -p "$PREFIX/bin"
cd "$HERE"
go mod verify
go build -trimpath -buildvcs=false -ldflags '-s -w' -o "$PREFIX/bin/userguard" ./cmd/userguard
chmod 0755 "$PREFIX/bin/userguard"
printf 'USERGUARD_BUILD_OK binary=%s goos=%s goarch=%s cgo=%s\n' "$PREFIX/bin/userguard" "$GOOS" "$GOARCH" "$CGO_ENABLED"
