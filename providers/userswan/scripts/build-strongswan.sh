#!/bin/sh
set -eu
VERSION=${STRONGSWAN_VERSION:-6.0.7}
SHA256=${STRONGSWAN_SHA256:-e518e34e159514f4c6ba80d1f926cb151e0dd4e3a1d94213171234b8b9ae6f55}
PREFIX=${PREFIX:-/opt/userswan/strongswan}
WORKDIR=${WORKDIR:-/tmp/userswan-strongswan-build}
PIDDIR=${PIDDIR:-/run/userswan}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PATCH="$HERE/patches/strongswan-${VERSION}/0001-kernel-libipsec-no-tun-packet-socket.patch"

[ -f "$PATCH" ] || { echo "missing patch: $PATCH" >&2; exit 2; }
command -v curl >/dev/null
command -v sha256sum >/dev/null
command -v patch >/dev/null

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" "$PIDDIR"
cd "$WORKDIR"
curl -fLso strongswan.tar.bz2 "https://download.strongswan.org/strongswan-${VERSION}.tar.bz2"
printf '%s  %s\n' "$SHA256" strongswan.tar.bz2 | sha256sum -c -
mkdir src
tar -xjf strongswan.tar.bz2 -C src --strip-components=1
cd src
patch -p2 < "$PATCH"
./configure \
    --prefix="$PREFIX" \
    --with-piddir="$PIDDIR" \
    --enable-kernel-libipsec \
    --enable-libipsec
make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
make check
make install
printf 'STRONGSWAN_BUILD_OK version=%s prefix=%s\n' "$VERSION" "$PREFIX"
