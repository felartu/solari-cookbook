#!/usr/bin/env python3
"""Configuration loading and non-secret runtime-state persistence."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Iterable

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MODULE_DIR.parent
DEFAULT_CONFIG_CANDIDATES = (
    PROJECT_DIR / "config" / "toolkit.conf",
    Path.cwd() / "toolkit.conf",
    Path.home() / ".config" / "solari-enterprise-vdi-toolkit" / "config.ini",
    Path("/etc/solari-enterprise-vdi-toolkit.conf"),
)


def _candidate_paths() -> Iterable[Path]:
    explicit = os.getenv("VDI_CONFIG_FILE", "").strip()
    if explicit:
        yield Path(explicit).expanduser()
        return
    seen: set[Path] = set()
    for path in DEFAULT_CONFIG_CANDIDATES:
        path = path.expanduser()
        if path not in seen:
            seen.add(path)
            yield path


def _normalize_key(key: str) -> str:
    return key.strip().upper().replace("-", "_").replace(".", "_")


def _select_main_config() -> Path | None:
    for path in _candidate_paths():
        if path.is_file():
            return path.resolve()
    return None


def _state_path(main_config: Path | None) -> Path | None:
    explicit = os.getenv("VDI_STATE_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if main_config is None:
        return None
    return Path(str(main_config) + ".state")


def _load_ini_missing(path: Path) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open("r", encoding="utf-8") as handle:
        parser.read_file(handle)
    pairs: list[tuple[str, str]] = []
    for key, value in parser.defaults().items():
        pairs.append((_normalize_key(key), str(value).strip()))
    for section in parser.sections():
        for key, value in parser.items(section, raw=True):
            pairs.append((_normalize_key(key), str(value).strip()))
    for key, value in pairs:
        if key and value and key not in os.environ:
            os.environ[key] = value


def load_config_fallback() -> tuple[Path | None, Path | None]:
    main = _select_main_config()
    state = _state_path(main)
    if state is not None and state.is_file():
        _load_ini_missing(state)
    if main is not None:
        _load_ini_missing(main)
        os.environ.setdefault("VDI_CONFIG_FILE_LOADED", str(main))
    if state is not None:
        os.environ.setdefault("VDI_STATE_FILE", str(state))
    return main, state


def persist_runtime_values(values: dict[str, str]) -> Path | None:
    """Persist non-secret lifecycle IDs to the config sidecar."""
    if not values or STATE_FILE is None:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if STATE_FILE.is_file():
        try:
            parser.read(STATE_FILE, encoding="utf-8")
        except configparser.Error:
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
    if not parser.has_section("runtime"):
        parser.add_section("runtime")
    for key, value in values.items():
        if value:
            parser.set("runtime", _normalize_key(key), str(value))
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass
    return STATE_FILE


LOADED_CONFIG_FILE, STATE_FILE = load_config_fallback()
