#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export VDI_ENV_FILE=${VDI_ENV_FILE:-$ROOT/.env}
export VDI_CONFIG_FILE=${VDI_CONFIG_FILE:-$ROOT/config/toolkit.conf}
if [[ ! -f "$VDI_CONFIG_FILE" ]]; then
  echo "Missing $VDI_CONFIG_FILE" >&2
  echo "Copy config/toolkit.conf.example to config/toolkit.conf first." >&2
  exit 2
fi
exec python3 "$ROOT/src/solari_vdi_toolkit.py" "$@"
