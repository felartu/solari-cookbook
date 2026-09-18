#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import stat
from pathlib import Path

ap = argparse.ArgumentParser(description='Render a mode-0600 swanctl configuration for userswan.')
ap.add_argument('--psk-file', required=True)
ap.add_argument('--output', required=True)
ap.add_argument('--gateway', required=True)
ap.add_argument('--local-id', required=True)
ap.add_argument('--remote-id', required=True)
ap.add_argument('--remote-ts', required=True)
ap.add_argument('--connection', default='userswan')
ap.add_argument('--child', default='userswan-child')
ap.add_argument('--ike-proposals', default='aes256gcm16-prfsha384-ecp384,aes256gcm16-prfsha256-ecp256,aes256-sha256-modp2048')
ap.add_argument('--esp-proposals', default='aes256gcm16-ecp384,aes256gcm16-ecp256,aes256-sha256-modp2048')
ap.add_argument('--rekey-time', default='1h')
ap.add_argument('--life-time', default='70m')
ap.add_argument('--rand-time', default='5m')
ap.add_argument('--childless', choices=['allow','prefer','force','never'], default='allow')
a = ap.parse_args()

psk_path = Path(a.psk_file)
mode = stat.S_IMODE(psk_path.stat().st_mode)
if mode & 0o077:
    raise SystemExit('PSK file must not be group/world accessible; chmod 600 it first')
psk = psk_path.read_text(encoding='utf-8').strip()
if not (psk.startswith('0s') or psk.startswith('0x')):
    raise SystemExit('PSK must use strongSwan 0s... (base64) or 0x... (hex) encoding')

text = f'''connections {{
  {a.connection} {{
    version = 2
    local_addrs = %any
    remote_addrs = {a.gateway}
    vips = 0.0.0.0
    fragmentation = yes
    mobike = yes
    encap = yes
    dpd_delay = 30s
    reauth_time = 0s
    unique = replace
    childless = {a.childless}
    proposals = {a.ike_proposals}
    local {{
      auth = psk
      id = {a.local_id}
    }}
    remote {{
      auth = psk
      id = {a.remote_id}
    }}
    children {{
      {a.child} {{
        local_ts = dynamic
        remote_ts = {a.remote_ts}
        esp_proposals = {a.esp_proposals}
        start_action = none
        close_action = none
        dpd_action = restart
        rekey_time = {a.rekey_time}
        life_time = {a.life_time}
        rand_time = {a.rand_time}
      }}
    }}
  }}
}}
secrets {{
  ike-userswan {{
    id-gateway = {a.remote_id}
    id-client = {a.local_id}
    secret = {psk}
  }}
}}
'''
out = Path(a.output)
out.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write(text)
os.chmod(out, 0o600)
print(f'wrote {out} mode=0600')
