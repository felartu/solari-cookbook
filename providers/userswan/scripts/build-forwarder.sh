#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${PREFIX:-/opt/userswan}
CC=${CC:-cc}
CFLAGS=${CFLAGS:--O2 -g -Wall -Wextra -Werror}
command -v pkg-config >/dev/null
pkg-config --exists lwip || { echo 'lwIP development package not found (pkg-config lwip)' >&2; exit 2; }
mkdir -p "$PREFIX/bin"
# CFLAGS is intentionally word-split here to support normal compiler flag lists.
# shellcheck disable=SC2086
$CC $CFLAGS -pthread $(pkg-config --cflags lwip) \
    "$HERE/src/userswan-forwarder.c" \
    -o "$PREFIX/bin/userswan-forwarder" \
    $(pkg-config --libs lwip)
chmod 0755 "$PREFIX/bin/userswan-forwarder"
printf 'FORWARDER_BUILD_OK binary=%s\n' "$PREFIX/bin/userswan-forwarder"
