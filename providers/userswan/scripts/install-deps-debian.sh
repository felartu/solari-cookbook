#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential pkg-config libssl-dev libgmp-dev libcap-dev liblwip-dev \
    ca-certificates curl bzip2 patch python3 tcpdump netcat-openbsd
