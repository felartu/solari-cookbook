#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

required=(
  README.md .env.example requirements.txt
  src/solari_vdi_toolkit.py src/agent_engine.py src/vpn_mcp.py src/security_mcp.py
  providers/userswan/README.md providers/userguard/README.md
  docs/ARCHITECTURE.md docs/VPN_PROVIDERS.md docs/SECRETS_AND_MFA.md docs/COMPUTER_USE.md
)
for f in "${required[@]}"; do
  [[ -f "$f" ]] || { echo "missing required file: $f" >&2; exit 2; }
done

python3 - <<'PY'
from pathlib import Path
for path in Path('src').glob('*.py'):
    compile(path.read_text(encoding='utf-8'), str(path), 'exec')
print('PYTHON_SOURCE_OK')
PY

for f in scripts/*.sh providers/userswan/scripts/*.sh providers/userguard/scripts/*.sh; do
  bash -n "$f"
done
echo SHELL_SYNTAX_OK

if grep -RIniE 'solari_desktop_agent_v[0-9]|config_v[0-9]|security_vault_v[0-9]|vdi_vpn_mcp_v[0-9]|demo_ui_v[0-9]' \
  README.md docs src config examples vaultwarden scripts/run-toolkit.sh scripts/run-vpn-mcp.sh scripts/run-security-mcp.sh scripts/check-vpn.sh scripts/check-vault.sh 2>/dev/null; then
  echo 'development naming leaked into public repository' >&2
  exit 3
fi
echo PUBLIC_NAMING_OK

if find . -type f \( -name '*.log' -o -name '*.state' -o -name '*.pyc' -o -name '*.png' -o -name '*.jpg' \) | grep -q .; then
  echo 'generated/runtime artifacts found in repository' >&2
  find . -type f \( -name '*.log' -o -name '*.state' -o -name '*.pyc' -o -name '*.png' -o -name '*.jpg' \)
  exit 4
fi
echo NO_RUNTIME_ARTIFACTS_OK

(
  cd providers/userswan
  bash tests/check-repo.sh
)
(
  cd providers/userguard
  bash tests/check-repo.sh
)

echo TOOLKIT_REPOSITORY_CHECK_OK
