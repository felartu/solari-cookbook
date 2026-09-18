#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export VDI_ENV_FILE=${VDI_ENV_FILE:-$ROOT/.env}
set -a
[[ -f "$VDI_ENV_FILE" ]] && . "$VDI_ENV_FILE"
set +a
python3 - <<'PY'
import json
from security_vault import VAULT
print(json.dumps(VAULT.safe_status(), indent=2))
PY
