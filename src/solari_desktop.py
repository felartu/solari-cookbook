#!/usr/bin/env python3
"""Toolkit compatibility client for Solari's current unified VM API.

Solari migrated GUI VMs to the shared /sandboxes lifecycle surface.  The
published solari-desktop 0.2.0 client still creates through the legacy
/desktops route and does not expose from_snapshot.  Toolkit uses solari-sandbox
0.2.1's unified create_desktop() and constructs a GUI Desktop handle for
reattach so the rest of the agent can retain its DesktopClient-shaped API.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

from solari_sandbox import SandboxClient
from solari_core import CreateDesktopResponse, Desktop, DesktopConfig


class DesktopClient:
    """DesktopClient-shaped wrapper backed by SandboxClient(kind='desktop')."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: Any | None = None,
        call_timeout_ms: int | None = None,
    ) -> None:
        self._client = SandboxClient(
            api_key=api_key,
            base_url=base_url,
            http=http,
            call_timeout_ms=call_timeout_ms,
            kind="desktop",
        )
        self.volumes = self._client.volumes
        self._call_timeout_ms = call_timeout_ms

    async def __aenter__(self) -> "DesktopClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create(
        self,
        *,
        template: str = "workstation",
        ttl_seconds: Optional[int] = None,
        resolution: Optional[str] = None,
        cpu: Optional[int] = None,
        mem_mb: Optional[int] = None,
        metadata: Optional[Dict[str, str]] = None,
        record: Optional[bool] = None,
        timeout_ms: Optional[int] = None,
        lifecycle: Optional[Dict[str, Any]] = None,
        volumes: Optional[list] = None,
        from_snapshot: Optional[str] = None,
        fromSnapshot: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        snapshotId: Optional[str] = None,
        **_unused: Any,
    ) -> Desktop:
        # Preserve all historical Toolkit snapshot keyword spellings, but always
        # send the current SDK's from_snapshot field to the unified route.
        snap = from_snapshot or fromSnapshot or snapshot_id or snapshotId
        if timeout_ms is None and ttl_seconds is not None:
            timeout_ms = int(ttl_seconds) * 1000
        return await self._client.create_desktop(
            template=template,
            cpu=cpu,
            mem_mb=mem_mb,
            metadata=metadata,
            timeout_ms=timeout_ms,
            from_snapshot=snap,
            resolution=resolution,
            record=record,
            lifecycle=lifecycle,
            volumes=volumes,
        )

    def _desktop_config(self) -> DesktopConfig:
        base = self._client._handle_config()
        cfg = DesktopConfig(headers=base.headers, hooks=base.hooks)
        if base.callTimeoutMs is not None:
            cfg.callTimeoutMs = base.callTimeoutMs
        return cfg

    async def connect(self, session_id: str) -> Desktop:
        """Reattach a unified-route GUI VM as a Desktop rather than Sandbox."""
        encoded = quote(session_id, safe="")
        data = await self._client._request("GET", f"/sandboxes/{encoded}")
        state = str((data or {}).get("state") or "").lower()
        kind = str((data or {}).get("kind") or "desktop").lower()
        if kind != "desktop":
            raise RuntimeError(f"session {session_id!r} is not a desktop (kind={kind!r})")
        if state == "paused":
            await self._client._hook_resume(session_id)
            data = await self._client._request("GET", f"/sandboxes/{encoded}")
        origin = self._client._t.ws_origin()
        session = CreateDesktopResponse(
            sessionId=session_id,
            controlUrl=f"{origin}/control/{encoded}",
            streamUrl=f"{origin}/stream/{encoded}",
            expiresAt=str((data or {}).get("expiresAt") or ""),
            recordingUrl=(data or {}).get("recordingUrl"),
        )
        return Desktop(session, self._desktop_config())

    async def get(self, session_id: str) -> Any:
        return await self._client.get(session_id)

    async def destroy(self, session_id: str) -> Any:
        return await self._client.kill(session_id)

    async def kill(self, session_id: str) -> Any:
        return await self._client.kill(session_id)


__all__ = ["DesktopClient"]
