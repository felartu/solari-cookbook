#!/usr/bin/env python3
"""Host-only Vaultwarden/Bitwarden credential backend for Solari Enterprise VDI Toolkit.

The model never calls ``bw`` directly and never receives secret material.
This module intentionally returns secrets only to trusted Python callers in the
host process. The Toolkit engine immediately types them into the attached Solari
desktop and returns only redacted success/failure metadata to the model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VaultError(RuntimeError):
    pass


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class SafeVaultItem:
    id: str
    name: str
    kind: str
    has_username: bool
    has_password: bool
    has_totp: bool

    def to_model_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "has_username": self.has_username,
            "has_password": self.has_password,
            "has_totp": self.has_totp,
        }


class BitwardenVault:
    """Small non-shelling wrapper around the official ``bw`` CLI.

    Authentication state lives in an isolated Bitwarden CLI app-data directory
    by default, so Toolkit does not disturb a human operator's normal ``bw`` login.
    For unattended use, the recommended setup is:

      * BW_CLIENTID / BW_CLIENTSECRET for ``bw login --apikey``;
      * VAULT_MASTER_PASSWORD_FILE for ``bw unlock --passwordfile``.

    The generated BW_SESSION token is held only in process memory.
    """

    def __init__(self) -> None:
        self.enabled = _truthy("VAULT_ENABLED", True)
        self.backend = _env("VAULT_BACKEND", "bitwarden").lower() or "bitwarden"
        self.bw_bin = _env("VAULT_BW_BIN", "bw") or "bw"
        self.server_url = _env("VAULT_SERVER_URL")
        self.timeout = max(5.0, float(_env("VAULT_COMMAND_TIMEOUT", "30") or "30"))
        self.auto_sync = _truthy("VAULT_AUTO_SYNC", True)
        self.lock_on_exit = _truthy("VAULT_LOCK_ON_EXIT", True)
        self.master_password_file = _env("VAULT_MASTER_PASSWORD_FILE")
        self.session_file = _env("VAULT_BW_SESSION_FILE")
        root = Path(__file__).resolve().parent.parent
        appdata = Path(_env("VAULT_BW_APPDATA_DIR", ".vault-bw-toolkit")).expanduser()
        self.appdata_dir = appdata if appdata.is_absolute() else (root / appdata)
        self.root = root
        self._session: str | None = None
        self._configured_server = False
        self._lock = threading.RLock()

    def _base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["BITWARDENCLI_APPDATA_DIR"] = str(self.appdata_dir)
        if self._session:
            env["BW_SESSION"] = self._session
        return env

    def _binary_path(self) -> str | None:
        if os.path.isabs(self.bw_bin) or "/" in self.bw_bin:
            return self.bw_bin if Path(self.bw_bin).is_file() else None
        return shutil.which(self.bw_bin)

    def installed(self) -> bool:
        return self._binary_path() is not None

    def _run(
        self,
        args: list[str],
        *,
        require_session: bool = False,
        env_extra: dict[str, str] | None = None,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        binary = self._binary_path()
        if not binary:
            raise VaultError(
                "Bitwarden CLI 'bw' is not installed. Install it or set VAULT_BW_BIN."
            )
        self.appdata_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.appdata_dir, 0o700)
        except OSError:
            pass
        env = self._base_env()
        if env_extra:
            env.update(env_extra)
        cmd = [binary, *args]
        if require_session:
            session = self._ensure_session_locked()
            env["BW_SESSION"] = session
            # Prefer environment delivery so the decryption key is not exposed
            # in argv/process listings.
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=self.timeout,
            check=False,
        )
        if proc.returncode != 0 and not allow_failure:
            detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
            if len(detail) > 320:
                detail = detail[-320:]
            # Never include command arguments or stdout from successful secret
            # retrieval in errors/logs.
            raise VaultError(
                f"Bitwarden CLI command failed with exit code {proc.returncode}"
                + (f": {detail}" if detail else "")
            )
        return proc

    def _status_raw(self) -> dict[str, Any]:
        proc = self._run(["status"], allow_failure=True)
        if proc.returncode != 0:
            return {"status": "unknown"}
        try:
            value = json.loads(proc.stdout or "{}")
            return value if isinstance(value, dict) else {"status": "unknown"}
        except Exception:
            return {"status": "unknown"}

    def _configure_server_locked(self) -> None:
        if self._configured_server or not self.server_url:
            return
        status = self._status_raw()
        current = str(status.get("serverUrl") or status.get("server_url") or "").rstrip("/")
        wanted = self.server_url.rstrip("/")
        if current != wanted:
            # Isolated app-data makes this safe; no human/global bw session is
            # affected. Bitwarden requires configuring the server before login.
            self._run(["logout"], allow_failure=True)
            self._run(["config", "server", self.server_url])
        self._configured_server = True

    def _resolve_local_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.root / path)

    def _session_from_file_locked(self) -> str | None:
        if not self.session_file:
            return None
        path = self._resolve_local_path(self.session_file)
        try:
            token = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return None
        return token or None

    def _ensure_session_locked(self) -> str:
        if not self.enabled:
            raise VaultError("Security vault is disabled by VAULT_ENABLED=0")
        if self.backend != "bitwarden":
            raise VaultError(f"Unsupported VAULT_BACKEND={self.backend!r}")
        if self._session:
            return self._session

        inherited = os.getenv("BW_SESSION", "").strip()
        if inherited:
            self._session = inherited
            return inherited
        from_file = self._session_from_file_locked()
        if from_file:
            self._session = from_file
            return from_file

        self._configure_server_locked()
        status = self._status_raw()
        state = str(status.get("status") or "unknown").lower()

        if state == "unauthenticated":
            client_id = os.getenv("BW_CLIENTID", "").strip()
            client_secret = os.getenv("BW_CLIENTSECRET", "").strip()
            if not client_id or not client_secret:
                raise VaultError(
                    "Vault is not logged in. Configure BW_CLIENTID and BW_CLIENTSECRET "
                    "for unattended API-key login, or pre-login the isolated bw profile."
                )
            # Credentials are inherited through the environment, never argv.
            self._run(["login", "--apikey"])
            status = self._status_raw()
            state = str(status.get("status") or "unknown").lower()

        if state not in {"locked", "unlocked"}:
            raise VaultError(f"Unexpected Bitwarden vault state: {state}")

        if self.master_password_file:
            path = self._resolve_local_path(self.master_password_file)
            if not path.is_file():
                raise VaultError("VAULT_MASTER_PASSWORD_FILE does not exist")
            proc = self._run(["unlock", "--passwordfile", str(path), "--raw"])
        else:
            master = os.getenv("VAULT_MASTER_PASSWORD", "")
            if not master:
                raise VaultError(
                    "Vault is locked. Configure VAULT_MASTER_PASSWORD_FILE (recommended) "
                    "or VAULT_MASTER_PASSWORD."
                )
            proc = self._run(
                ["unlock", "--passwordenv", "TOOLKIT_VAULT_MASTER_PASSWORD", "--raw"],
                env_extra={"TOOLKIT_VAULT_MASTER_PASSWORD": master},
            )
        token = (proc.stdout or "").strip()
        if not token:
            raise VaultError("Bitwarden unlock succeeded but returned no session key")
        self._session = token
        if self.auto_sync:
            self._run(["sync"], require_session=True, allow_failure=True)
        return token

    def ensure_ready(self) -> None:
        with self._lock:
            self._ensure_session_locked()

    def safe_status(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "backend": self.backend,
                "ready": False,
            }
        if not self.installed():
            return {
                "enabled": True,
                "backend": self.backend,
                "ready": False,
                "bw_installed": False,
            }
        try:
            self.ensure_ready()
            status = self._status_raw()
            return {
                "enabled": True,
                "backend": self.backend,
                "ready": True,
                "bw_installed": True,
                "server": self.server_url or str(status.get("serverUrl") or "self-configured"),
                "vault_state": "unlocked",
            }
        except Exception as exc:
            return {
                "enabled": True,
                "backend": self.backend,
                "ready": False,
                "bw_installed": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _safe_item(value: dict[str, Any]) -> SafeVaultItem:
        login = value.get("login") if isinstance(value.get("login"), dict) else {}
        item_type = value.get("type")
        kind = "login" if item_type in {1, "1", "login"} or login else str(item_type or "item")
        return SafeVaultItem(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or "unnamed item"),
            kind=kind,
            has_username=bool(login.get("username")),
            has_password=bool(login.get("password")),
            has_totp=bool(login.get("totp")),
        )

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            raise VaultError("query cannot be empty")
        with self._lock:
            self._ensure_session_locked()
            proc = self._run(
                ["list", "items", "--search", query],
                require_session=True,
            )
            try:
                values = json.loads(proc.stdout or "[]")
            except Exception as exc:
                raise VaultError("Bitwarden returned invalid JSON for item search") from exc
            if not isinstance(values, list):
                raise VaultError("Bitwarden returned an unexpected item search response")
            safe = [self._safe_item(v).to_model_dict() for v in values if isinstance(v, dict)]
            return safe[: max(1, min(int(limit), 25))]

    def _get_secret(self, field: str, item_ref: str) -> str:
        item_ref = str(item_ref or "").strip()
        if not item_ref:
            raise VaultError("credential reference cannot be empty")
        if field not in {"username", "password", "totp"}:
            raise VaultError("unsupported credential field")
        with self._lock:
            self._ensure_session_locked()
            proc = self._run(["get", field, item_ref], require_session=True)
            value = (proc.stdout or "").rstrip("\r\n")
            if not value:
                raise VaultError(f"Vault item has no {field} value")
            return value

    def get_username(self, item_ref: str) -> str:
        return self._get_secret("username", item_ref)

    def get_password(self, item_ref: str) -> str:
        return self._get_secret("password", item_ref)

    def get_totp(self, item_ref: str) -> str:
        return self._get_secret("totp", item_ref)

    def close(self) -> None:
        with self._lock:
            if self.lock_on_exit and self.installed() and self._session:
                try:
                    self._run(["lock"], allow_failure=True)
                except Exception:
                    pass
            self._session = None


VAULT = BitwardenVault()

__all__ = ["BitwardenVault", "SafeVaultItem", "VaultError", "VAULT"]
