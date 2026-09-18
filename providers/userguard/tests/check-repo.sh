#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$HERE"

[ -z "$(gofmt -l ./cmd)" ] || { echo 'gofmt changes required' >&2; gofmt -l ./cmd >&2; exit 1; }
go mod verify
go vet ./...
go test ./...

rm -rf /tmp/userguard-check-amd64 /tmp/userguard-check-arm64
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1 go build -trimpath -buildvcs=false -o /tmp/userguard-check-amd64 ./cmd/userguard
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -buildvcs=false -o /tmp/userguard-check-arm64 ./cmd/userguard

sh -n scripts/install-deps-debian.sh scripts/build.sh scripts/genkey.sh scripts/status-userguard.sh scripts/stop-userguard.sh tests/check-repo.sh
bash -n scripts/run-userguard.sh tests/selftest.sh

if find . -type f | grep -Ei '/(\.env($|\.)|.*\.key$|.*\.psk$|.*\.log$|.*\.pid$)'; then
  echo 'unexpected secret/runtime file in repository' >&2
  exit 2
fi

echo USERGUARD_REPO_CHECK_OK
