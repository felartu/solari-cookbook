#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$HERE"
make check
if find . -type f | grep -Ei '/(\.env|.*\.psk|.*secret.*|.*\.pcap|.*\.log)$'; then
    echo 'unexpected secret/runtime file in repository' >&2
    exit 1
fi
echo REPO_CHECK_OK
