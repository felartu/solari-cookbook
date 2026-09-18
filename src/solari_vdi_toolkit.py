#!/usr/bin/env python3
"""Solari Enterprise VDI Toolkit.

Single entry point for enterprise VDI computer use:

* create or attach a Solari desktop;
* prepare a provider-backed loopback path to the protected RDP service;
* expose a real Remmina/Windows GUI to the vision-capable agent;
* provide durable goal management and verified desktop input; and
* type Vaultwarden/Bitwarden credentials and TOTP values host-side without exposing secret values to the AI provider.

`userswan` (IKEv2/IPsec) can be built into a reusable Solari snapshot from the
bundled source tree. `userguard` (WireGuard) is selected at runtime and expects
a Solari desktop snapshot/image that already contains the userguard binary.
VPN PSKs/private keys and RDP passwords are consumed only by host-owned code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import shlex
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import LOADED_CONFIG_FILE


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'""":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# toolkit is portable across host users/checkouts. By default the .env sits
# at the repository root by default; override VDI_ENV_FILE when a different
# secret/config location is required.
ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("VDI_ENV_FILE", str(ROOT.parent / ".env"))).expanduser()
# Keep the toolkit engine and the compatible current transport bridge writing lifecycle identifiers to the
# same host environment file that was loaded above.
os.environ.setdefault("VDI_ENV_FILE", str(ENV_FILE))
if os.getenv("VDI_DISABLE_DOTENV", "0").strip().lower() not in {"1", "true", "yes", "on"}:
    _load_dotenv(ENV_FILE)

# The engine provides the full hardened Enterprise VDI Toolkit interaction surface: persistent
# conversation, live RFB, OCR-first text clicks, visible grids/zoom, semantic
# double-clicks, goal budgets, and transient provider retries.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import agent_engine as agent  # noqa: E402
import vpn_mcp as vdi_bridge  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _api_timeout_seconds() -> float:
    return max(5.0, float(_env("VDI_SOLARI_API_TIMEOUT_SEC", "45") or "45"))


async def _await_api(value: Any, *, label: str, timeout_sec: float | None = None) -> Any:
    timeout = _api_timeout_seconds() if timeout_sec is None else max(5.0, float(timeout_sec))
    try:
        return await asyncio.wait_for(_maybe_await(value), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Solari API operation {label!r} exceeded {timeout:.0f}s"
        ) from exc


def _configure_runtime_environment() -> None:
    """Apply common runtime defaults for the enterprise VDI bridge."""
    if not os.getenv("VDI_STRONGSWAN_PREFIX") or os.getenv("VDI_STRONGSWAN_PREFIX") == "/opt/strongswan-notun":
        os.environ["VDI_STRONGSWAN_PREFIX"] = "/opt/userswan/strongswan"
    if not os.getenv("VDI_STRONGSWAN_RUNTIME_DIR") or os.getenv("VDI_STRONGSWAN_RUNTIME_DIR") == "/run/strongswan-notun":
        os.environ["VDI_STRONGSWAN_RUNTIME_DIR"] = "/run/userswan"
    if not os.getenv("VDI_FORWARDER") or os.getenv("VDI_FORWARDER", "").endswith("/notun-lwip-forwarder"):
        os.environ["VDI_FORWARDER"] = "/opt/userswan/bin/userswan-forwarder"
    os.environ.setdefault("VDI_RDP_LOCAL_HOST", "127.0.0.1")
    os.environ.setdefault("VDI_RDP_LOCAL_PORT", "3390")
    if not os.getenv("VDI_REMOTE_TARGET_IP"):
        configured_remote_ip = _env("VDI_RDP_HOST")
        if configured_remote_ip:
            os.environ["VDI_REMOTE_TARGET_IP"] = configured_remote_ip
    if not os.getenv("VDI_REMOTE_TARGET_PORT"):
        configured_remote_port = _env("VDI_RDP_PORT")
        if configured_remote_port:
            os.environ["VDI_REMOTE_TARGET_PORT"] = configured_remote_port
    os.environ.setdefault("VDI_GUI_USER", "desktop")
    os.environ.setdefault("VDI_DISPLAY", ":0")
    if not os.getenv("VDI_TEMPLATE_MANIFEST"):
        os.environ["VDI_TEMPLATE_MANIFEST"] = str(ROOT / "vdi-image.json")

    # The checked-in example recommends a PSK file.  The bridge historically
    # accepted only VDI_IPSEC_PSK, so load the file into this process without
    # ever printing it.  The bridge renders it into a mode-0600 guest runtime
    # config and redacts it from all model/log output.
    if not os.getenv("VDI_IPSEC_PSK"):
        psk_file = _env("VDI_IPSEC_PSK_FILE")
        if psk_file:
            path = Path(psk_file).expanduser()
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    os.environ["VDI_IPSEC_PSK"] = value


_configure_runtime_environment()


def _userswan_source() -> Path:
    configured = _env("VDI_USER_SWAN_SOURCE")
    source = Path(configured).expanduser() if configured else (ROOT.parent / "providers" / "userswan")
    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(
            "userswan source checkout was not found: "
            f"{source}. Set VDI_USER_SWAN_SOURCE to the bundled providers/userswan path or another checkout."
        )
    required = (
        source / "scripts" / "build-strongswan.sh",
        source / "scripts" / "build-forwarder.sh",
        source / "scripts" / "install-deps-debian.sh",
        source / "scripts" / "run-userswan.sh",
        source / "src" / "userswan-forwarder.c",
        source / "patches" / "strongswan-6.0.7"
        / "0001-kernel-libipsec-no-tun-packet-socket.patch",
    )
    missing = [str(path.relative_to(source)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "userswan checkout is incomplete; missing: " + ", ".join(missing)
        )
    return source


def _make_userswan_archive(source: Path) -> tuple[Path, str]:
    """Create a source-only archive, excluding builds, VCS data, and secrets."""
    fd, name = tempfile.mkstemp(prefix="solari-vdi-userswan-", suffix=".tar.gz")
    os.close(fd)
    archive_path = Path(name)
    excluded_dirs = {".git", "__pycache__", "build", "dist", ".venv", "secrets"}
    excluded_suffixes = {".pem", ".key", ".crt", ".p12", ".psk"}
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname="userswan", recursive=False)
        for candidate in sorted(source.rglob("*")):
            relative = candidate.relative_to(source)
            if any(part in excluded_dirs for part in relative.parts):
                continue
            if candidate.name.startswith(".env") and candidate.name != ".env.example":
                continue
            if candidate.suffix.lower() in excluded_suffixes:
                continue
            if candidate.is_file() or candidate.is_symlink():
                archive.add(
                    candidate,
                    arcname=str(Path("userswan") / relative),
                    recursive=False,
                )
    # Hash the logical archive contents rather than gzip bytes.  gzip embeds
    # the current timestamp, which would otherwise force a rebuild every run.
    digest = hashlib.sha256()
    with tarfile.open(archive_path, "r:gz") as check:
        for member in sorted(check.getmembers(), key=lambda item: item.name):
            digest.update(member.name.encode("utf-8"))
            digest.update(b"\0")
            if member.isfile():
                extracted = check.extractfile(member)
                if extracted is not None:
                    digest.update(extracted.read())
            digest.update(b"\0")
    return archive_path, digest.hexdigest()


async def _upload_file(desktop: Any, local: Path, remote: str) -> None:
    print(f"[toolkit-vdi] requesting upload URL for {local.name}", flush=True)
    info = await _await_api(desktop.upload_url(remote), label="request guest upload URL")
    url = _get(info, "url", "uploadUrl", "upload_url", default="")
    if not url:
        raise RuntimeError("Solari did not return an upload URL")
    method = str(_get(info, "method", default="PUT") or "PUT").upper()
    headers = dict(_get(info, "headers", default={}) or {})
    fields = _get(info, "fields", "formFields", default=None)
    payload = local.read_bytes()
    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
        if isinstance(fields, dict):
            response = await client.post(
                str(url),
                data=fields,
                files={"file": (local.name, payload)},
            )
        else:
            response = await client.request(
                method,
                str(url),
                headers=headers,
                content=payload,
            )
        response.raise_for_status()
    print(f"[toolkit-vdi] uploaded {local.name} to builder", flush=True)


async def _guest_exec(
    desktop: Any,
    script: str,
    *,
    label: str,
    timeout_ms: int = 60000,
) -> Any:
    print(f"[toolkit-vdi] guest step start: {label}", flush=True)
    commands = getattr(desktop, "commands", None)
    runner = getattr(commands, "run", None) if commands is not None else None
    if callable(runner):
        # Current Solari unified VM API: cmd.start is a short RPC and the SDK
        # waits for cmd.exit asynchronously, avoiding the old 300s exec-RPC ceiling.
        try:
            result = await asyncio.wait_for(
                runner("/bin/sh", args=["-c", script]),
                timeout=max(1.0, timeout_ms / 1000.0),
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"guest step {label!r} exceeded {timeout_ms / 1000:.0f}s"
            ) from exc
    else:
        result = await _maybe_await(
            desktop.exec("/bin/sh", args=["-c", script], timeout_ms=timeout_ms)
        )
    code = int(_get(result, "exit_code", "exitCode", default=0) or 0)
    stdout = str(_get(result, "stdout", "output", default="") or "")
    stderr = str(_get(result, "stderr", "error", "message", default="") or "")
    if code:
        detail = (stderr or stdout).strip()
        raise RuntimeError(
            f"guest step {label!r} failed with exit code {code}: "
            f"{detail[-5000:] or '<no guest output>'}"
        )
    print(f"[toolkit-vdi] guest step complete: {label}", flush=True)
    return result


async def _guest_check(
    desktop: Any,
    script: str,
    *,
    timeout_ms: int = 30000,
) -> tuple[bool, str]:
    commands = getattr(desktop, "commands", None)
    runner = getattr(commands, "run", None) if commands is not None else None
    if callable(runner):
        try:
            result = await asyncio.wait_for(
                runner("/bin/sh", args=["-c", script]),
                timeout=max(1.0, timeout_ms / 1000.0),
            )
        except asyncio.TimeoutError:
            return False, f"guest check exceeded {timeout_ms / 1000:.0f}s"
    else:
        result = await _maybe_await(
            desktop.exec("/bin/sh", args=["-c", script], timeout_ms=timeout_ms)
        )
    code = int(_get(result, "exit_code", "exitCode", default=0) or 0)
    stdout = str(_get(result, "stdout", "output", default="") or "")
    stderr = str(_get(result, "stderr", "error", default="") or "")
    return code == 0, (stdout + ("\n" + stderr if stderr else "")).strip()


async def _guest_has_userswan_stack(desktop: Any, source_sha256: str) -> bool:
    expected = shlex.quote(source_sha256)
    ok, _ = await _guest_check(
        desktop,
        f"""
set -eu
test -x /usr/bin/remmina
test -x /opt/userswan/strongswan/libexec/ipsec/charon
test -x /opt/userswan/strongswan/sbin/swanctl
test -x /opt/userswan/bin/userswan-forwarder
test -x /opt/userswan-src/scripts/run-userswan.sh
test -f /opt/userswan/strongswan/lib/ipsec/plugins/libstrongswan-kernel-libipsec.so
test -f /opt/solari-vdi/USERSWAN_TOOLKIT
grep -Fxq {expected} /opt/solari-vdi/userswan.source.sha256
""",
    )
    return ok


async def _guest_has_userguard_stack(desktop: Any) -> bool:
    binary = shlex.quote(_env("USERGUARD_BINARY", "/opt/userguard/bin/userguard"))
    ok, _ = await _guest_check(
        desktop,
        f"""
set -eu
test -x /usr/bin/remmina
test -x {binary}
""",
    )
    return ok


async def _create_blank_desktop(
    client: Any,
    template_id: str | None = None,
) -> Any:
    """Create a healthy base desktop and clean it up if startup fails."""
    template_id = template_id or _env("VDI_TEMPLATE_ID", "workstation") or "workstation"
    resolution = _env("VDI_RESOLUTION", "1440x900")
    cpu = int(_env("VDI_CPU", "4"))
    mem_mb = int(_env("VDI_MEM_MB", "8192"))
    timeout_ms = int(_env("VDI_TIMEOUT_MS", str(30 * 60 * 1000)))
    desktop = None
    try:
        desktop = await _await_api(
            client.create(
                template=template_id,
                resolution=resolution,
                cpu=cpu,
                mem_mb=mem_mb,
                timeout_ms=timeout_ms,
                lifecycle={"onTimeout": "pause"},
                metadata={"label": _env("VDI_BUILDER_LABEL", "solari-vdi-toolkit-builder")},
            ),
            label="create blank builder desktop",
        )
        await _maybe_await(desktop.connect())
        await agent._wait_for_desktop_ready(desktop)
        return desktop
    except Exception:
        if desktop is not None:
            await _destroy_desktop(desktop)
        raise


async def _destroy_desktop(desktop: Any) -> None:
    """Release a temporary/builder desktop without leaking a live session."""
    for name in ("kill", "destroy", "close"):
        method = getattr(desktop, name, None)
        if not callable(method):
            continue
        try:
            await _maybe_await(method())
            return
        except Exception:
            continue


async def _clean_guest_before_snapshot(desktop: Any) -> None:
    gui_user = shlex.quote(_env("VDI_GUI_USER", "desktop"))
    profile_path = shlex.quote(vdi_bridge._persistent_profile_path())
    await _guest_exec(
        desktop,
        f"""
set +e
for pidfile in /run/userswan/forwarder.pid /run/userswan/charon.pid /run/solari-vdi/remmina.pid; do
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) kill -TERM "$pid" 2>/dev/null || true; sleep 0.3; kill -KILL "$pid" 2>/dev/null || true ;;
    esac
  fi
done
pkill -x charon 2>/dev/null || true
pkill -u {gui_user} -x remmina 2>/dev/null || true
rm -rf /run/userswan /run/solari-vdi
rm -f /tmp/solari-vdi-remmina-*.remmina
rm -f {profile_path}
find /run/secrets -maxdepth 1 -type f -delete 2>/dev/null || true
find /etc/swanctl -type f -delete 2>/dev/null || true
""",
        label="remove userswan runtime and credentials before snapshot",
        timeout_ms=30000,
    )


async def _install_userswan_stack(
    desktop: Any,
    archive_path: Path,
    source_sha256: str,
) -> None:
    await _upload_file(desktop, archive_path, "/tmp/solari-vdi-userswan.tar.gz")
    script = r"""
set -eu
export DEBIAN_FRONTEND=noninteractive
test "$(id -u)" -eq 0

# Build tooling and GUI packages. The checked-in userswan dependency script
# installs the pinned build prerequisites, including liblwip-dev.
rm -rf /opt/userswan-src
mkdir -p /opt/userswan-src
tar -xzf /tmp/solari-vdi-userswan.tar.gz \
  -C /opt/userswan-src --strip-components=1

sh /opt/userswan-src/scripts/install-deps-debian.sh
apt-get install -y --no-install-recommends \
  remmina remmina-plugin-rdp remmina-plugin-secret gnome-keyring \
  wireguard-tools wireguard-go iproute2 nftables iputils-ping \
  netcat-openbsd dnsutils dbus-x11 wmctrl xdotool procps psmisc \
  ca-certificates curl jq python3 python3-venv

systemctl disable --now strongswan-starter strongswan strongswan-swanctl charon-systemd \
  >/dev/null 2>&1 || true
pkill -x charon >/dev/null 2>&1 || true
rm -f /run/charon.vici /run/charon.pid

rm -rf /opt/userswan
mkdir -p /opt/userswan /run/userswan
cd /opt/userswan-src
PREFIX=/opt/userswan/strongswan \
  STRONGSWAN_VERSION="${STRONGSWAN_VERSION:-6.0.7}" \
  PIDDIR=/run/userswan \
  sh ./scripts/build-strongswan.sh
PREFIX=/opt/userswan sh ./scripts/build-forwarder.sh

# Make the custom prefix loadable by direct charon/swanctl invocations.
printf '%s\n' /opt/userswan/strongswan/lib /opt/userswan/strongswan/lib/ipsec \
  >/etc/ld.so.conf.d/solari-userswan-toolkit.conf
ldconfig

install -d -m 0755 /opt/solari-vdi /etc/solari-vdi /var/lib/solari-vdi
install -d -m 0700 /run/userswan /run/solari-vdi /run/secrets /etc/wireguard
printf '%s\n' "solari-vdi-userswan" >/opt/solari-vdi/IMAGE
printf '%s\n' "__SOURCE_SHA256__" >/opt/solari-vdi/userswan.source.sha256
cat >/opt/solari-vdi/USERSWAN_TOOLKIT <<'EOF'
userswan source: /opt/userswan-src
strongSwan prefix: /opt/userswan/strongswan
forwarder: /opt/userswan/bin/userswan-forwarder
runtime: /run/userswan
backend: patched kernel-libipsec no_tun AF_UNIX packet socket
EOF

# Prove the patched no-TUN plugin can start and create both sockets without
# starting a VPN or retaining runtime state in the snapshot.
check=/run/userswan-build-check
rm -rf "$check"
mkdir -p "$check"
cat >"$check/strongswan.conf" <<'EOF'
charon {
  load_modular = yes
  port = 15000
  port_nat_t = 14500
  routing_table = 0
  install_routes = no
  install_virtual_ip = no
  plugins {
    include /opt/userswan/strongswan/etc/strongswan.d/charon/*.conf
    kernel-libipsec {
      load = 2
      no_tun = yes
      packet_socket = /run/userswan-build-check/notun.sock
    }
    kernel-netlink { load = 1 }
    vici { socket = unix:///run/userswan-build-check/charon.vici }
  }
}
swanctl { socket = unix:///run/userswan-build-check/charon.vici }
EOF
STRONGSWAN_CONF="$check/strongswan.conf" \
  /opt/userswan/strongswan/libexec/ipsec/charon \
  >"$check/charon.log" 2>&1 &
pid=$!
ready=0
for _ in $(seq 1 150); do
  if [ -S "$check/charon.vici" ] && [ -S "$check/notun.sock" ]; then
    ready=1
    break
  fi
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.1
done
if [ "$ready" -ne 1 ]; then
  cat "$check/charon.log" >&2 || true
  kill "$pid" 2>/dev/null || true
  exit 41
fi
kill -TERM "$pid" 2>/dev/null || true
sleep 0.5
kill -KILL "$pid" 2>/dev/null || true
rm -rf "$check"

rm -f /tmp/solari-vdi-userswan.tar.gz
apt-get clean
rm -rf /var/lib/apt/lists/*
""".replace("__SOURCE_SHA256__", source_sha256)
    await _guest_exec(
        desktop,
        script,
        label="install current userswan, Remmina, and no-TUN self-test",
        timeout_ms=int(_env("VDI_BUILD_GUEST_TIMEOUT_MS", str(45 * 60 * 1000))),
    )
    if not await _guest_has_userswan_stack(desktop, source_sha256):
        raise RuntimeError("guest userswan/Remmina verification failed after installation")


def _manifest_path() -> Path:
    return Path(
        _env("VDI_TEMPLATE_MANIFEST", str(ROOT / "vdi-image.json"))
    ).expanduser().resolve()


async def _write_manifest(
    snapshot_id: str,
    *,
    source_sha256: str,
    builder_session_id: str = "",
) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    packages = [
        "remmina",
        "remmina-plugin-rdp",
        "remmina-plugin-secret",
        "gnome-keyring",
        "wireguard-tools",
        "wireguard-go",
        "iproute2",
        "nftables",
        "liblwip-dev",
        "build-essential",
        "libssl-dev",
        "libgmp-dev",
        "libcap-dev",
        "python3",
        "tcpdump",
        "netcat-openbsd",
    ]
    data = {
        "schema": 2,
        "kind": "solari-vdi-desktop-snapshot",
        "implementation_version": "userswan-no-tun",
        "name": _env("VDI_IMAGE_NAME", "solari-enterprise-vdi-toolkit-userswan"),
        "snapshot_id": snapshot_id,
        "base_template": _env("VDI_TEMPLATE_ID", "workstation") or "workstation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolution": _env("VDI_RESOLUTION", "1440x900"),
        "userswan": {
            "source_sha256": source_sha256,
            "strongswan_version": _env("STRONGSWAN_VERSION", "6.0.7"),
            "strongswan_prefix": "/opt/userswan/strongswan",
            "forwarder": "/opt/userswan/bin/userswan-forwarder",
            "runtime_dir": "/run/userswan",
            "backend": "patched kernel-libipsec no_tun AF_UNIX packet socket",
        },
        "remmina": {
            "installed": True,
            "profile_provisioned_at_runtime": True,
            "profile_server": "127.0.0.1:3390",
        },
        "packages": packages,
        "builder_session_id": builder_session_id or None,
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def _build_userswan_snapshot(
    client: Any,
    *,
    template_id: str,
    archive_path: Path,
    source_sha256: str,
) -> str:
    builder = None
    try:
        print(
            f"[toolkit-vdi] building userswan template from base={template_id!r}; "
            f"source_sha256={source_sha256[:16]}…",
            flush=True,
        )
        try:
            builder = await _create_blank_desktop(client, template_id)
        except Exception as first_exc:
            if template_id == "workstation":
                raise
            print(
                f"[toolkit-vdi] base template {template_id!r} was unavailable "
                f"({type(first_exc).__name__}); retrying with 'workstation'",
                flush=True,
            )
            template_id = "workstation"
            builder = await _create_blank_desktop(client, template_id)
        # Persist the actual base template selected above so validation and
        # future runs do not keep retrying an unavailable template ID.
        os.environ["VDI_TEMPLATE_ID"] = template_id
        print("[toolkit-vdi] builder ready; uploading/installing userswan stack", flush=True)
        await _install_userswan_stack(builder, archive_path, source_sha256)
        print("[toolkit-vdi] userswan stack installed; cleaning builder before snapshot", flush=True)
        await _clean_guest_before_snapshot(builder)
        print("[toolkit-vdi] builder clean; creating reusable snapshot", flush=True)
        image_name = _env("VDI_IMAGE_NAME", "solari-enterprise-vdi-toolkit-userswan")
        snapshot = await _await_api(
            builder.snapshot(image_name),
            label="create snapshot",
            timeout_sec=float(_env("VDI_SOLARI_SNAPSHOT_TIMEOUT_SEC", "600") or "600"),
        )
        snapshot_id = str(
            _get(snapshot, "snapshot_id", "snapshotId", "id", default=snapshot)
            or ""
        ).strip()
        if not snapshot_id:
            raise RuntimeError("Solari did not return a snapshot ID")
        builder_id = str(_get(builder, "session_id", "sessionId", "id", default="") or "")
        await _write_manifest(
            snapshot_id,
            source_sha256=source_sha256,
            builder_session_id=builder_id if _truthy("VDI_KEEP_TEMPLATE_BUILDER") else "",
        )
        agent._persist_vdi_env(
            {
                "VDI_TEMPLATE_ID": template_id,
                "VDI_IMAGE_SNAPSHOT_ID": snapshot_id,
            }
        )
        print(f"[toolkit-vdi] reusable snapshot ready: {snapshot_id}", flush=True)
        return snapshot_id
    finally:
        if builder is not None and not _truthy("VDI_KEEP_TEMPLATE_BUILDER"):
            await _destroy_desktop(builder)


async def _create_from_snapshot(client: Any, template_id: str, snapshot_id: str) -> Any:
    base_kwargs = {
        "template": template_id,
        "resolution": _env("VDI_RESOLUTION", "1440x900"),
        "cpu": int(_env("VDI_CPU", "4")),
        "mem_mb": int(_env("VDI_MEM_MB", "8192")),
        "timeout_ms": int(_env("VDI_TIMEOUT_MS", str(30 * 60 * 1000))),
        "lifecycle": {"onTimeout": "pause"},
    }
    variants = [
        {**base_kwargs, "from_snapshot": snapshot_id},
        {**base_kwargs, "fromSnapshot": snapshot_id},
        {**base_kwargs, "snapshot_id": snapshot_id},
        {**base_kwargs, "snapshotId": snapshot_id},
    ]
    last_type_error: Exception | None = None
    desktop = None
    for kwargs in variants:
        try:
            desktop = await _await_api(
                client.create(
                    **kwargs,
                    metadata={"label": _env("VDI_DESKTOP_LABEL", "solari-vdi-toolkit")},
                ),
                label="create desktop from snapshot",
                timeout_sec=float(_env("VDI_SOLARI_CLONE_TIMEOUT_SEC", "180") or "180"),
            )
            break
        except TypeError as exc:
            last_type_error = exc
            try:
                desktop = await _await_api(
                    client.create(**kwargs),
                    label="create desktop from snapshot",
                    timeout_sec=float(_env("VDI_SOLARI_CLONE_TIMEOUT_SEC", "180") or "180"),
                )
                break
            except TypeError as inner:
                last_type_error = inner
                continue

    # Older SDKs do not accept a snapshot keyword.  In that case restore the
    # snapshot explicitly; never silently fall back to a generic desktop.
    if desktop is None:
        try:
            desktop = await _await_api(
                client.create(
                    **base_kwargs,
                    metadata={"label": _env("VDI_DESKTOP_LABEL", "solari-vdi-toolkit")},
                ),
                label="create fallback desktop",
            )
        except TypeError:
            desktop = await _await_api(client.create(**base_kwargs), label="create fallback desktop")
        await _maybe_await(desktop.connect())
        await agent._wait_for_desktop_ready(desktop)
        revert = getattr(desktop, "revert", None)
        if not callable(revert):
            raise RuntimeError(
                "Solari SDK supports neither snapshot create arguments nor desktop.revert"
            )
        await _maybe_await(revert(snapshot_id))
        await _maybe_await(desktop.connect())
        await agent._wait_for_desktop_ready(desktop)
    else:
        await _maybe_await(desktop.connect())
        await agent._wait_for_desktop_ready(desktop)
    return desktop


async def _attach_created_desktop(
    mcp: Any,
    desktop_sdk: Any,
    desktop: Any,
) -> tuple[str, Any, Any]:
    """Register the SDK-created desktop with MCP and start its live RFB."""
    session_id = agent._desktop_handle_id(desktop)
    if not session_id:
        raise RuntimeError("created desktop did not expose a session ID")
    confirmed_id = await agent._mcp_attach_session(mcp, session_id)
    await _maybe_await(desktop.connect())
    await agent._wait_for_desktop_ready(desktop)
    agent._configure_vdi_bridge(desktop_sdk, desktop)
    frame_cache = await agent._start_frame_cache_for_desktop(desktop)
    agent._persist_vdi_env({"VDI_DESKTOP_ID": confirmed_id})
    return confirmed_id, frame_cache, desktop


async def _validate_snapshot(
    client: Any,
    *,
    template_id: str,
    snapshot_id: str,
    source_sha256: str,
) -> Any | None:
    """Instantiate and validate an existing snapshot, returning that same desktop.

    A successful validation desktop is already a fresh operational instance of
    the requested snapshot, so Toolkit adopts it instead of destroying it and
    creating an identical second desktop. Invalid/failed validation instances
    are still destroyed here.
    """
    desktop = None
    try:
        print(f"[toolkit-vdi] validating snapshot {snapshot_id[:20]}…", flush=True)
        desktop = await _create_from_snapshot(client, template_id, snapshot_id)
        ok = await _guest_has_userswan_stack(desktop, source_sha256)
        if not ok:
            print("[toolkit-vdi] snapshot exists but lacks the current userswan/Remmina stack", flush=True)
            invalid_desktop = desktop
            desktop = None
            await _destroy_desktop(invalid_desktop)
            return None
        print("[toolkit-vdi] snapshot validated; adopting validation desktop as the operational desktop", flush=True)
        return desktop
    except Exception as exc:
        print(
            f"[toolkit-vdi] snapshot unavailable or invalid ({type(exc).__name__}: {exc})",
            flush=True,
        )
        if desktop is not None:
            try:
                await _destroy_desktop(desktop)
            except Exception:
                pass
        return None


async def _prepare_userguard_desktop(mcp: Any, desktop_sdk: Any) -> tuple[str, Any, Any]:
    """Create a fresh desktop from a snapshot that already contains userguard."""
    if _truthy("VDI_REBUILD_TEMPLATE"):
        raise RuntimeError(
            "automatic --rebuild-template is available for userswan. For userguard, build "
            "providers/userguard and bake the resulting binary into a Solari snapshot first."
        )
    template_id = _env("VDI_TEMPLATE_ID", "workstation") or "workstation"
    snapshot_id = _env("VDI_IMAGE_SNAPSHOT_ID")
    if not snapshot_id:
        raise RuntimeError(
            "VPN_PROVIDER=userguard requires VDI_IMAGE_SNAPSHOT_ID pointing to a Solari snapshot "
            "that contains Remmina and /opt/userguard/bin/userguard. Build userguard from providers/userguard first."
        )
    desktop = await _create_from_snapshot(desktop_sdk, template_id, snapshot_id)
    if not await _guest_has_userguard_stack(desktop):
        await _destroy_desktop(desktop)
        raise RuntimeError(
            "configured userguard snapshot does not contain both Remmina and the userguard binary"
        )
    agent.VDI_TEMPLATE_ID = template_id
    agent.VDI_IMAGE_SNAPSHOT_ID = snapshot_id
    agent.VDI_DESKTOP_ID = ""
    agent.VDI_PREFER_SAVED_DESKTOP = False
    fresh_id, frame_cache, desktop = await _attach_created_desktop(
        mcp, desktop_sdk, desktop
    )
    agent._persist_vdi_env({
        "VDI_TEMPLATE_ID": template_id,
        "VDI_IMAGE_SNAPSHOT_ID": snapshot_id,
        "VDI_DESKTOP_ID": fresh_id,
    })
    if hasattr(vdi_bridge, "_CLIENT"):
        vdi_bridge._CLIENT = desktop_sdk
    if hasattr(vdi_bridge, "_DESKTOP"):
        vdi_bridge._DESKTOP = desktop
    print(f"[toolkit-vdi] userguard-ready desktop instantiated; session={fresh_id}", flush=True)
    return fresh_id, frame_cache, desktop


async def _prepare_vdi_desktop(mcp: Any, desktop_sdk: Any) -> tuple[str, Any, Any]:
    """Prepare one fresh desktop for the selected enterprise VPN provider."""
    _configure_runtime_environment()
    provider = _env("VPN_PROVIDER", "userswan").lower() or "userswan"
    if provider == "userguard":
        return await _prepare_userguard_desktop(mcp, desktop_sdk)
    if provider != "userswan":
        raise RuntimeError("VPN_PROVIDER must be 'userswan' or 'userguard'")
    source = _userswan_source()
    archive_path, source_sha256 = _make_userswan_archive(source)
    try:
        raw_template_id = os.getenv("VDI_TEMPLATE_ID", "").strip()
        template_id = raw_template_id or "workstation"
        snapshot_id = _env("VDI_IMAGE_SNAPSHOT_ID")
        force_build = (
            not raw_template_id
            or not snapshot_id
            or _truthy("VDI_REBUILD_TEMPLATE")
        )

        desktop = None
        if not force_build:
            desktop = await _validate_snapshot(
                desktop_sdk,
                template_id=template_id,
                snapshot_id=snapshot_id,
                source_sha256=source_sha256,
            )
            force_build = desktop is None

        if force_build:
            reason = "missing/stale/invalid snapshot" if snapshot_id else "no snapshot ID"
            if not raw_template_id:
                reason = "VDI_TEMPLATE_ID is missing"
            print(f"[toolkit-vdi] {reason}; compiling a new userswan template", flush=True)
            snapshot_id = await _build_userswan_snapshot(
                desktop_sdk,
                template_id=template_id,
                archive_path=archive_path,
                source_sha256=source_sha256,
            )
            # _build_userswan_snapshot may have fallen back from a missing custom
            # base template to the known Solari workstation template.
            template_id = _env("VDI_TEMPLATE_ID", template_id) or "workstation"

        agent.VDI_TEMPLATE_ID = template_id
        agent.VDI_IMAGE_SNAPSHOT_ID = snapshot_id
        agent.VDI_DESKTOP_ID = ""
        agent.VDI_PREFER_SAVED_DESKTOP = False

        if desktop is None:
            # New/rebuilt snapshot path: create the operational desktop now.
            # Existing valid snapshots already supplied ``desktop`` from
            # _validate_snapshot(), avoiding a duplicate instantiation.
            desktop = await _create_from_snapshot(
                desktop_sdk,
                template_id,
                snapshot_id,
            )
            if not await _guest_has_userswan_stack(desktop, source_sha256):
                await _destroy_desktop(desktop)
                raise RuntimeError(
                    "desktop created from the snapshot failed guest stack verification"
                )
        else:
            print(
                "[toolkit-vdi] reusing validated snapshot desktop; no second desktop instantiation",
                flush=True,
            )
        fresh_id, frame_cache, desktop = await _attach_created_desktop(
            mcp,
            desktop_sdk,
            desktop,
        )
        agent._persist_vdi_env(
            {
                "VDI_TEMPLATE_ID": template_id,
                "VDI_IMAGE_SNAPSHOT_ID": snapshot_id,
                "VDI_DESKTOP_ID": fresh_id,
            }
        )
        if hasattr(vdi_bridge, "_CLIENT"):
            vdi_bridge._CLIENT = desktop_sdk
        if hasattr(vdi_bridge, "_DESKTOP"):
            vdi_bridge._DESKTOP = desktop
        print(
            f"[toolkit-vdi] desktop instantiated from snapshot; session={fresh_id}",
            flush=True,
        )
        return fresh_id, frame_cache, desktop
    finally:
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _main() -> None:
    args = _ARGS
    if args.debug_ui:
        agent.DEMO_UI.set_mode("debug")
    # Hide technical diagnostics before provisioning begins. They are still
    # captured verbatim in the Toolkit session log; debug mode mirrors them.
    agent.DEMO_UI.install_capture()
    agent.DEMO_UI.start_working("Preparing secure workspace…")
    if LOADED_CONFIG_FILE is not None:
        print(f"[toolkit] configuration loaded: {LOADED_CONFIG_FILE}", flush=True)
    if args.rebuild_template:
        os.environ["VDI_REBUILD_TEMPLATE"] = "1"
    # A normal toolkit invocation always provisions a fresh desktop. Reuse is
    # explicit via --reuse; an old value in .env must not silently change that.
    os.environ["VDI_REUSE_DESKTOP"] = "1" if args.reuse else "0"
    if args.no_vpn_preflight:
        os.environ["VDI_PREPARE_ON_START"] = "0"
        # The engine reads this flag at import time; update its live global too.
        agent.VDI_PREPARE_ON_START = False

    original_resolver = agent.resolve_existing_desktop

    async def resolver(mcp: Any, desktop_sdk: Any):
        if _truthy("VDI_REUSE_DESKTOP") and agent.VDI_DESKTOP_ID:
            return await original_resolver(mcp, desktop_sdk)
        return await _prepare_vdi_desktop(mcp, desktop_sdk)

    agent.resolve_existing_desktop = resolver
    agent.vdi_bridge = vdi_bridge
    try:
        await agent.main()
    finally:
        agent.DEMO_UI.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="explicitly attach to VDI_DESKTOP_ID instead of creating a fresh desktop",
    )
    parser.add_argument(
        "--rebuild-template",
        action="store_true",
        help="force a new snapshot build from the configured userswan provider source",
    )
    parser.add_argument(
        "--no-vpn-preflight",
        action="store_true",
        help="leave VPN/profile startup to the model's vdi_vpn_connect tool",
    )
    parser.add_argument(
        "--debug-ui",
        action="store_true",
        help="mirror the full technical trace to the terminal instead of the enterprise demo UI",
    )
    return parser.parse_args()


_ARGS = _parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        raise SystemExit(130)
