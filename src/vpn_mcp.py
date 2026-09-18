#!/usr/bin/env python3
"""toolkit host-side MCP tools for the current userswan/IPsec + Remmina VDI path.

The MCP server deliberately keeps VPN credentials and RDP credentials host-side.
The model receives only sanitized status and action results.  The same async
tool functions are imported by the current desktop agent and are registered
with FastMCP so they can also be served over MCP stdio.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP
from solari_desktop import DesktopClient


ENV_FILE = Path(
    os.getenv(
        "VDI_ENV_FILE",
        str(Path(__file__).resolve().parent.parent / ".env"),
    )
).expanduser()
mcp = FastMCP(
    "solari-vdi-vpn",
    instructions=(
        "Controls the selected host-owned enterprise VPN provider (userswan or userguard) "
        "and the Remmina RDP session. VPN and RDP credentials are never returned to the model."
    ),
)

_CLIENT: DesktopClient | None = None
_DESKTOP: Any | None = None
_LOCK = asyncio.Lock()


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(ENV_FILE)

# current uses the current userswan checkout.  Migrate legacy snapshot defaults
# unless the operator explicitly supplied another non-legacy path.
if not os.getenv("VDI_STRONGSWAN_PREFIX") or os.getenv("VDI_STRONGSWAN_PREFIX") == "/opt/strongswan-notun":
    os.environ["VDI_STRONGSWAN_PREFIX"] = "/opt/userswan/strongswan"
if not os.getenv("VDI_STRONGSWAN_RUNTIME_DIR") or os.getenv("VDI_STRONGSWAN_RUNTIME_DIR") == "/run/strongswan-notun":
    os.environ["VDI_STRONGSWAN_RUNTIME_DIR"] = "/run/userswan"
if not os.getenv("VDI_FORWARDER") or os.getenv("VDI_FORWARDER", "").endswith("/notun-lwip-forwarder"):
    os.environ["VDI_FORWARDER"] = "/opt/userswan/bin/userswan-forwarder"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _runtime() -> str:
    return _env("VDI_STRONGSWAN_RUNTIME_DIR", "/run/userswan")


def _prefix() -> str:
    return _env("VDI_STRONGSWAN_PREFIX", "/opt/userswan/strongswan")


def _connection() -> str:
    return _env("VDI_IPSEC_CONNECTION", "vdi-rdp-client")


def _child() -> str:
    return _env("VDI_IPSEC_CHILD", "rdp-only")


def _local_host() -> str:
    return _env("VDI_RDP_LOCAL_HOST", "127.0.0.1")


def _local_port() -> int:
    return int(_env("VDI_RDP_LOCAL_PORT", "3390"))


def _remote_host() -> str:
    value = _env("VDI_REMOTE_TARGET_IP") or _env("VDI_RDP_HOST")
    if not value:
        raise RuntimeError("VDI_REMOTE_TARGET_IP or VDI_RDP_HOST is required")
    return value


def _remote_port() -> int:
    value = _env("VDI_REMOTE_TARGET_PORT") or _env("VDI_RDP_PORT")
    if not value:
        raise RuntimeError("VDI_REMOTE_TARGET_PORT or VDI_RDP_PORT is required")
    return int(value)


def _gui_user() -> str:
    value = _env("VDI_GUI_USER", "desktop")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", value):
        raise RuntimeError("VDI_GUI_USER contains invalid characters")
    return value


def _gui_home() -> str:
    return _env("VDI_GUI_HOME", f"/home/{_gui_user()}")


def _gui_display() -> str:
    value = _env("VDI_DISPLAY", ":0")
    if not re.fullmatch(r":[0-9]+", value):
        raise RuntimeError("VDI_DISPLAY must look like :0")
    return value


def _desktop_id() -> str:
    value = _env("VDI_DESKTOP_ID") or _env("SOLARI_DESKTOP_ID")
    if not value:
        raise RuntimeError("VDI_DESKTOP_ID/SOLARI_DESKTOP_ID is not configured")
    return value


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


async def _desktop() -> Any:
    global _CLIENT, _DESKTOP
    async with _LOCK:
        if _DESKTOP is not None:
            return _DESKTOP
        api_key = _env("SOLARI_API_KEY")
        if not api_key:
            raise RuntimeError("SOLARI_API_KEY is not configured")
        _CLIENT = DesktopClient(
            api_key=api_key,
            base_url=_env("SOLARI_BASE_URL", "https://api.getsolari.com"),
        )
        _DESKTOP = await _CLIENT.connect(_desktop_id())
        await _DESKTOP.connect()
        return _DESKTOP


async def _exec(command: str, args: list[str], timeout_ms: int = 60000) -> tuple[int, str, str]:
    desktop = await _desktop()
    result = await desktop.exec(command, args=args, timeout_ms=timeout_ms)
    code = int(_field(result, "exit_code", "exitCode", default=0))
    stdout = str(_field(result, "stdout", default="") or "")
    stderr = str(_field(result, "stderr", default="") or "")
    return code, stdout, stderr


async def _shell(script: str, timeout_ms: int = 60000, check: bool = True) -> str:
    code, stdout, stderr = await _exec("/bin/sh", ["-c", script], timeout_ms)
    if check and code != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(f"desktop command failed ({code}): {detail[-1800:]}")
    return stdout


async def _upload(remote: str, payload: bytes) -> None:
    desktop = await _desktop()
    info = await desktop.upload_url(remote)
    url = info.get("url") or info.get("uploadUrl") or info.get("upload_url")
    if not url:
        raise RuntimeError("desktop upload URL was not returned")
    method = str(info.get("method") or "PUT").upper()
    headers = dict(info.get("headers") or {})
    fields = info.get("fields") or info.get("formFields")
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        if isinstance(fields, dict):
            response = await client.post(
                str(url),
                data=dict(fields),
                files={"file": ("payload", payload)},
            )
        else:
            response = await client.request(
                method, str(url), headers=headers, content=payload
            )
        response.raise_for_status()


def _swanctl() -> str:
    return f"{shlex.quote(_prefix())}/sbin/swanctl"


def _vici() -> str:
    return f"unix://{shlex.quote(_runtime())}/charon.vici"


def _status_command() -> str:
    r = shlex.quote(_runtime())
    p = shlex.quote(_prefix())
    lp = _local_port()
    return f"""
R={r}
P={p}
sa="$($P/sbin/swanctl --uri unix://$R/charon.vici --list-sas 2>/dev/null || true)"
charon=0
forwarder=0
listener=0
remmina=0
if [ -f "$R/charon.pid" ] && kill -0 "$(cat "$R/charon.pid" 2>/dev/null)" 2>/dev/null; then charon=1; fi
if [ -f "$R/forwarder.pid" ] && kill -0 "$(cat "$R/forwarder.pid" 2>/dev/null)" 2>/dev/null; then forwarder=1; fi
if ss -lntH 2>/dev/null | awk '$4 ~ /127\\.0\\.0\\.1:{lp}$/ {{found=1}} END {{exit(found ? 0 : 1)}}'; then listener=1; fi
if pgrep -u {_gui_user()} -x remmina >/dev/null 2>&1; then remmina=1; fi
printf 'charon=%s\\nforwarder=%s\\nlistener=%s\\nremmina=%s\\nsa=%s\\n' \
  "$charon" "$forwarder" "$listener" "$remmina" \
  "$(printf '%s' "$sa" | grep -Eq 'ESTABLISHED.*|INSTALLED' && echo 1 || echo 0)"
printf '%s\\n' "$sa"
""".replace("{lp}", str(lp))


async def _raw_status() -> tuple[dict[str, Any], str]:
    text = await _shell(_status_command(), timeout_ms=30000)
    values: dict[str, str] = {}
    sa_lines: list[str] = []
    in_sa = False
    for line in text.splitlines():
        if re.match(r"^(charon|forwarder|listener|remmina|sa)=", line):
            key, value = line.split("=", 1)
            values[key] = value.strip()
            continue
        if line.strip():
            in_sa = True
        if in_sa:
            sa_lines.append(line)
    sa = "\n".join(sa_lines)
    status = {
        "provider": "userswan",
        "vpn_type": "ipsec-psk",
        "gateway": _env("VDI_IPSEC_GATEWAY", "<unset>"),
        "remote_target": f"{_remote_host()}:{_remote_port()}",
        "local_endpoint": f"{_local_host()}:{_local_port()}",
        "charon_running": values.get("charon") == "1",
        "forwarder_running": values.get("forwarder") == "1",
        "listener_ready": values.get("listener") == "1",
        "remmina_running": values.get("remmina") == "1",
        "sa_established": values.get("sa") == "1",
        "ready": all(
            values.get(key) == "1"
            for key in ("charon", "forwarder", "listener", "sa")
        ),
    }
    if sa:
        status["sa_summary"] = re.sub(r"\s+", " ", sa)[:1200]
    return status, sa


async def _userswan_status() -> dict[str, Any]:
    """Return sanitized userswan IPsec and relay status."""
    status, _ = await _raw_status()
    return status


def _render_swanctl() -> str:
    psk = _env("VDI_IPSEC_PSK")
    if not psk:
        psk_file = _env("VDI_IPSEC_PSK_FILE")
        if psk_file:
            try:
                psk = Path(psk_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(f"could not read VDI_IPSEC_PSK_FILE: {type(exc).__name__}") from exc
    if not psk:
        raise RuntimeError("VDI_IPSEC_PSK or VDI_IPSEC_PSK_FILE is required if the guest config is absent")
    gateway = _env("VDI_IPSEC_GATEWAY")
    if not gateway:
        raise RuntimeError("VDI_IPSEC_GATEWAY is required")
    local_id = _env("VDI_IPSEC_LOCAL_ID", "vdi-client-01")
    remote_id = _env("VDI_IPSEC_REMOTE_ID", gateway)
    remote_target = _remote_host()
    remote_ts = _env(
        "VDI_IPSEC_REMOTE_TS",
        remote_target if "/" in remote_target else f"{remote_target}/32",
    )
    ike = _env(
        "VDI_IPSEC_IKE_PROPOSALS",
        "aes256gcm16-prfsha384-ecp384,aes256gcm16-prfsha256-ecp256,aes256-sha256-modp2048",
    )
    esp = _env(
        "VDI_IPSEC_ESP_PROPOSALS",
        "aes256gcm16-ecp384,aes256gcm16-ecp256,aes256-sha256-modp2048",
    )
    return f"""connections {{
  {_connection()} {{
    version = 2
    local_addrs = %any
    remote_addrs = {gateway}
    vips = 0.0.0.0
    fragmentation = yes
    mobike = yes
    encap = yes
    dpd_delay = 30s
    reauth_time = 0s
    unique = replace
    childless = allow
    proposals = {ike}
    local {{
      auth = psk
      id = {local_id}
    }}
    remote {{
      auth = psk
      id = {remote_id}
    }}
    children {{
      {_child()} {{
        local_ts = dynamic
        remote_ts = {remote_ts}
        esp_proposals = {esp}
        start_action = none
        close_action = none
        dpd_action = restart
        rekey_time = 1h
        life_time = 70m
        rand_time = 5m
      }}
    }}
  }}
}}
secrets {{
  ike-vdi-agent {{
    id-gateway = {remote_id}
    id-client = {local_id}
    secret = {psk}
  }}
}}
"""


async def _ensure_config() -> None:
    r = shlex.quote(_runtime())
    await _shell(f"mkdir -p {r}/swanctl && test -s {r}/swanctl/swanctl.conf", check=False)
    code, _, _ = await _exec(
        "/bin/sh",
        ["-c", f"test -s {r}/swanctl/swanctl.conf"],
        timeout_ms=10000,
    )
    # Re-render on every connect by default so a reused desktop never keeps
    # stale target, pool, identity, or PSK material. Operators may explicitly
    # preserve a hand-edited runtime config for diagnostics.
    if code == 0 and _env("VDI_PRESERVE_RUNTIME_CONFIG", "0").lower() in {"1", "true", "yes"}:
        return
    content = _render_swanctl().encode("utf-8")
    remote = f"{_runtime()}/swanctl/swanctl.conf"
    await _upload(remote, content)
    await _shell(f"chmod 600 {shlex.quote(remote)}")


async def _ensure_charon() -> None:
    r = shlex.quote(_runtime())
    p = shlex.quote(_prefix())
    # The reusable snapshot intentionally has no runtime files or secrets.
    # Recreate the userspace no-TUN charon configuration on each fresh desktop.
    config = f"""charon {{
    load_modular = yes
    port = 15000
    port_nat_t = 14500
    routing_table = 0
    install_routes = no
    install_virtual_ip = no
    plugins {{
        include {_prefix()}/etc/strongswan.d/charon/*.conf
        kernel-libipsec {{
            load = 2
            no_tun = yes
            packet_socket = {_runtime()}/notun.sock
        }}
        kernel-netlink {{
            load = 1
        }}
        vici {{
            socket = unix://{_runtime()}/charon.vici
        }}
    }}
}}
swanctl {{
    socket = unix://{_runtime()}/charon.vici
}}
"""
    config_b64 = base64.b64encode(config.encode("utf-8")).decode("ascii")
    await _shell(
        f"mkdir -p {r}; "
        f"printf %s {shlex.quote(config_b64)} | base64 -d >{r}/strongswan.conf; "
        f"chmod 600 {r}/strongswan.conf",
        timeout_ms=20000,
    )
    code, _, _ = await _exec(
        "/bin/sh",
        ["-c", f"[ -f {r}/charon.pid ] && kill -0 $(cat {r}/charon.pid) 2>/dev/null"],
        timeout_ms=10000,
    )
    if code == 0:
        return
    await _shell(
        f"nohup env LD_LIBRARY_PATH={p}/lib:{p}/lib/ipsec STRONGSWAN_CONF={r}/strongswan.conf "
        f"{p}/libexec/ipsec/charon >{r}/charon.log 2>&1 & "
        f"echo $! >{r}/charon.pid",
        timeout_ms=20000,
    )
    for _ in range(100):
        code, _, _ = await _exec(
            "/bin/sh",
            ["-c", f"test -S {r}/charon.vici && test -S {r}/notun.sock"],
            timeout_ms=10000,
        )
        if code == 0:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("charon VICI/packet socket did not become ready")


def _extract_vip(sa: str) -> str:
    candidates = re.findall(r"\[([0-9]{1,3}(?:\.[0-9]{1,3}){3})\]", sa)
    if candidates:
        return candidates[-1]
    candidates = re.findall(
        r"(?m)^\s+local\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3})/32\s*$", sa
    )
    if candidates:
        return candidates[-1]
    raise RuntimeError("the IPsec SA did not expose a negotiated IPv4 VIP")


async def _ensure_forwarder(sa: str) -> None:
    r = shlex.quote(_runtime())
    fwd = shlex.quote(_env("VDI_FORWARDER", f"{_prefix().rsplit('/strongswan', 1)[0]}/bin/userswan-forwarder"))
    code, _, _ = await _exec(
        "/bin/sh",
        ["-c", f"[ -f {r}/forwarder.pid ] && kill -0 $(cat {r}/forwarder.pid) 2>/dev/null"],
        timeout_ms=10000,
    )
    if code == 0:
        return
    vip = _extract_vip(sa)
    await _shell(
        f"rm -f {r}/forwarder.sock; "
        f"nohup {fwd} --vip {shlex.quote(vip)} "
        f"--packet-socket {r}/notun.sock --bind-socket {r}/forwarder.sock "
        f"--listen-ip {shlex.quote(_local_host())} --listen-port {int(_local_port())} "
        f"--remote-ip {shlex.quote(_remote_host())} --remote-port {int(_remote_port())} "
        f"--max-sessions 32 >{r}/forwarder.log 2>&1 & "
        f"echo $! >{r}/forwarder.pid",
        timeout_ms=20000,
    )
    for _ in range(100):
        code, _, _ = await _exec(
            "/bin/sh",
            ["-c", f"grep -q '^LISTENER_READY ' {r}/forwarder.log"],
            timeout_ms=10000,
        )
        if code == 0:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("userswan forwarder did not become ready")


async def _userswan_connect() -> dict[str, Any]:
    """Bring up the host-owned IPsec SA and localhost userswan listener."""
    status, sa = await _raw_status()
    if status["ready"]:
        await _persist_remmina_profile()
        status["profile_persisted"] = True
        return status
    await _ensure_config()
    await _ensure_charon()
    r = shlex.quote(_runtime())
    p = shlex.quote(_prefix())
    prefix = f"LD_LIBRARY_PATH={p}/lib:{p}/lib/ipsec SWANCTL_DIR={r}/swanctl "
    sw = shlex.quote(_swanctl())
    uri = shlex.quote(_vici())
    await _shell(f"{prefix}{sw} --uri {uri} --load-all", timeout_ms=60000)
    status, sa = await _raw_status()
    if not status["sa_established"]:
        child = shlex.quote(_child())
        await _shell(
            f"{prefix}{sw} --uri {uri} --initiate --child {child} --timeout 40",
            timeout_ms=90000,
        )
    status, sa = await _raw_status()
    if not status["sa_established"]:
        raise RuntimeError("IPsec did not reach ESTABLISHED/INSTALLED")
    await _ensure_forwarder(sa)
    for _ in range(60):
        status, _ = await _raw_status()
        if status["ready"]:
            await _persist_remmina_profile()
            status["profile_persisted"] = True
            return status
        await asyncio.sleep(0.5)
    raise RuntimeError("VPN connected but userswan listener did not become ready")



def _provider_name() -> str:
    value = _env("VPN_PROVIDER", "userswan").strip().lower()
    if value not in {"userswan", "userguard"}:
        raise RuntimeError("VPN_PROVIDER must be 'userswan' or 'userguard'")
    return value


def _userguard_runtime() -> str:
    return _env("USERGUARD_RUNTIME_DIR", "/run/userguard")


def _userguard_binary() -> str:
    return _env("USERGUARD_BINARY", "/opt/userguard/bin/userguard")


def _userguard_value(name: str, fallback: str = "") -> str:
    return _env(name, fallback)


async def _userguard_status() -> dict[str, Any]:
    runtime = shlex.quote(_userguard_runtime())
    local_host = _local_host()
    local_port = _local_port()
    script = f"""
set +e
running=0
listener=0
ready=0
pid=''
if [ -f {runtime}/userguard.pid ]; then
  pid=$(cat {runtime}/userguard.pid 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then running=1; fi
fi
if ss -lntH 2>/dev/null | awk '$4 ~ /{re.escape(local_host)}:{local_port}$/ {{found=1}} END {{exit(found ? 0 : 1)}}'; then listener=1; fi
if grep -q 'USERGUARD_READY' {runtime}/userguard.log 2>/dev/null; then ready=1; fi
printf 'running=%s\nlistener=%s\nready=%s\n' "$running" "$listener" "$ready"
"""
    text = await _shell(script, timeout_ms=30000, check=False)
    vals: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    status = {
        "provider": "userguard",
        "vpn_type": "wireguard-userspace",
        "remote_target": f"{_remote_host()}:{_remote_port()}",
        "local_endpoint": f"{local_host}:{local_port}",
        "runtime_running": vals.get("running") == "1",
        "listener_ready": vals.get("listener") == "1",
        "wireguard_ready": vals.get("ready") == "1",
    }
    status["ready"] = bool(status["runtime_running"] and status["listener_ready"] and status["wireguard_ready"])
    return status


async def _upload_secret_file(host_path_value: str, guest_path: str) -> str:
    host_path = Path(host_path_value).expanduser()
    if not host_path.is_file():
        raise RuntimeError(f"secret file does not exist: {host_path}")
    payload = host_path.read_bytes()
    await _shell(f"mkdir -p {shlex.quote(str(Path(guest_path).parent))}", timeout_ms=10000)
    await _upload(guest_path, payload)
    await _shell(f"chmod 600 {shlex.quote(guest_path)}", timeout_ms=10000)
    return guest_path


async def _userguard_connect() -> dict[str, Any]:
    status = await _userguard_status()
    if status.get("ready"):
        await _persist_remmina_profile()
        status["profile_persisted"] = True
        return status

    address = _userguard_value("USERGUARD_ADDRESS", _env("WG_ADDRESS"))
    peer_public = _userguard_value("USERGUARD_PEER_PUBLIC_KEY", _env("WG_PEER_PUBLIC_KEY"))
    endpoint = _userguard_value("USERGUARD_ENDPOINT", _env("WG_ENDPOINT"))
    allowed_ips = _userguard_value("USERGUARD_ALLOWED_IPS", _env("WG_ALLOWED_IPS"))
    private_key_file = _userguard_value("USERGUARD_PRIVATE_KEY_FILE", _env("WG_PRIVATE_KEY_FILE"))
    if not all([address, peer_public, endpoint, allowed_ips, private_key_file]):
        raise RuntimeError(
            "userguard requires USERGUARD_ADDRESS, USERGUARD_PRIVATE_KEY_FILE, "
            "USERGUARD_PEER_PUBLIC_KEY, USERGUARD_ENDPOINT and USERGUARD_ALLOWED_IPS"
        )

    runtime = _userguard_runtime()
    guest_private = f"{runtime}/private.key"
    await _upload_secret_file(private_key_file, guest_private)
    guest_psk = ""
    psk_file = _userguard_value("USERGUARD_PRESHARED_KEY_FILE", _env("WG_PRESHARED_KEY_FILE"))
    if psk_file:
        guest_psk = await _upload_secret_file(psk_file, f"{runtime}/preshared.key")

    args: list[str] = [
        "--private-key-file", guest_private,
        "--peer-public-key", peer_public,
        "--peer-endpoint", endpoint,
        "--listen-port", _userguard_value("USERGUARD_LISTEN_PORT", _env("WG_LISTEN_PORT", "0")),
        "--persistent-keepalive", _userguard_value("USERGUARD_PERSISTENT_KEEPALIVE", _env("WG_PERSISTENT_KEEPALIVE", "25")),
        "--mtu", _userguard_value("USERGUARD_MTU", _env("WG_MTU", "1420")),
        "--max-sessions", _userguard_value("USERGUARD_MAX_SESSIONS", "64"),
    ]
    for value in [x.strip() for x in address.split(",") if x.strip()]:
        args += ["--address", value]
    for value in [x.strip() for x in allowed_ips.split(",") if x.strip()]:
        args += ["--allowed-ip", value]
    if guest_psk:
        args += ["--peer-preshared-key-file", guest_psk]
    args += ["--forward", f"{_local_host()}:{_local_port()}={_remote_host()}:{_remote_port()}"]

    runtime_q = shlex.quote(runtime)
    binary_q = shlex.quote(_userguard_binary())
    argv = " ".join(shlex.quote(a) for a in args)
    await _shell(
        f"mkdir -p {runtime_q}; chmod 700 {runtime_q}; "
        f"if [ -f {runtime_q}/userguard.pid ]; then p=$(cat {runtime_q}/userguard.pid); kill -TERM \"$p\" 2>/dev/null || true; fi; "
        f"rm -f {runtime_q}/userguard.log {runtime_q}/userguard.pid; "
        f"nohup {binary_q} {argv} >{runtime_q}/userguard.log 2>&1 & echo $! >{runtime_q}/userguard.pid",
        timeout_ms=20000,
    )
    for _ in range(100):
        status = await _userguard_status()
        if status.get("ready"):
            await _persist_remmina_profile()
            status["profile_persisted"] = True
            return status
        await asyncio.sleep(0.1)
    raise RuntimeError("userguard did not become ready; inspect /run/userguard/userguard.log")


async def _userguard_disconnect() -> dict[str, Any]:
    runtime = shlex.quote(_userguard_runtime())
    await _shell(
        f"if [ -f {runtime}/userguard.pid ]; then p=$(cat {runtime}/userguard.pid); "
        f"kill -TERM \"$p\" 2>/dev/null || true; sleep 1; kill -KILL \"$p\" 2>/dev/null || true; fi; "
        f"rm -f {runtime}/userguard.pid",
        timeout_ms=15000,
        check=False,
    )
    status = await _userguard_status()
    status["disconnected"] = not bool(status.get("runtime_running"))
    return status


@mcp.tool
async def vdi_vpn_status() -> dict[str, Any]:
    """Return sanitized status for the selected enterprise VPN provider."""
    provider = _provider_name()
    if provider == "userguard":
        return await _userguard_status()
    status = await _userswan_status()
    status["provider"] = "userswan"
    return status


@mcp.tool
async def vdi_vpn_connect() -> dict[str, Any]:
    """Start the selected userspace enterprise VPN provider and RDP relay."""
    if _provider_name() == "userguard":
        return await _userguard_connect()
    status = await _userswan_connect()
    status["provider"] = "userswan"
    return status


@mcp.tool
async def vdi_vpn_disconnect() -> dict[str, Any]:
    """Disconnect the selected userspace enterprise VPN provider."""
    if _provider_name() == "userguard":
        return await _userguard_disconnect()
    status = await _userswan_disconnect()
    status["provider"] = "userswan"
    return status


def _persistent_profile_path() -> str:
    path = _env(
        "VDI_RDP_PROFILE_PATH",
        f"{_gui_home()}/.local/share/remmina/windows-server-enterprise-vpn.remmina",
    )
    if not path.startswith("/"):
        raise RuntimeError("VDI_RDP_PROFILE_PATH must be absolute")
    return path


async def _persist_remmina_profile() -> str:
    """Write the GUI-clickable Remmina profile with host-provisioned credentials."""
    persistent = _persistent_profile_path()
    qpath = shlex.quote(persistent)
    qdir = shlex.quote(str(Path(persistent).parent))
    # The upload API writes a file but does not create arbitrary parent
    # directories, so create the Remmina profile directory first.
    await _shell(
        f"mkdir -p {qdir}; chown {_gui_user()}:{_gui_user()} {qdir}; chmod 700 {qdir}",
        timeout_ms=20000,
    )
    await _upload(persistent, _remmina_profile(include_credentials=True).encode("utf-8"))
    await _shell(
        f"chown {_gui_user()}:{_gui_user()} {qpath}; chmod 600 {qpath}",
        timeout_ms=20000,
    )
    return persistent


@mcp.tool
async def vdi_remmina_prepare_profile() -> dict[str, Any]:
    """Persist the credentialed Remmina profile without launching Remmina."""
    path = await _persist_remmina_profile()
    return {
        "profile_persisted": True,
        "profile_path": path,
        "profile_server": f"{_local_host()}:{_local_port()}",
        "credentials_present": True,
    }


async def _userswan_disconnect() -> dict[str, Any]:
    """Stop the userswan localhost relay and terminate the IKE_SA."""
    r = shlex.quote(_runtime())
    pidfile = f"{r}/forwarder.pid"
    await _shell(
        f"if [ -f {pidfile} ]; then "
        f"pid=$(cat {pidfile}); kill -TERM \"$pid\" 2>/dev/null || true; "
        f"sleep 1; kill -KILL \"$pid\" 2>/dev/null || true; rm -f {pidfile}; fi; "
        f"rm -f {r}/forwarder.sock",
        timeout_ms=20000,
        check=False,
    )
    sw = shlex.quote(_swanctl())
    uri = shlex.quote(_vici())
    p = shlex.quote(_prefix())
    prefix = f"LD_LIBRARY_PATH={p}/lib:{p}/lib/ipsec SWANCTL_DIR={r}/swanctl "
    await _shell(
        f"{prefix}{sw} --uri {uri} --terminate --ike {shlex.quote(_connection())} --timeout 5",
        timeout_ms=20000,
        check=False,
    )
    status, _ = await _raw_status()
    status["disconnected"] = not status["sa_established"] and not status["listener_ready"]
    return status


def _remmina_profile(*, include_credentials: bool = True) -> str:
    user = _env("VDI_RDP_USERNAME", "Administrator")
    password = os.getenv("VDI_RDP_PASSWORD", "")
    if not password:
        raise RuntimeError("VDI_RDP_PASSWORD is not configured")
    domain = _env("VDI_RDP_DOMAIN")
    security = _env("VDI_RDP_SECURITY", "nla")
    name = _env("VDI_RDP_NAME", "Windows Server via Enterprise VPN")
    server = f"{_local_host()}:{_local_port()}"
    values = {
        "name": name,
        "protocol": "RDP",
        "server": server,
        "username": user,
        # The runtime-saved profile is intentionally credentialed for GUI use.
        # The model never receives this value; it only clicks the saved profile.
        "password": password,
        "domain": domain,
        "security": security,
        # Use a stable client-resolution layout.  scale=2 enables Remmina
        # dynamic display-control updates, which is unreliable in this build.
        "resolution_mode": _env("VDI_RDP_RESOLUTION_MODE", "1"),
        "scale": _env("VDI_RDP_SCALE", "1"),
        # Fullscreen makes the RDP canvas use the whole Solari desktop.
        "viewmode": _env("VDI_RDP_VIEWMODE", "2"),
        "colordepth": "32",
        "quality": "9",
        "network": "none",
        "disablepasswordstoring": "0",
        "cert_ignore": "1" if _env("VDI_RDP_IGNORE_CERT", "true").lower() in {"1", "true", "yes"} else "0",
        "shareprinter": "0",
        "sharesmartcard": "0",
        "microphone": "0",
        "multimon": "0",
    }
    for key, value in values.items():
        if "\n" in str(value) or "\r" in str(value):
            raise RuntimeError(f"newline in Remmina field {key}")
    return "[remmina]\n" + "".join(f"{key}={value}\n" for key, value in values.items())


@mcp.tool
async def vdi_remmina_connect() -> dict[str, Any]:
    """Open Remmina against the env-provisioned localhost RDP profile."""
    status = await vdi_vpn_connect()
    profile = f"/tmp/solari-vdi-remmina-{uuid.uuid4().hex}.remmina"
    persistent = _persistent_profile_path()
    # Save a credentialed profile so GUI navigation through Remmina's
    # Applications > Internet > Remmina path can reuse it without prompting.
    await _upload(
        persistent,
        _remmina_profile(include_credentials=True).encode("utf-8"),
    )
    await _upload(profile, _remmina_profile(include_credentials=True).encode("utf-8"))
    qprofile = shlex.quote(profile)
    qpersistent = shlex.quote(persistent)
    qtitle = shlex.quote(_env("VDI_RDP_NAME", "Windows Server via Enterprise VPN"))
    script = f"""
set -eu
mkdir -p "$(dirname {qpersistent})"
chown {_gui_user()}:{_gui_user()} {qpersistent}
chmod 600 {qpersistent}
chown {_gui_user()}:{_gui_user()} {qprofile}
chmod 600 {qprofile}
pkill -u {_gui_user()} -x remmina 2>/dev/null || true
sleep 0.5
mkdir -p /run/solari-vdi
chown {_gui_user()}:{_gui_user()} /run/solari-vdi
runuser -u {_gui_user()} -- env HOME={shlex.quote(_gui_home())} USER={_gui_user()} LOGNAME={_gui_user()} DISPLAY={_gui_display()} \
  XDG_RUNTIME_DIR=/run/desktop DBUS_SESSION_BUS_ADDRESS=unix:path=/run/desktop/bus \
  nohup remmina -c {qprofile} >/run/solari-vdi/remmina.log 2>&1 &
echo $! >/run/solari-vdi/remmina.pid
sleep 2
if command -v wmctrl >/dev/null 2>&1; then
  wmctrl -r {qtitle} -b add,fullscreen 2>/dev/null \
    || wmctrl -r {qtitle} -b add,maximized_vert,maximized_horz 2>/dev/null \
    || true
fi
"""
    await _shell(script, timeout_ms=30000)
    await asyncio.sleep(8)
    check = await _shell(
        """set +e
running=0
connections=0
windows=0
if pgrep -u desktop -x remmina >/dev/null 2>&1; then running=1; fi
if ss -ntH 2>/dev/null | awk '$4 ~ /127\\.0\\.0\\.1:[0-9]+/ && $5 ~ /127\\.0\\.0\\.1:3390/ {found=1} END {exit(found ? 0 : 1)}'; then connections=1; fi
if command -v wmctrl >/dev/null 2>&1 && wmctrl -l 2>/dev/null | grep -qi remmina; then windows=1; fi
printf 'running=%s\\nconnections=%s\\nwindows=%s\\n' "$running" "$connections" "$windows"
""",
        timeout_ms=30000,
    )
    await _shell(f"rm -f {qprofile}", timeout_ms=10000, check=False)
    values: dict[str, str] = {}
    for line in check.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v
    status["remmina_running"] = values.get("running") == "1"
    status["remmina_local_connection"] = values.get("connections") == "1"
    status["remmina_window"] = values.get("windows") == "1"
    status["rdp_gui_started"] = status["remmina_running"] and (
        status["remmina_local_connection"] or status["remmina_window"]
    )
    status["profile_server"] = f"{_local_host()}:{_local_port()}"
    status["profile_path"] = persistent
    status["profile_persisted"] = True
    return status


@mcp.tool
async def vdi_desktop_state() -> dict[str, Any]:
    """Return sanitized GUI/process state for the current agent."""
    status, _ = await _raw_status()
    text = await _shell(
        """set +e
wmctrl -l 2>/dev/null | tail -30 || true
ps -eo user,pid,args | grep -E 'remmina|userswan-forwarder|userguard|charon' | grep -v grep || true
""",
        timeout_ms=30000,
    )
    status["desktop_windows_and_processes"] = text[-5000:]
    return status


TOOL_FUNCS = {
    "vdi_vpn_status": vdi_vpn_status,
    "vdi_vpn_connect": vdi_vpn_connect,
    "vdi_vpn_disconnect": vdi_vpn_disconnect,
    "vdi_remmina_prepare_profile": vdi_remmina_prepare_profile,
    "vdi_remmina_connect": vdi_remmina_connect,
    "vdi_desktop_state": vdi_desktop_state,
}


if __name__ == "__main__":
    mcp.run()
