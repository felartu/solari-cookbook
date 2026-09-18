#!/bin/sh
set -eu
PRIVATE=${1:-userguard.key}
PUBLIC=${2:-userguard.pub}
command -v wg >/dev/null 2>&1 || { echo 'wg command not found; install wireguard-tools' >&2; exit 2; }
umask 077
wg genkey > "$PRIVATE"
chmod 0600 "$PRIVATE"
wg pubkey < "$PRIVATE" > "$PUBLIC"
chmod 0644 "$PUBLIC"
printf 'USERGUARD_KEYPAIR_OK private=%s public=%s\n' "$PRIVATE" "$PUBLIC"
