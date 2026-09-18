#!/usr/bin/env python3
"""Bitwarden/Vaultwarden secrets + MFA MCP for Solari desktops.

Secret values never appear in MCP responses. The server retrieves a requested
field host-side and types it directly through Solari's Desktop keyboard channel.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP


def _load_dotenv() -> None:
    path = Path(os.getenv("VDI_ENV_FILE", str(Path(__file__).resolve().parent.parent / ".env"))).expanduser()
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


_load_dotenv()

from security_vault import VAULT, VaultError
from solari_desktop import DesktopClient

mcp = FastMCP(
    "solari-vdi-security",
    instructions=(
        "Search safe Bitwarden/Vaultwarden item metadata and type username, password, "
        "or TOTP values directly into the attached Solari desktop. Secret values are "
        "never returned in tool responses."
    ),
)

_CLIENT: DesktopClient | None = None
_DESKTOP: Any | None = None
_LOCK = asyncio.Lock()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


async def _desktop() -> Any:
    global _CLIENT, _DESKTOP
    async with _LOCK:
        if _DESKTOP is not None:
            return _DESKTOP
        desktop_id = _env("VDI_DESKTOP_ID") or _env("SOLARI_DESKTOP_ID")
        if not desktop_id:
            raise RuntimeError("VDI_DESKTOP_ID/SOLARI_DESKTOP_ID is required")
        api_key = _env("SOLARI_API_KEY")
        if not api_key:
            raise RuntimeError("SOLARI_API_KEY is required")
        _CLIENT = DesktopClient(
            api_key=api_key,
            base_url=_env("SOLARI_API_BASE", "https://api.getsolari.com"),
        )
        _DESKTOP = await _CLIENT.connect(desktop_id)
        await _DESKTOP.connect()
        return _DESKTOP


@mcp.tool
async def secrets_status() -> dict[str, Any]:
    """Return sanitized Vaultwarden/Bitwarden readiness information."""
    status = await asyncio.to_thread(VAULT.safe_status)
    return {"secret_values_returned": False, **status}


@mcp.tool
async def secrets_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search safe vault item metadata; never returns credential values."""
    items = await asyncio.to_thread(VAULT.search, query, limit=limit)
    return {
        "success": True,
        "items": items,
        "secret_values_returned": False,
    }


async def _type_secret(field: str, credential: str, submit: bool) -> dict[str, Any]:
    getters = {
        "username": VAULT.get_username,
        "password": VAULT.get_password,
        "totp": VAULT.get_totp,
    }
    if field not in getters:
        raise VaultError(f"unsupported secret field: {field}")
    secret = ""
    try:
        secret = await asyncio.to_thread(getters[field], credential)
        desktop = await _desktop()
        await desktop.keyboard.type(secret)
        if submit:
            await desktop.keyboard.press("Return")
        return {
            "success": True,
            "field": field,
            "credential": credential,
            "submitted": bool(submit),
            "secret_values_returned": False,
        }
    finally:
        secret = ""


@mcp.tool
async def secrets_type_username(credential: str, submit: bool = False) -> dict[str, Any]:
    """Type a vault username into the currently focused field."""
    return await _type_secret("username", credential, submit)


@mcp.tool
async def secrets_type_password(credential: str, submit: bool = False) -> dict[str, Any]:
    """Type a vault password into the currently focused field."""
    return await _type_secret("password", credential, submit)


@mcp.tool
async def mfa_type_totp(credential: str, submit: bool = True) -> dict[str, Any]:
    """Generate/type the vault item's current TOTP code and optionally submit it."""
    return await _type_secret("totp", credential, submit)


if __name__ == "__main__":
    mcp.run()
