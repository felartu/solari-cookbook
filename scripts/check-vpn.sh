#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export VDI_ENV_FILE=${VDI_ENV_FILE:-$ROOT/.env}
python3 - <<'PY'
import asyncio, json
from vpn_mcp import vdi_vpn_status
print(json.dumps(asyncio.run(vdi_vpn_status()), indent=2))
PY
