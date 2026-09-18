#!/usr/bin/env python3
"""Solari Enterprise VDI Toolkit enterprise agent-native coordinate engine.

Toolkit keeps the proven Solari desktop lifecycle, persistent RFB framebuffer, VDI
bridge, keyboard, and post-action verification machinery, but removes host
visual localization from the active interaction path. The chat model itself
selects native framebuffer pixels and calls desktop_click_native or
desktop_double_click_native. Legacy CV/grid functions remain dormant in this
transitional file for rollback comparison and are not exposed or executable
through the model tool surface.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import inspect
import json
import os
import re
import secrets
import struct
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from config import LOADED_CONFIG_FILE, persist_runtime_values
from security_vault import VAULT, VaultError

try:
    # The VDI bridge is intentionally optional so the reference desktop agent
    # remains usable for non-VDI sessions.  When present, its functions are
    # host-owned and never expose PSKs or RDP credentials to the model.
    import vpn_mcp as vdi_bridge
except Exception as _vdi_import_error:  # pragma: no cover - depends on host install
    vdi_bridge = None
    _VDI_IMPORT_ERROR = f"{type(_vdi_import_error).__name__}: {_vdi_import_error}"
else:
    _VDI_IMPORT_ERROR = ""

try:
    import httpx2
except ImportError:  # The published dependency is normally plain httpx.
    import httpx as httpx2
import websockets
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
from openai import AsyncOpenAI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from solari_desktop import DesktopClient
from demo_ui import DemoUI


# ============================================================================
# Configuration
# ============================================================================


def _redact(value: Any) -> Any:
    """Remove configured credentials before values reach logs or model context."""
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    secrets = [
        os.getenv("AI_API_KEY", ""),
        os.getenv("SOLARI_API_KEY", ""),
        os.getenv("VDI_IPSEC_PSK", ""),
        os.getenv("VDI_RDP_PASSWORD", ""),
        os.getenv("BW_CLIENTSECRET", ""),
        os.getenv("BW_SESSION", ""),
        os.getenv("VAULT_MASTER_PASSWORD", ""),
    ]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text.replace("secret = 0s", "secret = <redacted>")


# Required runtime configuration. There is intentionally NO default AI
# provider, endpoint, model, or provider-specific API-key fallback. The caller
# must explicitly choose an OpenAI-compatible provider at launch time.
# Required runtime configuration. There is intentionally NO default AI
# provider, endpoint, model, or provider-specific API-key fallback. The caller
# must explicitly choose an OpenAI-compatible provider at launch time.
SOLARI_API_KEY = os.getenv("SOLARI_API_KEY")
AI_API_KEY = os.getenv("AI_API_KEY")
BASE_URL = os.getenv("AI_BASE_URL")
MODEL = os.getenv("AI_MODEL")

missing_ai_config = [
    name
    for name, value in (
        ("AI_API_KEY", AI_API_KEY),
        ("AI_BASE_URL", BASE_URL),
        ("AI_MODEL", MODEL),
    )
    if not value
]
if missing_ai_config:
    raise RuntimeError(
        "Missing required AI configuration: "
        + ", ".join(missing_ai_config)
        + ". This agent has no default AI provider; set all three explicitly."
    )

if not SOLARI_API_KEY:
    raise RuntimeError("SOLARI_API_KEY environment variable is not set")

# Optional display label only. It never selects or changes the provider.
# When omitted, the CLI uses the generic label "OpenAI-compatible".
AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip()
AI_PROVIDER_LABEL = AI_PROVIDER or "OpenAI-compatible"

# Optional provider/model knobs. They are deliberately opt-in so an endpoint
# that does not implement them never sees them.
AI_REASONING_EFFORT = os.getenv("AI_REASONING_EFFORT", "").strip()
AI_EXTRA_BODY_JSON = os.getenv("AI_EXTRA_BODY_JSON", "").strip()
AI_TOOL_CHOICE = os.getenv("AI_TOOL_CHOICE", "auto").strip() or "auto"
AI_SEND_PARALLEL_TOOL_CALLS = os.getenv("AI_SEND_PARALLEL_TOOL_CALLS", "1").lower() not in {
    "0", "false", "no"
}


def load_ai_extra_body() -> dict[str, Any]:
    if not AI_EXTRA_BODY_JSON:
        return {}
    try:
        parsed = json.loads(AI_EXTRA_BODY_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI_EXTRA_BODY_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("AI_EXTRA_BODY_JSON must decode to a JSON object")
    return parsed


AI_EXTRA_BODY = load_ai_extra_body()

MCP_SERVER = os.getenv(
    "MCP_SERVER",
    "https://mcp.getsolari.com/mcp",
)

SOLARI_API_BASE = os.getenv(
    "SOLARI_API_BASE",
    "https://api.getsolari.com",
)

# Startup desktop policy. If solari_list returns no running desktop, the host
# creates one before the first chat prompt is shown. The metadata label makes
# auto-created desktops easy to identify on later runs.
AUTO_DESKTOP_TEMPLATE = os.getenv("SOLARI_DESKTOP_TEMPLATE", "default")
AUTO_DESKTOP_LABEL = os.getenv("SOLARI_DESKTOP_LABEL", "solari-vdi-toolkit")

# VDI lifecycle/runtime settings.  The attached hardened agent remains the
# interaction engine; these values only select the pre-provisioned VDI image
# and host-owned VPN bridge around it.
VDI_TEMPLATE_ID = os.getenv("VDI_TEMPLATE_ID", os.getenv("SOLARI_DESKTOP_TEMPLATE", "workstation"))
VDI_IMAGE_SNAPSHOT_ID = os.getenv("VDI_IMAGE_SNAPSHOT_ID", "").strip()
VDI_DESKTOP_ID = os.getenv("VDI_DESKTOP_ID", "").strip()
VDI_PREPARE_ON_START = os.getenv("VDI_PREPARE_ON_START", "1").lower() not in {"0", "false", "no"}
VDI_PREFER_SAVED_DESKTOP = os.getenv("VDI_PREFER_SAVED_DESKTOP", "1").lower() not in {"0", "false", "no"}
# If all discovered sessions fail the health/RFB preflight, provision one fresh
# desktop from the configured template instead of ending before the chat loop.
# This never deletes the failed session; set to 0 when an operator wants a hard
# stop rather than consuming another desktop slot.
VDI_CREATE_ON_ATTACH_FAILURE = os.getenv("VDI_CREATE_ON_ATTACH_FAILURE", "1").lower() not in {"0", "false", "no"}
VDI_PERSIST_ENV_FILE = Path(os.getenv("VDI_ENV_FILE", ".env"))

OUTPUT_DIR = Path(
    os.getenv("MCP_OUTPUT_DIR", "mcp_output")
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Agent-loop round budget.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "40"))
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "50000"))
MAX_VISION_MESSAGES_IN_CONTEXT = min(
    int(os.getenv("MAX_VISION_MESSAGES_IN_CONTEXT", "1")),
    1,
)

# Hard cap on actual image_url parts in one model request. The conservative
# default keeps vision requests small across OpenAI-compatible providers. Set
# 0 for unlimited only if your selected provider/model explicitly supports it.
MAX_IMAGES_PER_REQUEST = 1

# Vision snapshots are JPEG-compressed before being sent to the model.
# OCR always runs against the lossless in-memory RGB framebuffer.
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "82"))
SAVE_STREAM_FRAMES = os.getenv("SAVE_STREAM_FRAMES", "1").lower() not in {"0", "false", "no"}

# OCR settings.
OCR_SCALE = int(os.getenv("OCR_SCALE", "4"))
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.72"))
AMBIGUITY_MARGIN = float(os.getenv("AMBIGUITY_MARGIN", "0.025"))

# RFB / framebuffer timing.
RFB_START_TIMEOUT = float(os.getenv("RFB_START_TIMEOUT", "15"))
RFB_RECONNECT_DELAY = float(os.getenv("RFB_RECONNECT_DELAY", "1.0"))
RFB_POST_ACTION_TIMEOUT = float(os.getenv("RFB_POST_ACTION_TIMEOUT", "2.0"))
RFB_STABLE_SECONDS = float(os.getenv("RFB_STABLE_SECONDS", "0.12"))
RFB_SNAPSHOT_TIMEOUT = float(os.getenv("RFB_SNAPSHOT_TIMEOUT", "8.0"))

# A desktop can be attachable while still booting or resuming.  Do not start
# RFB in that window: the Solari gateway may accept the WebSocket and then
# close it with code 1005 before the RFB banner is available.
DESKTOP_HEALTH_TIMEOUT = float(os.getenv("DESKTOP_HEALTH_TIMEOUT", "60"))
DESKTOP_HEALTH_POLL_INTERVAL = float(os.getenv("DESKTOP_HEALTH_POLL_INTERVAL", "1.0"))
RFB_STREAM_ATTEMPTS = max(1, int(os.getenv("RFB_STREAM_ATTEMPTS", "2")))
RFB_STREAM_RETRY_DELAY = max(0.1, float(os.getenv("RFB_STREAM_RETRY_DELAY", "1.0")))
TOOLKIT_FRESH_USER_FRAME = os.getenv("TOOLKIT_FRESH_USER_FRAME", "1").strip().lower() not in {"0", "false", "no", "off"}
TOOLKIT_FRESH_REFRESH_VIEW = os.getenv("TOOLKIT_FRESH_REFRESH_VIEW", "1").strip().lower() not in {"0", "false", "no", "off"}
TOOLKIT_FRESH_FRAME_STRICT = os.getenv("TOOLKIT_FRESH_FRAME_STRICT", "1").strip().lower() not in {"0", "false", "no", "off"}
# Toolkit returns all model keyboard input to the native Solari Desktop keyboard
# channel. xdotool is not used to emit text or key events.
TOOLKIT_KEYBOARD_TRANSPORT = "solari"

# Atomic navigation timing. The host performs Ctrl+L / type / Enter (or Ctrl+T)
# without returning to the model between those low-level steps.
NAVIGATION_INTERSTEP_DELAY = float(os.getenv("NAVIGATION_INTERSTEP_DELAY", "0.12"))
NAVIGATION_TYPE_SETTLE_DELAY = float(os.getenv("NAVIGATION_TYPE_SETTLE_DELAY", "0.35"))
NAVIGATION_TIMEOUT = float(os.getenv("NAVIGATION_TIMEOUT", "8.0"))
NAVIGATION_STABLE_SECONDS = float(os.getenv("NAVIGATION_STABLE_SECONDS", "0.40"))
NAVIGATION_RETRIES = int(os.getenv("NAVIGATION_RETRIES", "2"))
NAVIGATION_ENTER_RETRIES = int(os.getenv("NAVIGATION_ENTER_RETRIES", "1"))
NAVIGATION_POLL_SLICE = float(os.getenv("NAVIGATION_POLL_SLICE", "0.80"))
NAVIGATION_MIN_CHANGED_FRACTION = float(os.getenv("NAVIGATION_MIN_CHANGED_FRACTION", "0.02"))

# Reject fragile one-character OCR labels such as a stray digit inside an input.
OCR_MIN_TARGET_ALNUM_CHARS = int(os.getenv("OCR_MIN_TARGET_ALNUM_CHARS", "2"))

# A double-click is emitted as two clicks at the same host-resolved coordinate.
# Keep the interval configurable for desktops with slower event processing, but
# bound it so the operation remains a real double-click rather than two
# unrelated clicks.  The model never sees this timing or native coordinates.
DOUBLE_CLICK_INTERVAL_SECONDS = min(
    max(
        float(
            os.getenv("DOUBLE_CLICK_INTERVAL_MS", "140")
        )
        / 1000.0,
        0.05,
    ),
    0.50,
)

# Precision visual inspection for exact values. The host crops/enlarges only;
# the vision model reads the value from the enlarged crop.
PRECISION_CROP_SCALE = float(os.getenv("PRECISION_CROP_SCALE", "3.0"))
PRECISION_CROP_JPEG_QUALITY = int(os.getenv("PRECISION_CROP_JPEG_QUALITY", "92"))

# Visual point-click stale-frame validation. A point chosen from an older model-
# presented frame is accepted only if the local neighborhood around that point
# is still substantially unchanged in the current framebuffer.
POINT_STALE_REGION_RADIUS = int(os.getenv("POINT_STALE_REGION_RADIUS", "72"))
POINT_STALE_PIXEL_THRESHOLD = int(os.getenv("POINT_STALE_PIXEL_THRESHOLD", "18"))

# Visible grid navigation. The model never emits native or normalized pixels.
FULL_GRID_COLS = int(os.getenv("FULL_GRID_COLS", "8"))
FULL_GRID_ROWS = int(os.getenv("FULL_GRID_ROWS", "6"))
ZOOM_GRID_COLS = int(os.getenv("ZOOM_GRID_COLS", "5"))
ZOOM_GRID_ROWS = int(os.getenv("ZOOM_GRID_ROWS", "5"))
GRID_ZOOM_SCALE = int(os.getenv("GRID_ZOOM_SCALE", "3"))
ZOOM_CONTEXT_LIMIT = int(os.getenv("ZOOM_CONTEXT_LIMIT", "16"))
GRID_STALE_CHANGED_FRACTION = float(os.getenv("GRID_STALE_CHANGED_FRACTION", "0.10"))
GRID_STALE_MEAN_DIFF = float(os.getenv("GRID_STALE_MEAN_DIFF", "22.0"))

if FULL_GRID_COLS < 2 or FULL_GRID_ROWS < 2 or ZOOM_GRID_COLS < 2 or ZOOM_GRID_ROWS < 2:
    raise RuntimeError("Grid dimensions must be at least 2x2")
if FULL_GRID_ROWS > 26 or ZOOM_GRID_ROWS > 26:
    raise RuntimeError("Grid row counts cannot exceed 26 because rows use A-Z labels")
POINT_STALE_CHANGED_FRACTION = float(os.getenv("POINT_STALE_CHANGED_FRACTION", "0.08"))
POINT_STALE_MEAN_DIFF = float(os.getenv("POINT_STALE_MEAN_DIFF", "10.0"))

# AI-provider/model-response watchdogs. These are intentionally separate from
# the Solari RFB/WebSocket health checks. Empty SSE keepalive/metadata chunks do
# NOT reset the meaningful-output deadlines. Provider reasoning deltas count as
# activity for watchdog purposes but are never printed or persisted.
MODEL_HTTP_TIMEOUT = float(os.getenv("MODEL_HTTP_TIMEOUT", "300"))
MODEL_FIRST_OUTPUT_TIMEOUT = float(os.getenv("MODEL_FIRST_OUTPUT_TIMEOUT", "90"))
MODEL_INTER_OUTPUT_TIMEOUT = float(os.getenv("MODEL_INTER_OUTPUT_TIMEOUT", "60"))
MODEL_STREAM_TOTAL_TIMEOUT = float(os.getenv("MODEL_STREAM_TOTAL_TIMEOUT", "240"))
MODEL_STREAM_RETRIES = int(os.getenv("MODEL_STREAM_RETRIES", "2"))

# Transient OpenAI-compatible provider failures are separate from model-stream
# watchdog stalls. 429 / 5xx / connection/timeouts are retried with bounded
# exponential backoff. These retries never alter or recreate the Solari desktop.
MODEL_TRANSIENT_RETRIES = int(os.getenv("MODEL_TRANSIENT_RETRIES", "3"))
MODEL_TRANSIENT_RETRY_BASE_DELAY = float(os.getenv("MODEL_TRANSIENT_RETRY_BASE_DELAY", "1.0"))
MODEL_TRANSIENT_RETRY_MAX_DELAY = float(os.getenv("MODEL_TRANSIENT_RETRY_MAX_DELAY", "8.0"))

# Show ordinary model progress commentary during persistent goals. This is not
# private chain-of-thought; it is the assistant's normal streamed user-visible text.
SHOW_GOAL_PROGRESS = os.getenv("SHOW_GOAL_PROGRESS", "1").lower() not in {"0", "false", "no"}

# Agent-loop budgets. A desktop task should not need an unbounded sequence of
# shell/code or refresh calls.
MAX_TOTAL_TOOL_CALLS_PER_TURN = int(os.getenv("MAX_TOTAL_TOOL_CALLS_PER_TURN", "36"))
MAX_BLOCKING_TOOL_CALLS_PER_TURN = int(os.getenv("MAX_BLOCKING_TOOL_CALLS_PER_TURN", "4"))
MAX_REFRESH_CALLS_PER_TURN = int(os.getenv("MAX_REFRESH_CALLS_PER_TURN", "8"))

# Persistent-goal turns need more room than simple ACTION turns.  The limits are
# deliberately split into a soft warning and a hard safety ceiling.  Refreshes
# are tracked separately and DO NOT consume the environmental-action budget.
# A denied individual tool call never, by itself, marks the entire goal BLOCKED.
MAX_GOAL_TOOL_CALLS_PER_TURN = int(os.getenv("MAX_GOAL_TOOL_CALLS_PER_TURN", "120"))
GOAL_ACTION_WARNING_AT = int(os.getenv("GOAL_ACTION_WARNING_AT", "80"))
MAX_GOAL_TOOL_ROUNDS = int(os.getenv("MAX_GOAL_TOOL_ROUNDS", "100"))
MAX_GOAL_REFRESH_CALLS_PER_TURN = int(os.getenv("MAX_GOAL_REFRESH_CALLS_PER_TURN", "32"))
MAX_GOAL_BUDGET_RESOLUTION_ROUNDS = int(os.getenv("MAX_GOAL_BUDGET_RESOLUTION_ROUNDS", "2"))
MAX_PREMATURE_GOAL_FINALS = int(os.getenv("MAX_PREMATURE_GOAL_FINALS", "5"))

# Observation tools are cheap and do not change the desktop, but an LLM can
# still loop forever by repeatedly asking for a zoom/full screenshot. The host
# permits a few retries, then requires a meaningful action or a final answer.
MAX_REPEATED_OBSERVATION_CALLS = int(os.getenv("MAX_REPEATED_OBSERVATION_CALLS", "3"))
MAX_OBSERVATION_ONLY_ROUNDS = int(os.getenv("MAX_OBSERVATION_ONLY_ROUNDS", "12"))

# Toolkit semantic task-state and action-loop controls. These are deliberately
# independent of framebuffer version: a semantic loop can change pixels every
# time while still making no task progress.
TOOLKIT_GOAL_MEMORY_MAX_FACTS = max(4, int(os.getenv("TOOLKIT_GOAL_MEMORY_MAX_FACTS", "24")))
TOOLKIT_ACTION_HISTORY_WINDOW = max(4, int(os.getenv("TOOLKIT_ACTION_HISTORY_WINDOW", "12")))
TOOLKIT_SAME_TARGET_REPEAT_LIMIT = max(2, int(os.getenv("TOOLKIT_SAME_TARGET_REPEAT_LIMIT", "2")))
TOOLKIT_SEMANTIC_CYCLE_REPEATS = max(2, int(os.getenv("TOOLKIT_SEMANTIC_CYCLE_REPEATS", "2")))
TOOLKIT_TARGET_GROUNDING_STRICT = os.getenv("TOOLKIT_TARGET_GROUNDING_STRICT", "1").strip().lower() not in {"0", "false", "no", "off"}
TOOLKIT_TASKBAR_PROBE_FAMILY_LIMIT = max(2, int(os.getenv("TOOLKIT_TASKBAR_PROBE_FAMILY_LIMIT", "5")))
TOOLKIT_TASKBAR_X_BUCKET_PX = max(8, int(os.getenv("TOOLKIT_TASKBAR_X_BUCKET_PX", "32")))
TOOLKIT_HOTKEY_REPEAT_LIMIT = max(1, int(os.getenv("TOOLKIT_HOTKEY_REPEAT_LIMIT", "2")))
# Literal non-secret text uses Solari Desktop keyboard.type().
TOOLKIT_TEXT_INPUT_TRANSPORT = "solari"
# After this many identical no-visible-change attempts into the same semantic field,
# the host refuses further blind retries for the rest of the human turn.
TOOLKIT_TEXT_NOCHANGE_LIMIT = max(1, int(os.getenv("TOOLKIT_TEXT_NOCHANGE_LIMIT", "2")))

# This is a desktop-first CLI. Shell/code tools are disabled by default because
# they are a common source of agent loops for GUI tasks. Set ALLOW_SHELL_TOOLS=1
# if you explicitly want the model to run shell/code commands.
ALLOW_SHELL_TOOLS = os.getenv("ALLOW_SHELL_TOOLS", "0").lower() in {"1", "true", "yes"}
SHELL_TOOLS = {"solari_exec", "solari_run_command_bg", "solari_run_code"}
BLOCKING_TOOLS = {"solari_exec", "solari_run_code", "solari_run_command_bg"}

OBSERVATION_ONLY_TOOLS = {
    "desktop_refresh_view",
    "desktop_zoom_region",
    "desktop_inspect_cell",
    "vdi_vpn_status",
    "vdi_desktop_state",
    "security_vault_status",
    "security_vault_search",
    "solari_screenshot",
}


def is_observation_only_tool(tool_name: str) -> bool:
    return tool_name in OBSERVATION_ONLY_TOOLS

# Session/VM lifecycle is exclusively host-owned. These tools are hidden from
# the model AND denied again at execution time as defense in depth.
HOST_OWNED_LIFECYCLE_TOOLS = {
    "solari_list",
    "solari_connect",
    "solari_desktop_create",
    "solari_sandbox_create",
    "solari_kill",
}

# If your click coordinate space is known to differ from the RFB framebuffer,
# set these. Normally leave both at 0 so framebuffer pixels == click pixels.
DESKTOP_WIDTH = int(os.getenv("DESKTOP_WIDTH", "0"))
DESKTOP_HEIGHT = int(os.getenv("DESKTOP_HEIGHT", "0"))

# Model-visible visual coordinates are normalized to a provider-independent
# square space. The model never emits framebuffer/native desktop pixels.
# (0, 0) is the exact top-left of the attached full-frame image and
# (VISUAL_COORD_MAX, VISUAL_COORD_MAX) is the exact bottom-right.
VISUAL_COORD_MAX = int(os.getenv("VISUAL_COORD_MAX", "1000"))
if VISUAL_COORD_MAX <= 0:
    raise RuntimeError("VISUAL_COORD_MAX must be greater than zero")

# Keep a few exact full-frame snapshots prepared for model vision so point-click
# debugging can refer to the same frame version the model localized against.
VISUAL_FRAME_HISTORY_SIZE = int(os.getenv("VISUAL_FRAME_HISTORY_SIZE", "6"))

RAW_CLICK_TOOL = "solari_click"
SCREENSHOT_TOOL = "solari_screenshot"

openai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=BASE_URL, timeout=MODEL_HTTP_TIMEOUT)
# Toolkit deliberately does not instantiate any host visual grounder or secondary
# vision locator. The chat model itself chooses native framebuffer coordinates.
VISUAL_GROUNDER = None
DIRECT_LOCATOR = None
NATIVE_CURSOR_VERIFY = os.getenv("TOOLKIT_CURSOR_VERIFY", os.getenv("NATIVE_CURSOR_VERIFY", "1")).strip().lower() not in {"0", "false", "no", "off"}
NATIVE_CURSOR_TOLERANCE = max(0, int(os.getenv("TOOLKIT_CURSOR_TOLERANCE", os.getenv("NATIVE_CURSOR_TOLERANCE", "1"))))
DIRECT_ALLOW_FALLBACK = os.getenv("DIRECT_ALLOW_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}

# Toolkit host-owned credential vault. Secrets are fetched by the host and typed
# directly into the Solari desktop. They are never returned as tool content.
VAULT_ENABLED = os.getenv("VAULT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
VAULT_REDACT_HALF_HEIGHT = max(40, int(os.getenv("VAULT_REDACT_HALF_HEIGHT", "90")))
VAULT_REDACT_SECONDS = max(30.0, float(os.getenv("VAULT_REDACT_SECONDS", "120")))

DEMO_UI = DemoUI(root=Path(__file__).resolve().parent, model=MODEL)


# ============================================================================
# Data types
# ============================================================================

@dataclass(frozen=True)
class DesktopCandidateInfo:
    session_id: str
    label: str | None = None
    state: str | None = None


@dataclass
class FrameSnapshot:
    image: Image.Image
    version: int
    updated_at: float


@dataclass
class VisionImage:
    label: str
    mime_type: str
    base64_data: str
    saved_path: str | None
    frame_version: int
    width: int
    height: int
    is_full_frame: bool = True
    source_bbox: tuple[int, int, int, int] | None = None
    source_frame_width: int | None = None
    source_frame_height: int | None = None
    grid_kind: str | None = None
    grid_cols: int | None = None
    grid_rows: int | None = None
    zoom_id: str | None = None
    sensitive_redacted: bool = False


@dataclass
class SensitiveRedactionBand:
    y0: int
    y1: int
    expires_at: float
    reason: str


SENSITIVE_REDACTIONS: list[SensitiveRedactionBand] = []


@dataclass
class ZoomContext:
    zoom_id: str
    frame_version: int
    source_bbox: tuple[int, int, int, int]
    depth: int
    created_at: float


@dataclass
class GridFallbackAuthorization:
    token: str
    target_key: str
    frame_version: int
    session_id: str
    created_at: float


@dataclass
class OCRCandidate:
    text: str
    left: int
    top: int
    width: int
    height: int
    text_score: float
    ocr_confidence: float
    final_score: float

    @property
    def center_x(self) -> int:
        return self.left + self.width // 2

    @property
    def center_y(self) -> int:
        return self.top + self.height // 2


@dataclass
class GoalMemoryFact:
    key: str
    value: str
    evidence: str
    source: str
    frame_version: int | None = None
    completed: bool = False
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class SemanticActionRecord:
    tool: str
    target: str
    target_key: str
    family_key: str | None = None
    outcome: str = "attempted"
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class TurnState:
    """Host-owned state for one human turn.

    The original user request is always authoritative. The model may declare a
    compact persistent goal and success condition, but it cannot replace the
    user's request.
    """

    user_request: str
    mode: str = "UNDECIDED"  # UNDECIDED | CHAT | ACTION | GOAL
    objective: str | None = None
    success_condition: str | None = None
    target: str | None = None
    status: str = "NONE"  # NONE | IN_PROGRESS | SUCCESS | NOT_FOUND | BLOCKED
    result: str | None = None
    evidence: str | None = None
    premature_finishes: int = 0
    host_action_budget_exhausted: bool = False
    budget_warning_sent: bool = False
    host_blocked: bool = False
    host_block_reason: str | None = None
    precision_inspections: int = 0
    precision_last_frame: int | None = None
    precision_last_target: str | None = None
    # Durable semantic memory survives framebuffer/image compaction for the
    # lifetime of this user turn. The host re-injects it into every GOAL round.
    semantic_memory: dict[str, GoalMemoryFact] = field(default_factory=dict)
    action_history: list[SemanticActionRecord] = field(default_factory=list)
    semantic_loop_rejections: int = 0
    # Keyed by scope + semantic field target + hash(text). Raw text is not stored
    # here; this guard exists only to stop repeated no-effect typing loops.
    text_nochange_counts: dict[str, int] = field(default_factory=dict)

    @property
    def goal_active(self) -> bool:
        return self.mode == "GOAL" and self.status == "IN_PROGRESS"

    @property
    def goal_terminal(self) -> bool:
        return self.mode == "GOAL" and self.status in {"SUCCESS", "NOT_FOUND", "BLOCKED"}


# ============================================================================
# Global desktop state
# ============================================================================

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {}
ACTIVE_DESKTOP_SESSION_ID: str | None = None
FRAME_CACHE: "RFBFrameCache | None" = None
HOST_DESKTOP_CONTROL: Any | None = None

# Exact full-frame snapshots prepared for model vision, keyed by RFB frame
# version. Values are intentionally untyped here because FrameSnapshot is
# declared below this section.
PRESENTED_FRAMES: dict[int, Any] = {}
ZOOM_CONTEXTS: dict[str, ZoomContext] = {}
SEMANTIC_GROUNDING_CACHE: dict[str, tuple[Any, GroundingResult, float]] = {}
GRID_FALLBACK_AUTHORIZATIONS: dict[str, GridFallbackAuthorization] = {}
GRID_FALLBACK_MAX_AGE = float(os.getenv("GRID_FALLBACK_MAX_AGE", "120"))


def _invalidate_semantic_grounding_cache(reason: str) -> None:
    if SEMANTIC_GROUNDING_CACHE:
        count = len(SEMANTIC_GROUNDING_CACHE)
        SEMANTIC_GROUNDING_CACHE.clear()
        print(f"[toolkit-ground] invalidated semantic cache entries={count} reason={reason}", flush=True)


def _invalidate_grid_fallbacks(reason: str) -> None:
    if GRID_FALLBACK_AUTHORIZATIONS:
        count = len(GRID_FALLBACK_AUTHORIZATIONS)
        GRID_FALLBACK_AUTHORIZATIONS.clear()
        print(f"[toolkit-grid] invalidated fallback authorizations={count} reason={reason}", flush=True)


def _issue_grid_fallback_authorization(target: str, frame_version: int) -> str:
    if not ACTIVE_DESKTOP_SESSION_ID:
        raise RuntimeError("No active desktop session for grid fallback authorization")
    now = time.monotonic()
    # Opportunistic expiry cleanup.
    for token, auth in list(GRID_FALLBACK_AUTHORIZATIONS.items()):
        if GRID_FALLBACK_MAX_AGE > 0 and now - auth.created_at > GRID_FALLBACK_MAX_AGE:
            GRID_FALLBACK_AUTHORIZATIONS.pop(token, None)
    token = secrets.token_urlsafe(16)
    GRID_FALLBACK_AUTHORIZATIONS[token] = GridFallbackAuthorization(
        token=token,
        target_key=normalize_text(target),
        frame_version=int(frame_version),
        session_id=ACTIVE_DESKTOP_SESSION_ID,
        created_at=now,
    )
    print(
        f"[toolkit-grid] issued fallback authorization frame=v{frame_version} target={target!r}",
        flush=True,
    )
    return token


def _validate_grid_fallback_authorization(
    token: str | None,
    target: str,
    frame_version: int,
) -> tuple[bool, str]:
    if not token:
        return False, "Legacy grid is unavailable until semantic grounding fails and returns fallback_token."
    auth = GRID_FALLBACK_AUTHORIZATIONS.get(str(token))
    if auth is None:
        return False, "Unknown or expired fallback_token; retry semantic grounding on the current frame."
    if not ACTIVE_DESKTOP_SESSION_ID or auth.session_id != ACTIVE_DESKTOP_SESSION_ID:
        return False, "fallback_token belongs to a different desktop session."
    if GRID_FALLBACK_MAX_AGE > 0 and time.monotonic() - auth.created_at > GRID_FALLBACK_MAX_AGE:
        GRID_FALLBACK_AUTHORIZATIONS.pop(str(token), None)
        return False, "fallback_token expired; retry semantic grounding."
    if auth.target_key != normalize_text(target):
        return False, "fallback_token is bound to a different semantic target."
    if auth.frame_version != int(frame_version):
        return False, f"fallback_token is bound to frame v{auth.frame_version}, not v{frame_version}."
    return True, ""


# ============================================================================
# System prompt and virtual tools
# ============================================================================

messages: list[dict[str, Any]] = [
    {
        "role": "system",
        "content": (
            "You are a concise AI assistant controlling one already-running Solari desktop through its real GUI. "
            "Toolkit AGENT-NATIVE COORDINATE MODE: every clean desktop image is the exact raw RFB framebuffer. "
            "The host tells you the exact framebuffer version and native width/height beside each image. "
            "YOU own visual localization. Inspect the attached image yourself and choose native framebuffer pixels directly. "
            "For a click, call desktop_click_native with target, frame_version, x_px, and y_px. "
            "For a double-click, call desktop_double_click_native once with the same native coordinate contract. "
            "Coordinates are native pixels in the attached framebuffer: origin (0,0) is the exact top-left pixel, x increases right, y increases down. "
            "Never normalize to 0..1000, never invent another image size, and never ask the host to locate a target for you. "
            "Choose a point visibly inside the intended clickable element, not blank space, a surrounding container, or merely nearby text. "
            "The host performs NO OCR/CV/semantic localization for these native click tools. It only validates frame freshness, bounds, Solari display size, cursor echo, and sends input. "
            "Do not call solari_list, solari_connect, solari_screenshot, solari_click, solari_key, solari_type, or application-launch shortcuts directly; those low-level primitives are host-owned. "
            "After every click, double-click, key, or typing action, inspect the attached post-action framebuffer and verify the intended visible effect. A framebuffer change alone is not proof of semantic success. "
            "Toolkit VISUAL-IDENTITY CONTRACT: for every native click/double-click, provide ui_scope plus evidence_basis and visual_evidence from the SAME frame. visual_evidence must describe what is actually visible at the chosen point. "
            "Toolkit POINTER-SCOPE CONTRACT: ui_scope=local means Linux/XFCE/Remmina chrome (Applications menu, Remmina profile list, winserver Remmina titlebar). ui_scope=remote_rdp means controls rendered inside the Windows session (Windows desktop/taskbar, Paint, CPA2000, Notepad inside Windows). Do not mix these layers. "
            "Do not assign a hidden application identity to an unlabeled taskbar/toolbar icon. For example, if the frame does not visibly show the text CPA2000 on or next to an icon, you may describe it only by its visible appearance/position, not call it the CPA2000 icon. Prefer a visible labeled desktop shortcut/menu entry when identity matters. "
            "Preserve the user's target constraints. Do not invent colors, ordering, locations, button meanings, application-specific shortcuts, connection states, or causal explanations that are not supported by current visible evidence or explicit host metadata. "
            "Use desktop_hotkey only for host-approved generic shortcuts or keys explicitly requested by the human. Use desktop_type_text for literal text. "
            "Toolkit INPUT-SCOPE CONTRACT: every desktop_hotkey call declares scope=local or scope=remote_rdp. local means the Linux/Solari desktop and may invoke XFCE window-manager shortcuts. remote_rdp means the keystroke is intended for the focused Windows session inside the visible Remmina winserver window. "
            "Remote Alt+Tab remains disabled until it is separately re-qualified with the restored Solari keyboard transport; use the visible remote Windows taskbar for application switching. Never use local Alt+Tab to switch Windows applications. To switch an already-running remote Windows app, use the visible REMOTE Windows taskbar instead. "
            "For an unlabeled remote taskbar button, perform a bounded spatial probe: describe it only by visible position/appearance, set evidence_basis=spatial_control, click it once, then identify the resulting window from visible title/content. Never rename the same taskbar slot repeatedly to evade a rejected click or loop guard. "
            "KEYBOARD AVAILABILITY INVARIANT: when an active desktop is attached, desktop_type_text and desktop_hotkey are the supported keyboard input tools. "
            "Toolkit TEXT-SCOPE CONTRACT: every desktop_type_text call must provide target plus scope=local or scope=remote_rdp. target names the currently focused semantic field/control. Literal text is emitted through the native Solari Desktop keyboard channel; for remote_rdp the host first verifies that the active local X11 window is winserver. "
            "If desktop_type_text reports no_visible_change, do not blindly repeat the exact same text into the same field. Re-check focus or use a different progress-making action; Toolkit hard-stops the third identical no-change attempt by default. "
            "Never claim that keyboard input is unavailable merely because an obsolete or invented tool name failed. Never launch or use the Windows On-Screen Keyboard as a substitute for normal text/key entry. "
            "If you ever think of vdi_type_text, desktop_type, or desktop_key_native, use desktop_type_text for literal text and desktop_hotkey for keys/chords instead. "
            "Use desktop_refresh_view only when a fresh visual inspection is genuinely needed. The host automatically injects the active desktop session; NEVER provide, guess, ask for, or search for a session ID. "
            "TURN MODES: normal conversation/questions are CHAT. A single bounded desktop operation is ACTION. A multi-step or outcome-oriented request requiring several desktop operations is a persistent GOAL. For a GOAL, your FIRST tool call MUST be start_goal. "
            "The original human request remains authoritative. While a GOAL is active, keep working toward that exact objective and resolve it through finish_goal with SUCCESS, NOT_FOUND, or host-authorized BLOCKED. "
            "Toolkit DURABLE GOAL MEMORY: when you read instructions, extract invoice/account values, verify a workflow step, or learn any fact needed after leaving the current screen, call goal_remember BEFORE navigating away. Store the actual useful content, not merely 'read it'. Facts saved with goal_remember are host-persisted for this goal and are re-injected every round even after older images are compacted. "
            "If durable goal memory already contains a complete fact/checkpoint, do NOT reopen the same source merely because its old screenshot is no longer attached. Reopen only if the stored fact is explicitly incomplete, contradicted by newer evidence, or the user asks for re-verification. "
            "DESKTOP-EVIDENCE RULE: conclusions about desktop state must come from what you actually observed on the attached frames or explicit host status tools. "
            "Toolkit VISUAL-EPOCH CONTRACT: only the newest host-provided framebuffer in the active request is authoritative for CURRENT GUI state. Older framebuffer pixels, geometry text, and assistant screen-state narration are superseded and must not be used to infer what is currently visible, focused, maximized, selected, or open. Durable goal memory remains authoritative only for facts/checkpoints explicitly saved with goal_remember."
        ),
    }
]


messages[0]["content"] += (
    f" VDI INFRASTRUCTURE: the selected enterprise VPN provider is {os.getenv('VPN_PROVIDER', 'userswan')}. "
    "The host-owned VDI bridge exposes the protected RDP target through a configured loopback relay. "
    "Never ask for, print, or type VPN private keys/PSKs or the RDP password. Use vdi_vpn_status first and "
    "vdi_vpn_connect only when the status is not ready. To open RDP, operate the real GUI: "
    "Applications > Internet > Remmina, then use the saved RDP profile that is visibly present. "
    "Its credentials and localhost endpoint are already stored. Do not assume a profile name that is not visible, "
    "and do not call a hidden Remmina launch shortcut or create/kill a Solari desktop. "
    "vdi_desktop_state is a redacted diagnostic tool. "
)

messages[0]["content"] += (
    " Toolkit SECURITY VAULT: credentials may live in a host-owned self-hosted Vaultwarden/Bitwarden vault. "
    "Never ask the human to paste a password, TOTP seed, or OTP into chat when a matching vault item is available. "
    "Use security_vault_search to identify safe item names/ids, then security_vault_type_username/password/totp with the exact visible input-field coordinates. "
    "These secure typing tools fetch and type secrets entirely host-side; secret values are never returned to you or sent to the AI provider. "
    "After secure typing, model-visible screenshots may contain a black horizontal security-redaction band. Do not infer or click inside that band. "
    "For a visible OTP field, prefer security_vault_type_totp with submit=true so retrieval, typing, and Enter are atomic. "
)

messages[0]["content"] += (
    " Toolkit OBSERVATION POLICY: do not repeatedly refresh an unchanged screen. After inspecting a frame, "
    "either act using native coordinates, use a keyboard action, or explain what is verified. "
)

DESKTOP_CLICK_NATIVE_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_click_native",
        "description": (
            "Click one native pixel that YOU visually choose in the exact clean framebuffer attached to the conversation. "
            "Use the frame_version and native x_px/y_px from that exact framebuffer. The host performs no OCR, CV, "
            "semantic grounding, bbox refinement, grid mapping, or secondary vision-model call. It only validates frame/bounds, "
            "maps to Solari's reported display size, verifies cursor echo, and sends the click."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Semantic identity of the intended control. It must be justified by visual_evidence from this same frame; do not name an unlabeled icon as a specific application."},
                "ui_scope": {"type": "string", "enum": ["local", "remote_rdp"], "description": "UI layer containing the target: local Linux/Remmina chrome, or the remote Windows session rendered inside Remmina."},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Exact framebuffer version shown beside the image you used."},
                "x_px": {"type": "integer", "minimum": 0, "description": "Native x pixel in the exact attached framebuffer; origin is top-left."},
                "y_px": {"type": "integer", "minimum": 0, "description": "Native y pixel in the exact attached framebuffer; origin is top-left."},
                "evidence_basis": {"type": "string", "enum": ["visible_text", "visible_icon", "spatial_control"], "description": "How the target identity is grounded in the current frame."},
                "visual_evidence": {"type": "string", "description": "Concrete visible evidence from the same frame, e.g. exact visible label text or a non-inferred icon/control description. Do not claim hidden application identity."},
            },
            "required": ["target", "ui_scope", "frame_version", "x_px", "y_px", "evidence_basis", "visual_evidence"],
        },
    },
}

DESKTOP_DOUBLE_CLICK_NATIVE_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_double_click_native",
        "description": (
            "Double-click one native framebuffer pixel that YOU visually choose. The host sends one atomic Solari double-click "
            "after frame/bounds/display/cursor checks. No host visual localization or secondary model call occurs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Semantic identity of the intended control, justified by visual_evidence from this same frame."},
                "ui_scope": {"type": "string", "enum": ["local", "remote_rdp"], "description": "UI layer containing the target: local Linux/Remmina chrome, or the remote Windows session rendered inside Remmina."},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Exact framebuffer version shown beside the image you used."},
                "x_px": {"type": "integer", "minimum": 0, "description": "Native x pixel in the exact attached framebuffer."},
                "y_px": {"type": "integer", "minimum": 0, "description": "Native y pixel in the exact attached framebuffer."},
                "evidence_basis": {"type": "string", "enum": ["visible_text", "visible_icon", "spatial_control"], "description": "How the target identity is grounded in the current frame."},
                "visual_evidence": {"type": "string", "description": "Concrete visible evidence from the same frame. An unlabeled icon must be described visually rather than assigned a hidden app identity."},
            },
            "required": ["target", "ui_scope", "frame_version", "x_px", "y_px", "evidence_basis", "visual_evidence"],
        },
    },
}

DESKTOP_CLICK_TARGET_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_click_target",
        "description": (
            "Locate and click any visible desktop target using native-pixel vision. The host sends the exact raw RFB framebuffer "
            "and its real width/height to the configured vision model, which returns one pixel coordinate in that image. "
            "The host then validates frame freshness, queries Solari display.size(), verifies cursor placement, and clicks. "
            "Describe only what you intend to click; do not provide coordinates yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Semantic description of the visible target, for example OK button, red Remmina connect icon, R01-C08, or fourth Chrome tab."},
                "region": {
                    "type": "string",
                    "enum": ["full", "top", "bottom", "left", "right", "center"],
                    "description": "Optional approximate area when the same text appears more than once."
                },
            },
            "required": ["target"],
        },
    },
}

DESKTOP_DOUBLE_CLICK_TARGET_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_double_click_target",
        "description": (
            "Locate any visible desktop target with native-pixel vision and double-click the returned point atomically. "
            "The exact raw RFB dimensions are supplied to the vision model and Solari display dimensions are queried before input. "
            "Do not call two independent click tools and do not provide coordinates yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Exact visible text, for example Windows Server via Enterprise VPN or Home."},
                "region": {
                    "type": "string",
                    "enum": ["full", "top", "bottom", "left", "right", "center"],
                    "description": "Optional approximate area when the same text appears more than once."
                },
            },
            "required": ["target"],
        },
    },
}

DESKTOP_CLICK_GRID_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_click_grid",
        "description": (
            "HOST-GATED COMPATIBILITY FALLBACK ONLY after semantic grounding is unresolved. Requires fallback_token. Click a visual control by choosing a VISIBLE grid cell. For a full desktop image, use its coarse "
            "cell plus frame_version. For a zoom image, provide zoom_id and choose a cell from that zoom's finer grid. "
            "Do not use this for controls identifiable by visible text; use desktop_click_target instead. The host may "
            "automatically reroute obvious labeled targets such as 'OK button' to OCR."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Short description of the intended visual control."},
                "cell": {"type": "string", "description": "Visible grid cell such as B7 or D3."},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Required for a full-frame grid click."},
                "zoom_id": {"type": "string", "description": "Provide this when clicking inside a zoomed grid."},
                "fallback_token": {"type": "string", "description": "Required host-issued token from the failed semantic target call for this same target."},
                "position": {
                    "type": "string",
                    "enum": ["center", "top-left", "top", "top-right", "left", "right", "bottom-left", "bottom", "bottom-right"],
                    "description": "Optional non-numeric placement inside the selected cell. Use center by default; zoom for tiny targets."
                },
            },
            "required": ["target", "cell", "fallback_token"],
        },
    },
}

DESKTOP_DOUBLE_CLICK_GRID_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_double_click_grid",
        "description": (
            "HOST-GATED COMPATIBILITY FALLBACK ONLY after semantic grounding is unresolved. Requires fallback_token. Double-click a visual control by choosing a VISIBLE grid cell. For a full desktop image, "
            "use its coarse cell plus frame_version. For a zoom image, provide zoom_id and choose a cell from that "
            "zoom's finer grid. Use desktop_double_click_target for visible text. The host resolves one location and "
            "emits two clicks with a controlled interval, then returns a post-action frame for verification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Short description of the intended visual control."},
                "cell": {"type": "string", "description": "Visible grid cell such as B7 or D3."},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Required for a full-frame grid double-click."},
                "zoom_id": {"type": "string", "description": "Provide this when double-clicking inside a zoomed grid."},
                "fallback_token": {"type": "string", "description": "Required host-issued token from the failed semantic target call for this same target."},
                "position": {
                    "type": "string",
                    "enum": ["center", "top-left", "top", "top-right", "left", "right", "bottom-left", "bottom", "bottom-right"],
                    "description": "Optional non-numeric placement inside the selected cell. Use center by default; zoom for tiny targets."
                },
            },
            "required": ["target", "cell", "fallback_token"],
        },
    },
}

DESKTOP_ZOOM_REGION_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_zoom_region",
        "description": (
            "HOST-GATED COMPATIBILITY FALLBACK: enlarge one visible grid cell and overlay a finer grid for precise visual localization. Requires fallback_token from a failed semantic target call. From a full desktop image, "
            "provide cell + frame_version. To zoom deeper inside an existing zoom, provide source_zoom_id + a fine-grid cell. "
            "The returned zoom_id can be used with desktop_click_grid, desktop_zoom_region again, or desktop_inspect_cell."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "What visual element you are trying to localize."},
                "cell": {"type": "string", "description": "Cell to enlarge, e.g. H2 or C4."},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Required when zooming from a full-frame grid."},
                "source_zoom_id": {"type": "string", "description": "Use instead of frame_version to zoom inside an existing zoom."},
                "fallback_token": {"type": "string", "description": "Required host-issued token from the failed semantic target call for this same target."},
            },
            "required": ["target", "cell", "fallback_token"],
        },
    },
}

DESKTOP_INSPECT_CELL_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_inspect_cell",
        "description": (
            "Return a CLEAN enlarged crop (no grid overlay) of one coarse or fine grid cell for reading exact text/numbers. "
            "Use this for prices, currency markers, serials, hostnames, IPs, ports, and error codes. For a full frame provide "
            "cell + frame_version; for a zoom provide zoom_id + cell."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Exact value/card/region being inspected."},
                "cell": {"type": "string", "description": "Visible grid cell to inspect."},
                "frame_version": {"type": "integer", "minimum": 0},
                "zoom_id": {"type": "string"},
            },
            "required": ["target", "cell"],
        },
    },
}

DESKTOP_HOTKEY_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_hotkey",
        "description": (
            "Press a real desktop key/chord with an explicit input scope. scope=local targets the Linux/Solari desktop. "
            "scope=remote_rdp targets the currently focused Windows session inside the Remmina winserver window. "
            "Remote Alt+Tab is deliberately rejected because XFCE intercepts it; use the visible remote Windows taskbar for remote app switching. "
            "Examples for remote_rdp: Ctrl+A, Tab, Enter, Backspace, PageDown. Examples for local: Ctrl+L in a local browser or local Alt+Tab when genuinely switching Linux windows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
                "scope": {"type": "string", "enum": ["local", "remote_rdp"], "description": "Where the keystroke is intended to act. Required; never infer remote Windows from local."},
                "purpose": {"type": "string", "description": "Short description for logs and post-action verification."},
            },
            "required": ["keys", "scope"],
        },
    },
}

DESKTOP_TYPE_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_type_text",
        "description": (
            "Type literal NON-SECRET text into the currently focused control through the native Solari Desktop keyboard channel. "
            "Always declare target and scope. scope=remote_rdp means the focused control is inside the Windows session rendered by the winserver Remmina window; "
            "scope=local means a Linux/Solari control. Passwords/TOTP must use security_vault_type_* instead. "
            "Toolkit refuses repeated identical no-effect typing into the same field instead of looping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Literal non-secret text to type."},
                "target": {"type": "string", "description": "Semantic name of the focused field/control, e.g. Vendor input field or local search box."},
                "scope": {"type": "string", "enum": ["local", "remote_rdp"], "description": "UI scope containing the focused control."},
                "purpose": {"type": "string", "description": "Short description for logs and verification."},
            },
            "required": ["text", "target", "scope"],
        },
    },
}

DESKTOP_REFRESH_VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "desktop_refresh_view",
        "description": "Return the newest exact clean raw desktop framebuffer with its native frame version and width/height. No grid or CV overlay is added.",
        "parameters": {"type": "object", "properties": {}},
    },
}

# These are local host tools, not Solari lifecycle tools.  They are deliberately
# tiny and return only redacted status; the bridge keeps PSKs, profile passwords,
# and signed SDK handles outside the model context.
VDI_VPN_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "vdi_vpn_status",
        "description": "Return sanitized status for the selected enterprise VPN provider, loopback relay, and Remmina; never returns credentials.",
        "parameters": {"type": "object", "properties": {}},
    },
}

VDI_VPN_CONNECT_TOOL = {
    "type": "function",
    "function": {
        "name": "vdi_vpn_connect",
        "description": "Start or repair the selected host-owned enterprise VPN provider, loopback relay, and persistent Remmina profile.",
        "parameters": {"type": "object", "properties": {}},
    },
}

VDI_VPN_DISCONNECT_TOOL = {
    "type": "function",
    "function": {
        "name": "vdi_vpn_disconnect",
        "description": "Disconnect the VDI VPN/relay only when explicitly requested by the user.",
        "parameters": {"type": "object", "properties": {}},
    },
}

VDI_DESKTOP_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "vdi_desktop_state",
        "description": "Return sanitized desktop, relay, and Remmina process state for verification.",
        "parameters": {"type": "object", "properties": {}},
    },
}

LOCAL_VDI_TOOLS = {
    "vdi_vpn_status": VDI_VPN_STATUS_TOOL,
    "vdi_vpn_connect": VDI_VPN_CONNECT_TOOL,
    "vdi_vpn_disconnect": VDI_VPN_DISCONNECT_TOOL,
    "vdi_desktop_state": VDI_DESKTOP_STATE_TOOL,
}

SECURITY_VAULT_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "security_vault_status",
        "description": "Check whether the host-owned self-hosted credential vault is ready. Returns only sanitized status and never credentials or vault session keys.",
        "parameters": {"type": "object", "properties": {}},
    },
}

SECURITY_VAULT_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "security_vault_search",
        "description": "Search credential item names in the host-owned Vaultwarden/Bitwarden vault. Returns only safe metadata (item id/name and whether username/password/TOTP fields exist), never field values.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Service, system, or credential label to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "required": ["query"],
        },
    },
}

def _vault_type_tool(name: str, field: str, default_submit: bool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                f"Focus the specified visible {field} field using native framebuffer coordinates, fetch the {field} from the host-owned vault, "
                "and type it directly into the Solari desktop. The secret is NEVER returned to you, added to conversation history, or sent to the AI provider. "
                "The host also security-redacts the credential row from subsequent model-visible frames. "
                + ("For TOTP, submit=true is recommended so the rotating code is typed and submitted atomically. " if field == "totp" else "")
                + "Use an item name or exact safe item id returned by security_vault_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "credential": {"type": "string", "description": "Vault item name or safe item id. Never put a password, TOTP seed, or OTP code here."},
                    "frame_version": {"type": "integer", "minimum": 0, "description": "Exact framebuffer version containing the visible input field."},
                    "x_px": {"type": "integer", "minimum": 0, "description": "Native x pixel visibly inside the intended input field."},
                    "y_px": {"type": "integer", "minimum": 0, "description": "Native y pixel visibly inside the intended input field."},
                    "submit": {"type": "boolean", "default": default_submit, "description": "Press Enter immediately after secure typing without exposing the secret-filled field to the model."},
                },
                "required": ["credential", "frame_version", "x_px", "y_px"],
            },
        },
    }

SECURITY_VAULT_TYPE_USERNAME_TOOL = _vault_type_tool("security_vault_type_username", "username", False)
SECURITY_VAULT_TYPE_PASSWORD_TOOL = _vault_type_tool("security_vault_type_password", "password", False)
SECURITY_VAULT_TYPE_TOTP_TOOL = _vault_type_tool("security_vault_type_totp", "totp", True)

LOCAL_SECURITY_TOOLS = {
    "security_vault_status": SECURITY_VAULT_STATUS_TOOL,
    "security_vault_search": SECURITY_VAULT_SEARCH_TOOL,
    "security_vault_type_username": SECURITY_VAULT_TYPE_USERNAME_TOOL,
    "security_vault_type_password": SECURITY_VAULT_TYPE_PASSWORD_TOOL,
    "security_vault_type_totp": SECURITY_VAULT_TYPE_TOTP_TOOL,
}

START_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "start_goal",
        "description": (
            "Declare a persistent multi-step/outcome-oriented goal. Use this as the FIRST tool call "
            "when the user's request requires discovery, searching, research, verification, or several "
            "desktop operations before success can be known. Do NOT use it for greetings, explanations, "
            "or a single bounded action such as clicking one button or navigating to one URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "Compact restatement of the user's requested outcome; never broaden or replace it.",
                },
                "success_condition": {
                    "type": "string",
                    "description": "Observable condition that must be satisfied before the goal can be called successful.",
                },
                "target": {
                    "type": "string",
                    "description": "Optional exact product, item, page, fact, or entity the outcome is about.",
                },
            },
            "required": ["objective", "success_condition"],
        },
    },
}

FINISH_GOAL_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_goal",
        "description": (
            "Resolve the current persistent goal. Call SUCCESS only after the exact success condition is "
            "satisfied with concrete desktop/tool evidence tied to the requested target. Call NOT_FOUND only after "
            "reasonably exhausting relevant paths with observed evidence. BLOCKED is host-authorized only: request "
            "BLOCKED only when a host/tool message explicitly says host_authorized_blocked=true. This tool unlocks "
            "the final user-facing answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["SUCCESS", "NOT_FOUND", "BLOCKED"],
                },
                "result": {
                    "type": "string",
                    "description": "Concise resolved outcome. Required for SUCCESS; explain absence/blocker otherwise.",
                },
                "evidence": {
                    "type": "string",
                    "description": "Concrete evidence supporting the status, including the exact target association when relevant.",
                },
            },
            "required": ["status", "result", "evidence"],
        },
    },
}


GOAL_REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "goal_remember",
        "description": (
            "Persist an observed fact/checkpoint for the active multi-step goal so it survives screenshot compaction. "
            "Call this immediately after reading instructions, extracting invoice/account data, or verifying a workflow step BEFORE leaving that screen. "
            "Store the useful content itself; do not store vague notes such as 'instructions read'. Desktop-derived facts must reference the exact frame_version that showed them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Stable short key such as reimbursement_instructions, invoice_fields, claim_draft_saved."},
                "value": {"type": "string", "description": "Concise but complete fact/checkpoint content needed later."},
                "evidence": {"type": "string", "description": "Concrete observation supporting the fact."},
                "source": {"type": "string", "enum": ["desktop_frame", "tool_result", "user_request"]},
                "frame_version": {"type": "integer", "minimum": 0, "description": "Required when source=desktop_frame; exact frame where the fact was visible."},
                "completed": {"type": "boolean", "default": False, "description": "True when this represents a completed workflow checkpoint rather than a reference fact."},
            },
            "required": ["key", "value", "evidence", "source"],
        },
    },
}


# ============================================================================
# RFB byte stream over WebSocket
# ============================================================================

class WebSocketByteStream:
    """Turn WebSocket messages into a continuous byte stream for RFB."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.buffer = bytearray()
        self.offset = 0

    async def readexactly(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("negative read size")

        while len(self.buffer) - self.offset < size:
            message = await self.websocket.recv()
            if isinstance(message, str):
                # A base64 subprotocol isn't requested, but this makes the
                # client diagnostic-friendly if a proxy unexpectedly sends text.
                try:
                    chunk = base64.b64decode(message, validate=True)
                except Exception as exc:
                    raise RuntimeError(
                        "RFB WebSocket returned text instead of binary data"
                    ) from exc
            else:
                chunk = bytes(message)

            if self.offset:
                # Compact before growing a large framebuffer-sized buffer.
                del self.buffer[: self.offset]
                self.offset = 0
            self.buffer.extend(chunk)

        start = self.offset
        end = start + size
        output = bytes(self.buffer[start:end])
        self.offset = end

        if self.offset == len(self.buffer):
            self.buffer.clear()
            self.offset = 0
        elif self.offset > 1024 * 1024:
            del self.buffer[: self.offset]
            self.offset = 0

        return output


class RFBFrameCache:
    """Minimal RFB 3.x viewer that requests Raw framebuffer rectangles.

    Solari's streamUrl is a signed WebSocket carrying RFB/VNC bytes. We request
    only Raw encoding (plus DesktopSize) so the decoder remains deterministic
    and dependency-light.
    """

    ENCODING_RAW = 0
    ENCODING_DESKTOP_SIZE = -223
    ENCODING_LAST_RECT = -224

    def __init__(self, stream_url: str, *, initial_version: int = 0):
        self._stream_url = stream_url
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stopping = False

        self._image: Image.Image | None = None
        self._version = max(0, int(initial_version))
        self._updated_at = 0.0
        self._condition = asyncio.Condition()
        self._ready = asyncio.Event()
        self._connected = asyncio.Event()

        self.width = 0
        self.height = 0
        self.desktop_name = ""
        self.last_error: str | None = None

    @property
    def version(self) -> int:
        return self._version

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="solari-rfb-cache")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=RFB_START_TIMEOUT)
        except Exception:
            await self.stop()
            detail = self.last_error or "no framebuffer arrived"
            raise RuntimeError(f"RFB stream failed to become ready: {detail}")

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None
        self._connected.clear()

    async def snapshot(self) -> FrameSnapshot:
        # On reconnect, _ready is cleared until a fresh full framebuffer arrives.
        # Bound this wait so a dead RFB connection cannot freeze the CLI forever.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=RFB_SNAPSHOT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            detail = self.last_error or "waiting for a fresh framebuffer"
            raise RuntimeError(f"RFB snapshot timed out: {detail}") from exc

        async with self._condition:
            if self._image is None:
                raise RuntimeError("RFB framebuffer is not initialized")
            return FrameSnapshot(
                image=self._image.copy(),
                version=self._version,
                updated_at=self._updated_at,
            )

    async def wait_for_change(
        self,
        after_version: int,
        *,
        timeout: float = RFB_POST_ACTION_TIMEOUT,
        stable_seconds: float = RFB_STABLE_SECONDS,
    ) -> FrameSnapshot:
        """Wait for a real framebuffer change, then return after it settles.

        If no update arrives before timeout, return the newest frame instead of
        failing the action. This is useful for clicks that only change focus.
        """
        deadline = time.monotonic() + timeout

        async with self._condition:
            while self._version <= after_version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError:
                    break

        # A page may repaint in several quick RFB updates. Wait until the frame
        # version remains unchanged for one short stability interval.
        while time.monotonic() < deadline:
            before = self._version
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(stable_seconds, remaining))
            if self._version == before:
                break

        return await self.snapshot()

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._connected.clear()
                self._ready.clear()
                if not self._stopping:
                    print(f"[RFB stream reconnecting after {self.last_error}]", flush=True)
                    await asyncio.sleep(RFB_RECONNECT_DELAY)

    async def _run_connection(self) -> None:
        # The signed stream URL is itself the credential. Do not print it or
        # attach the Solari API bearer to this connection.
        async with websockets.connect(
            self._stream_url,
            subprotocols=["binary"],
            max_size=None,
            compression=None,
            open_timeout=10,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            reader = WebSocketByteStream(ws)
            await self._handshake(ws, reader)
            self._connected.set()
            print(
                f"[RFB stream connected: {self.width}x{self.height}"
                + (f" name={self.desktop_name!r}" if self.desktop_name else "")
                + "]",
                flush=True,
            )

            await self._send_set_pixel_format(ws)
            await self._send_set_encodings(ws)
            await self._request_update(ws, incremental=False)

            while not self._stopping:
                message_type = (await reader.readexactly(1))[0]

                if message_type == 0:  # FramebufferUpdate
                    await self._handle_framebuffer_update(ws, reader)
                elif message_type == 1:  # SetColourMapEntries
                    await reader.readexactly(1)  # padding
                    _, count = struct.unpack(">HH", await reader.readexactly(4))
                    await reader.readexactly(count * 6)
                elif message_type == 2:  # Bell
                    pass
                elif message_type == 3:  # ServerCutText
                    await reader.readexactly(3)
                    length = struct.unpack(">I", await reader.readexactly(4))[0]
                    await reader.readexactly(length)
                else:
                    raise RuntimeError(f"Unsupported RFB server message type {message_type}")

    async def _handshake(self, ws, reader: WebSocketByteStream) -> None:
        banner = await reader.readexactly(12)
        if not banner.startswith(b"RFB ") or not banner.endswith(b"\n"):
            raise RuntimeError(f"Invalid RFB protocol banner: {banner!r}")

        try:
            server_major = int(banner[4:7])
            server_minor = int(banner[8:11])
        except Exception as exc:
            raise RuntimeError(f"Unparseable RFB version: {banner!r}") from exc

        if server_major < 3:
            raise RuntimeError(f"Unsupported RFB server version {server_major}.{server_minor}")

        # Solari/noVNC currently uses 3.8. Treat later 3.x / 4.x banners as 3.8
        # for standard negotiation, mirroring common noVNC behavior.
        client_minor = 8 if (server_major > 3 or server_minor >= 8) else (7 if server_minor >= 7 else 3)
        await ws.send(f"RFB 003.{client_minor:03d}\n".encode("ascii"))

        if client_minor >= 7:
            count = (await reader.readexactly(1))[0]
            if count == 0:
                reason_length = struct.unpack(">I", await reader.readexactly(4))[0]
                reason = (await reader.readexactly(reason_length)).decode("utf-8", "replace")
                raise RuntimeError(f"RFB security negotiation failed: {reason}")

            security_types = list(await reader.readexactly(count))
            if 1 not in security_types:
                raise RuntimeError(
                    "RFB stream did not offer None authentication; offered "
                    + ",".join(str(x) for x in security_types)
                )

            # The signed stream URL is already the capability; select RFB None.
            await ws.send(b"\x01")
            result = struct.unpack(">I", await reader.readexactly(4))[0]
            if result != 0:
                reason = "authentication failed"
                if client_minor >= 8:
                    length = struct.unpack(">I", await reader.readexactly(4))[0]
                    reason = (await reader.readexactly(length)).decode("utf-8", "replace")
                raise RuntimeError(f"RFB security result {result}: {reason}")
        else:
            security_type = struct.unpack(">I", await reader.readexactly(4))[0]
            if security_type == 0:
                length = struct.unpack(">I", await reader.readexactly(4))[0]
                reason = (await reader.readexactly(length)).decode("utf-8", "replace")
                raise RuntimeError(f"RFB connection failed: {reason}")
            if security_type != 1:
                raise RuntimeError(f"Unsupported RFB 3.3 security type {security_type}")

        # Shared desktop flag.
        await ws.send(b"\x01")

        self.width, self.height = struct.unpack(">HH", await reader.readexactly(4))
        await reader.readexactly(16)  # server pixel format; we set ours next
        name_length = struct.unpack(">I", await reader.readexactly(4))[0]
        self.desktop_name = (await reader.readexactly(name_length)).decode("utf-8", "replace")
        self._image = Image.new("RGB", (self.width, self.height), "black")

    async def _send_set_pixel_format(self, ws) -> None:
        # 32-bit little-endian true-color with R/G/B shifts 16/8/0.
        # Wire bytes for a pixel are therefore B, G, R, padding.
        payload = struct.pack(
            ">B3xBBBBHHHBBB3x",
            0,   # message type: SetPixelFormat
            32,  # bits per pixel
            24,  # depth
            0,   # little endian
            1,   # true color
            255,
            255,
            255,
            16,
            8,
            0,
        )
        await ws.send(payload)

    async def _send_set_encodings(self, ws) -> None:
        encodings = [self.ENCODING_RAW, self.ENCODING_DESKTOP_SIZE]
        payload = struct.pack(">BBH", 2, 0, len(encodings))
        payload += b"".join(struct.pack(">i", encoding) for encoding in encodings)
        await ws.send(payload)

    async def _request_update(self, ws, *, incremental: bool) -> None:
        if self.width <= 0 or self.height <= 0:
            return
        payload = struct.pack(
            ">BBHHHH",
            3,
            1 if incremental else 0,
            0,
            0,
            self.width,
            self.height,
        )
        await ws.send(payload)

    async def _handle_framebuffer_update(self, ws, reader: WebSocketByteStream) -> None:
        await reader.readexactly(1)  # padding
        rectangle_count = struct.unpack(">H", await reader.readexactly(2))[0]
        changed = False

        for _ in range(rectangle_count):
            x, y, width, height, encoding = struct.unpack(
                ">HHHHi", await reader.readexactly(12)
            )

            if encoding == self.ENCODING_RAW:
                if width == 0 or height == 0:
                    continue
                pixel_bytes = await reader.readexactly(width * height * 4)
                rectangle = Image.frombytes(
                    "RGB",
                    (width, height),
                    pixel_bytes,
                    "raw",
                    "BGRX",
                )
                if self._image is None:
                    self._image = Image.new("RGB", (self.width, self.height), "black")
                self._image.paste(rectangle, (x, y))
                changed = True

            elif encoding == self.ENCODING_DESKTOP_SIZE:
                new_width, new_height = width, height
                old = self._image
                resized = Image.new("RGB", (new_width, new_height), "black")
                if old is not None:
                    crop = old.crop((0, 0, min(old.width, new_width), min(old.height, new_height)))
                    resized.paste(crop, (0, 0))
                self._image = resized
                self.width = new_width
                self.height = new_height
                changed = True
                print(f"[RFB desktop resized: {new_width}x{new_height}]", flush=True)

            elif encoding == self.ENCODING_LAST_RECT:
                break

            else:
                # The client requested only Raw + DesktopSize, so receiving a
                # compressed encoding indicates server behavior we can't safely
                # skip because its payload length is encoding-specific.
                raise RuntimeError(
                    f"RFB server sent unrequested/unsupported encoding {encoding}"
                )

        if changed:
            async with self._condition:
                self._version += 1
                self._updated_at = time.monotonic()
                self._condition.notify_all()
                if not self._ready.is_set():
                    self._ready.set()
                    print(f"[RFB first framebuffer ready: version={self._version}]", flush=True)

        await self._request_update(ws, incremental=True)


# ============================================================================
# Frame -> model image helpers
# ============================================================================

def remember_presented_frame(snapshot: FrameSnapshot) -> None:
    """Keep a bounded copy of exact full frames prepared for model vision."""
    PRESENTED_FRAMES[snapshot.version] = FrameSnapshot(
        image=snapshot.image.copy(),
        version=snapshot.version,
        updated_at=snapshot.updated_at,
    )
    while len(PRESENTED_FRAMES) > max(1, VISUAL_FRAME_HISTORY_SIZE):
        oldest_version = next(iter(PRESENTED_FRAMES))
        PRESENTED_FRAMES.pop(oldest_version, None)


def _grid_font() -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=16)
    except TypeError:
        return ImageFont.load_default()


def _draw_grid_overlay(image: Image.Image, cols: int, rows: int) -> Image.Image:
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = base.size
    line_width = max(1, round(min(w, h) / 500))
    line_fill = (255, 215, 0, 105)
    font = _grid_font()

    for c in range(1, cols):
        x = round(c * w / cols)
        draw.line((x, 0, x, h), fill=line_fill, width=line_width)
    for r in range(1, rows):
        y = round(r * h / rows)
        draw.line((0, y, w, y), fill=line_fill, width=line_width)

    for r in range(rows):
        for c in range(cols):
            label = f"{chr(ord('A') + r)}{c + 1}"
            x0 = round(c * w / cols) + 4
            y0 = round(r * h / rows) + 4
            try:
                bbox = draw.textbbox((x0, y0), label, font=font)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
            except Exception:
                tw, th = 20, 12
            draw.rounded_rectangle(
                (x0 - 2, y0 - 2, x0 + tw + 3, y0 + th + 3),
                radius=3,
                fill=(0, 0, 0, 160),
            )
            draw.text((x0, y0), label, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(base, overlay).convert("RGB")


def _active_sensitive_redactions() -> list[SensitiveRedactionBand]:
    now = time.monotonic()
    if SENSITIVE_REDACTIONS:
        SENSITIVE_REDACTIONS[:] = [band for band in SENSITIVE_REDACTIONS if band.expires_at > now]
    return list(SENSITIVE_REDACTIONS)


def _register_sensitive_redaction(y_px: int, frame_height: int, reason: str) -> None:
    half = VAULT_REDACT_HALF_HEIGHT
    y0 = max(0, int(y_px) - half)
    y1 = min(int(frame_height), int(y_px) + half + 1)
    SENSITIVE_REDACTIONS.append(
        SensitiveRedactionBand(
            y0=y0,
            y1=y1,
            expires_at=time.monotonic() + VAULT_REDACT_SECONDS,
            reason=reason,
        )
    )
    # Keep only a small number of recent credential rows.
    if len(SENSITIVE_REDACTIONS) > 6:
        del SENSITIVE_REDACTIONS[:-6]


def _security_redact_render(
    image: Image.Image,
    *,
    source_bbox: tuple[int, int, int, int] | None,
    source_frame_width: int | None,
    source_frame_height: int | None,
) -> tuple[Image.Image, bool]:
    bands = _active_sensitive_redactions()
    if not bands:
        return image, False

    full_w = int(source_frame_width or image.width)
    full_h = int(source_frame_height or image.height)
    left, top, right, bottom = source_bbox or (0, 0, full_w, full_h)
    span_h = max(1, bottom - top)
    rendered = image.convert("RGB").copy()
    draw = ImageDraw.Draw(rendered)
    redacted = False
    for band in bands:
        iy0 = max(top, band.y0)
        iy1 = min(bottom, band.y1)
        if iy1 <= iy0:
            continue
        py0 = max(0, min(rendered.height, round((iy0 - top) * rendered.height / span_h)))
        py1 = max(0, min(rendered.height, round((iy1 - top) * rendered.height / span_h)))
        if py1 <= py0:
            continue
        draw.rectangle((0, py0, rendered.width, py1), fill=(0, 0, 0))
        if py1 - py0 >= 24:
            draw.text((8, py0 + 6), "Sensitive credential region redacted by host", fill=(255, 255, 255))
        redacted = True
    return rendered, redacted


def _encode_vision_pil(
    image: Image.Image,
    *,
    label: str,
    frame_version: int,
    is_full_frame: bool,
    source_bbox: tuple[int, int, int, int] | None = None,
    source_frame_width: int | None = None,
    source_frame_height: int | None = None,
    grid_kind: str | None = None,
    grid_cols: int | None = None,
    grid_rows: int | None = None,
    zoom_id: str | None = None,
) -> VisionImage:
    image, sensitive_redacted = _security_redact_render(
        image,
        source_bbox=source_bbox,
        source_frame_width=source_frame_width,
        source_frame_height=source_frame_height,
    )
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    saved_path: str | None = None
    if SAVE_STREAM_FRAMES:
        path = OUTPUT_DIR / f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_v{frame_version}.jpg"
        path.write_bytes(buffer.getvalue())
        saved_path = str(path.resolve())
    return VisionImage(
        label=label,
        mime_type="image/jpeg",
        base64_data=encoded,
        saved_path=saved_path,
        frame_version=frame_version,
        width=image.width,
        height=image.height,
        is_full_frame=is_full_frame,
        source_bbox=source_bbox,
        source_frame_width=source_frame_width,
        source_frame_height=source_frame_height,
        grid_kind=grid_kind,
        grid_cols=grid_cols,
        grid_rows=grid_rows,
        zoom_id=zoom_id,
        sensitive_redacted=sensitive_redacted,
    )


def frame_to_vision_image(snapshot: FrameSnapshot, label: str) -> VisionImage:
    # Preserve the raw framebuffer in PRESENTED_FRAMES for OCR/click mapping, but
    # show the model a lightweight visible grid overlay. The grid is the model's
    # coordinate system; the model never emits pixels.
    remember_presented_frame(snapshot)
    gridded = _draw_grid_overlay(snapshot.image, FULL_GRID_COLS, FULL_GRID_ROWS)
    return _encode_vision_pil(
        gridded,
        label=label,
        frame_version=snapshot.version,
        is_full_frame=True,
        source_frame_width=snapshot.image.width,
        source_frame_height=snapshot.image.height,
        grid_kind="full",
        grid_cols=FULL_GRID_COLS,
        grid_rows=FULL_GRID_ROWS,
    )


def frame_to_clean_vision_image(snapshot: FrameSnapshot, label: str) -> VisionImage:
    """Present the exact desktop without a coordinate overlay.

    legacy semantic grounding owns localization.  The main model receives clean
    pixels for routine observation; the old grid remains available only through
    explicit legacy/fallback paths.
    """
    remember_presented_frame(snapshot)
    return _encode_vision_pil(
        snapshot.image,
        label=label,
        frame_version=snapshot.version,
        is_full_frame=True,
        source_frame_width=snapshot.image.width,
        source_frame_height=snapshot.image.height,
        grid_kind=None,
    )


def geometry_note(image: VisionImage) -> str:
    width = image.source_frame_width or image.width
    height = image.source_frame_height or image.height
    if image.grid_kind is None and image.is_full_frame:
        security_note = (
            "A horizontal credential region is blacked out by the host so secret material never reaches the AI provider. "
            "Do not infer or click inside the redacted band. "
            if image.sensitive_redacted else ""
        )
        label = "Host security-redacted framebuffer" if image.sensitive_redacted else "Host exact clean framebuffer"
        return (
            f"[{label} v{image.frame_version}]\n"
            f"Native framebuffer size: WIDTH={width}, HEIGHT={height} pixels.\n"
            + security_note
            + "Origin (0,0) is the exact top-left pixel. x increases right; y increases down. "
            "Inspect THIS image yourself. For GUI input call desktop_click_native or desktop_double_click_native with "
            f"frame_version={image.frame_version} and native x_px/y_px inside this {width}x{height} image. "
            "Do not normalize coordinates and do not ask the host to locate the target."
        )
    return (
        f"[Host visual evidence from framebuffer v{image.frame_version}]\n"
        f"Source framebuffer size: WIDTH={width}, HEIGHT={height}. "
        "Toolkit interaction uses only native full-frame coordinates; request desktop_refresh_view before clicking if this is not a full clean framebuffer."
    )


def build_vision_message(images: list[VisionImage]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "[Host visual desktop output]\n"
                "The image(s) below are host-provided desktop evidence, not a new human request. A full-frame "
                "image is a live Solari framebuffer snapshot; a precision crop is explicitly labeled as such. "
                "Toolkit VISUAL EPOCH: this newest framebuffer evidence is authoritative for CURRENT GUI state. "
                "Any older screen layout/focus/window narration is superseded. Inspect this evidence and continue the original task."
            ),
        }
    ]
    for image in images:
        content.append({"type": "text", "text": geometry_note(image)})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime_type};base64,{image.base64_data}"
                },
            }
        )
    return {"role": "user", "content": content}


def _message_has_image(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


_SUPERSEDED_VISUAL_PLACEHOLDER = (
    "[Superseded desktop visual epoch omitted by host. Its pixels, geometry, focus/window state, "
    "and screen-layout narration are no longer authoritative. Use only the newest framebuffer for CURRENT GUI state.]"
)


def _is_host_visual_geometry_text(text: str) -> bool:
    value = str(text or "").lstrip()
    return value.startswith(
        (
            "[Host exact clean framebuffer v",
            "[Host security-redacted framebuffer v",
            "[Host visual evidence from framebuffer v",
        )
    )


def _human_request_prefix_from_visual_text(text: str) -> str:
    """Recover only the real human request from a user-turn multimodal message."""
    value = str(text or "")
    markers = (
        "\n\n[Host note: attached live RFB frame version ",
        "\n[Host exact clean framebuffer v",
        "\n[Host security-redacted framebuffer v",
        "\n[Host visual evidence from framebuffer v",
    )
    cut = len(value)
    found = False
    for marker in markers:
        pos = value.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
            found = True
    return value[:cut].rstrip() if found else ""


def _supersede_visual_message(message: dict[str, Any]) -> dict[str, Any]:
    """Remove an old framebuffer AND every host text description tied to it."""
    output = deepcopy(message)
    content = output.get("content")
    if not isinstance(content, list):
        output["content"] = _SUPERSEDED_VISUAL_PLACEHOLDER
        return output

    human_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        prefix = _human_request_prefix_from_visual_text(str(part.get("text", "") or ""))
        if prefix:
            human_parts.append(prefix)

    if human_parts:
        output["content"] = "\n\n".join(human_parts) + "\n\n" + _SUPERSEDED_VISUAL_PLACEHOLDER
    else:
        output["content"] = _SUPERSEDED_VISUAL_PLACEHOLDER
    return output


def _limit_visual_message_to_last_images(message: dict[str, Any], keep_last: int) -> dict[str, Any]:
    """Keep only newest image parts and their paired geometry text in one message."""
    output = deepcopy(message)
    content = output.get("content")
    if not isinstance(content, list):
        return output
    image_indices = [
        i for i, part in enumerate(content)
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    if keep_last <= 0:
        return _supersede_visual_message(output)
    if len(image_indices) <= keep_last:
        return output

    keep_images = set(image_indices[-keep_last:])
    rebuilt: list[Any] = []
    for i, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == "image_url":
            if i in keep_images:
                rebuilt.append(part)
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            text = str(part.get("text", "") or "")
            if _is_host_visual_geometry_text(text):
                next_image = next((j for j in image_indices if j > i), None)
                if next_image is not None and next_image not in keep_images:
                    continue
        rebuilt.append(part)
    output["content"] = rebuilt if rebuilt else _SUPERSEDED_VISUAL_PLACEHOLDER
    return output


def _strip_prior_assistant_visual_narration(compacted: list[dict[str, Any]]) -> None:
    """Remove model-authored stale screen-state prose but preserve tool protocol."""
    for index, message in enumerate(compacted):
        if message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            # Tool call objects remain; only free-form progress narration goes.
            message["content"] = ""
            continue
        # A no-tool message immediately followed by the host goal gate was
        # explicitly suppressed, so its visual prose is not durable evidence.
        if index + 1 < len(compacted):
            nxt = compacted[index + 1]
            nxt_content = nxt.get("content")
            if (
                nxt.get("role") == "user"
                and isinstance(nxt_content, str)
                and nxt_content.startswith("[Host goal gate]")
            ):
                message["content"] = "[Suppressed prior assistant narration omitted by host.]"


def count_request_images(source_messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in source_messages:
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1
                for part in content
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
    return count


def compact_messages_for_request(source_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a provider request with exactly one authoritative visual epoch."""
    compacted = deepcopy(source_messages)
    vision_indices = [
        index
        for index, message in enumerate(compacted)
        if _message_has_image(message)
    ]

    if vision_indices:
        _strip_prior_assistant_visual_narration(compacted)

    # Supersede complete old visual messages: pixels + host geometry text.
    if vision_indices:
        keep_count = max(0, MAX_VISION_MESSAGES_IN_CONTEXT)
        keep = set(vision_indices[-keep_count:]) if keep_count > 0 else set()
        for index in vision_indices:
            if index not in keep:
                compacted[index] = _supersede_visual_message(compacted[index])

    # Enforce actual image-part cap. Geometry for removed images is removed too.
    if MAX_IMAGES_PER_REQUEST > 0:
        remaining = MAX_IMAGES_PER_REQUEST
        for index in range(len(compacted) - 1, -1, -1):
            message = compacted[index]
            if not _message_has_image(message):
                continue
            content = message.get("content", [])
            image_count = sum(
                1 for part in content
                if isinstance(part, dict) and part.get("type") == "image_url"
            )
            keep_here = min(image_count, remaining)
            compacted[index] = _limit_visual_message_to_last_images(message, keep_here)
            remaining -= keep_here

    return compacted


def validate_tool_message_protocol(source_messages: list[dict[str, Any]]) -> None:
    """Validate OpenAI-compatible assistant/tool message ordering locally.

    Every assistant message that contains tool_calls must be followed
    immediately by one role="tool" message for each tool_call_id, with no
    user/system/assistant message inserted in between. This catches host-side
    history construction bugs before they become provider HTTP 400 errors.
    """
    index = 0
    while index < len(source_messages):
        message = source_messages[index]
        role = message.get("role")
        tool_calls = message.get("tool_calls") if role == "assistant" else None

        if tool_calls:
            expected_ids: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    raise RuntimeError(
                        f"Invalid tool protocol at message {index}: tool_calls entry is not an object"
                    )
                call_id = call.get("id")
                if not call_id:
                    raise RuntimeError(
                        f"Invalid tool protocol at message {index}: assistant tool call is missing id"
                    )
                expected_ids.append(str(call_id))

            seen_ids: list[str] = []
            cursor = index + 1
            while cursor < len(source_messages):
                next_message = source_messages[cursor]
                if next_message.get("role") != "tool":
                    break
                tool_call_id = next_message.get("tool_call_id")
                if not tool_call_id:
                    raise RuntimeError(
                        f"Invalid tool protocol at message {cursor}: tool message is missing tool_call_id"
                    )
                seen_ids.append(str(tool_call_id))
                cursor += 1

            missing = [call_id for call_id in expected_ids if call_id not in seen_ids]
            unexpected = [call_id for call_id in seen_ids if call_id not in expected_ids]
            duplicates = sorted(
                {call_id for call_id in seen_ids if seen_ids.count(call_id) > 1}
            )

            if missing or unexpected or duplicates:
                raise RuntimeError(
                    "Invalid OpenAI-compatible tool history before model request: "
                    f"assistant message index={index}, "
                    f"missing_tool_results={missing}, "
                    f"unexpected_tool_results={unexpected}, "
                    f"duplicate_tool_results={duplicates}. "
                    "A host/user message must never be inserted between assistant tool_calls "
                    "and their role='tool' responses."
                )

            index = cursor
            continue

        if role == "tool":
            raise RuntimeError(
                f"Invalid OpenAI-compatible tool history at message {index}: "
                "orphan role='tool' message without an immediately preceding assistant tool_calls message"
            )

        index += 1


# ============================================================================
# MCP tool/session helpers
# ============================================================================

def _model_visible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an MCP schema with host-owned session fields removed.

    TOOL_SCHEMAS keeps the original server schema. The model sees this sanitized
    version, so it cannot invent session IDs; execute_tool_call injects the real
    active session on every applicable MCP call.
    """
    visible = deepcopy(schema or {"type": "object", "properties": {}})
    properties = visible.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        visible["properties"] = properties

    session_fields = {"sessionId", "session_id", "sandboxId", "sandbox_id"}
    for key in list(properties):
        if key in session_fields:
            properties.pop(key, None)

    required = visible.get("required")
    if isinstance(required, list):
        visible["required"] = [key for key in required if key not in session_fields]

    return visible


def convert_mcp_tools(mcp_tools) -> list[dict[str, Any]]:
    global TOOL_SCHEMAS
    TOOL_SCHEMAS = {}
    output: list[dict[str, Any]] = []

    host_owned = set(HOST_OWNED_LIFECYCLE_TOOLS) | set(LOCAL_VDI_TOOLS) | set(LOCAL_SECURITY_TOOLS) | {
        SCREENSHOT_TOOL,
        RAW_CLICK_TOOL,
        "solari_key",
        "solari_type",
        "solari_open_app",
    }

    for tool in mcp_tools:
        # MCP SDKs expose the schema as either inputSchema (current) or input_schema (legacy).
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None)
        schema = schema or {"type": "object", "properties": {}}
        TOOL_SCHEMAS[tool.name] = schema

        if tool.name.startswith("solari_browser_"):
            continue
        if tool.name in host_owned:
            continue
        if not ALLOW_SHELL_TOOLS and tool.name in SHELL_TOOLS:
            continue

        output.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "") + (
                        " The host automatically supplies the active desktop session; "
                        "do not provide or ask for a session ID."
                        if session_args_for(tool.name, "__probe__")
                        else ""
                    ),
                    "parameters": _model_visible_schema(schema),
                },
            }
        )

    # VDI controls are host-local and therefore are not expected from the
    # public Solari MCP server.  Add their schemas explicitly after filtering
    # the remote tool list.
    output.extend(LOCAL_VDI_TOOLS.values())
    if VAULT_ENABLED:
        output.extend(LOCAL_SECURITY_TOOLS.values())

    # Toolkit intentionally exposes NO host visual-localization tools. The chat model
    # chooses native framebuffer coordinates itself.
    output.append(DESKTOP_CLICK_NATIVE_TOOL)
    output.append(DESKTOP_DOUBLE_CLICK_NATIVE_TOOL)
    output.append(DESKTOP_HOTKEY_TOOL)
    output.append(DESKTOP_TYPE_TEXT_TOOL)
    output.append(DESKTOP_REFRESH_VIEW_TOOL)
    output.append(START_GOAL_TOOL)
    output.append(GOAL_REMEMBER_TOOL)
    output.append(FINISH_GOAL_TOOL)
    return output


def schema_properties(tool_name: str) -> dict[str, Any]:
    schema = TOOL_SCHEMAS.get(tool_name) or {}
    properties = schema.get("properties") or {}
    return properties if isinstance(properties, dict) else {}


def session_args_for(tool_name: str, session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {}
    properties = schema_properties(tool_name)
    for key in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
        if key in properties:
            return {key: session_id}
    return {}


def session_id_from_arguments(arguments: dict[str, Any]) -> str | None:
    for key in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_json_text(text: str) -> Any | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def json_values_from_result(result) -> list[Any]:
    values: list[Any] = []
    structured = getattr(result, "structured_content", None)
    if structured not in (None, {}, []):
        values.append(structured)
    for block in getattr(result, "content", []):
        if isinstance(block, TextContent) and block.text:
            parsed = parse_json_text(block.text)
            if parsed is not None:
                values.append(parsed)
    return values


def result_text(result) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []):
        if isinstance(block, TextContent) and block.text:
            parts.append(block.text)
    structured = getattr(result, "structured_content", None)
    if structured not in (None, {}, []):
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    return "\n".join(parts).strip()


def find_session_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = find_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_session_id(child)
            if found:
                return found
    return None


def session_id_from_result(result) -> str | None:
    for value in json_values_from_result(result):
        found = find_session_id(value)
        if found:
            return found
    return None


def collect_desktop_candidates(value: Any, output: list[str], *, inherited_desktop: bool = False) -> None:
    if isinstance(value, dict):
        kind = str(value.get("kind") or value.get("type") or "").lower()
        is_desktop = inherited_desktop or kind == "desktop"

        if is_desktop:
            for key in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate and candidate not in output:
                    output.append(candidate)

        for child in value.values():
            collect_desktop_candidates(child, output, inherited_desktop=is_desktop)

    elif isinstance(value, list):
        for child in value:
            collect_desktop_candidates(child, output, inherited_desktop=inherited_desktop)


def desktop_candidates_from_list(result) -> list[str]:
    candidates: list[str] = []
    for value in json_values_from_result(result):
        collect_desktop_candidates(value, candidates)

    # If Solari returns an already desktop-filtered list without a kind field,
    # fall back to collecting session-like identifiers.
    if not candidates:
        for value in json_values_from_result(result):
            def walk(v: Any) -> None:
                if isinstance(v, dict):
                    for key in ("sessionId", "session_id", "sandboxId", "sandbox_id"):
                        candidate = v.get(key)
                        if isinstance(candidate, str) and candidate and candidate not in candidates:
                            candidates.append(candidate)
                    for child in v.values():
                        walk(child)
                elif isinstance(v, list):
                    for child in v:
                        walk(child)
            walk(value)

    return candidates


def _candidate_info_in_value(value: Any, session_id: str) -> DesktopCandidateInfo | None:
    """Find label/state metadata for one desktop session in a solari_list payload."""
    if isinstance(value, dict):
        current_id = None
        for key in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                current_id = candidate
                break

        if current_id == session_id:
            metadata = value.get("metadata")
            label = value.get("label")
            if not isinstance(label, str) or not label.strip():
                if isinstance(metadata, dict):
                    metadata_label = metadata.get("label")
                    label = metadata_label if isinstance(metadata_label, str) and metadata_label.strip() else None
                else:
                    label = None

            state = value.get("state") or value.get("status")
            if not isinstance(state, str) or not state.strip():
                state = None

            return DesktopCandidateInfo(
                session_id=session_id,
                label=label,
                state=state,
            )

        for child in value.values():
            found = _candidate_info_in_value(child, session_id)
            if found is not None:
                return found

    elif isinstance(value, list):
        for child in value:
            found = _candidate_info_in_value(child, session_id)
            if found is not None:
                return found

    return None


def desktop_candidate_infos_from_list(result, candidates: list[str]) -> dict[str, DesktopCandidateInfo]:
    infos: dict[str, DesktopCandidateInfo] = {}
    values = json_values_from_result(result)
    for session_id in candidates:
        info = None
        for value in values:
            info = _candidate_info_in_value(value, session_id)
            if info is not None:
                break
        infos[session_id] = info or DesktopCandidateInfo(session_id=session_id)
    return infos


def _candidate_label_text(info: DesktopCandidateInfo | None) -> str:
    if info is None or not info.label:
        return "<none>"
    return repr(info.label)


def _candidate_state_text(info: DesktopCandidateInfo | None) -> str:
    if info is None or not info.state:
        return "<unknown>"
    return info.state


async def _stream_url_from_desktop_handle(desktop) -> str:
    """Get a fresh signed RFB URL from a Desktop SDK handle.

    ``streamUrl`` can be cached on a reattached/resumed SDK object.  Prefer
    ``stream.start()`` so the gateway issues a current capability, then fall
    back to the property for older SDK versions that do not expose the method.
    The URL itself is never printed.
    """
    start_error: Exception | None = None
    stream = getattr(desktop, "stream", None)
    start = getattr(stream, "start", None)
    if callable(start):
        try:
            stream_info = start()
            if inspect.isawaitable(stream_info):
                stream_info = await stream_info
            if isinstance(stream_info, str):
                stream_url = stream_info
            elif isinstance(stream_info, dict):
                stream_url = stream_info.get("streamUrl") or stream_info.get("stream_url")
            else:
                stream_url = getattr(stream_info, "streamUrl", None) or getattr(stream_info, "stream_url", None)
            if isinstance(stream_url, str) and stream_url.startswith(("ws://", "wss://")):
                print("[Host obtained a fresh RFB stream URL]", flush=True)
                return stream_url
        except Exception as exc:
            start_error = exc

    stream_url = getattr(desktop, "streamUrl", None) or getattr(desktop, "stream_url", None)
    if isinstance(stream_url, str) and stream_url.startswith(("ws://", "wss://")):
        if start_error is not None:
            print(
                f"[Host fresh stream request unavailable: {type(start_error).__name__}; using SDK streamUrl property]",
                flush=True,
            )
        return stream_url

    detail = f" ({type(start_error).__name__})" if start_error is not None else ""
    raise RuntimeError(f"Desktop SDK did not return a valid streamUrl{detail}")


async def _wait_for_desktop_ready(desktop) -> Any:
    """Wait until the SDK reports a usable display/VNC health state."""
    health = getattr(desktop, "health", None)
    if not callable(health):
        # Older SDKs may not expose health().  Keep compatibility and let the
        # RFB handshake be the readiness check in that case.
        print("[Host desktop health() unavailable; using RFB handshake as readiness check]", flush=True)
        return None

    deadline = time.monotonic() + max(1.0, DESKTOP_HEALTH_TIMEOUT)
    last_error: Exception | None = None
    last_health: Any = None
    while True:
        try:
            value = health()
            if inspect.isawaitable(value):
                value = await value
            last_health = value
            if isinstance(value, dict):
                ready = value.get("ready")
            else:
                ready = getattr(value, "ready", None)

            if ready is True:
                print("[Host desktop health ready]", flush=True)
                return value
            if ready is None:
                # Do not reject an SDK variant whose health payload does not
                # include the documented boolean; RFB remains the final check.
                print("[Host desktop health returned no ready field; using RFB handshake]", flush=True)
                return value
            last_error = None
        except Exception as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error is not None:
                detail = f"last health error: {type(last_error).__name__}: {last_error}"
            else:
                detail = f"last health response: {last_health!r}"
            raise RuntimeError(
                f"Desktop did not become ready within {DESKTOP_HEALTH_TIMEOUT:.0f}s ({detail})"
            )
        await asyncio.sleep(min(max(0.1, DESKTOP_HEALTH_POLL_INTERVAL), remaining))


async def _start_frame_cache(stream_url: str, *, version_base: int = 0) -> RFBFrameCache:
    cache = RFBFrameCache(stream_url, initial_version=version_base)
    print("[Host starting live RFB framebuffer -- stream URL redacted]", flush=True)
    await cache.start()
    return cache


async def _start_frame_cache_for_desktop(desktop, *, version_base: int = 0) -> RFBFrameCache:
    """Start RFB, refreshing the signed stream capability after a failure."""
    failures: list[str] = []
    for attempt in range(1, RFB_STREAM_ATTEMPTS + 1):
        try:
            stream_url = await _stream_url_from_desktop_handle(desktop)
            return await _start_frame_cache(stream_url, version_base=version_base)
        except Exception as exc:
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < RFB_STREAM_ATTEMPTS:
                print(
                    f"[Host RFB preflight failed; refreshing stream capability "
                    f"({attempt}/{RFB_STREAM_ATTEMPTS})]",
                    flush=True,
                )
                await asyncio.sleep(RFB_STREAM_RETRY_DELAY)
    raise RuntimeError("RFB stream preflight failed: " + " | ".join(failures[-3:]))


async def _refresh_frame_cache_from_desktop(
    reason: str,
    *,
    strict: bool | None = None,
) -> FrameSnapshot:
    """Replace the long-lived RFB cache with a newly issued signed stream.

    Solari's long-lived incremental RFB connection can remain internally healthy
    while no longer reflecting GUI changes made from another viewer/session.
    A fresh ``desktop.stream.start()`` capability has proven to return the actual
    current framebuffer. Toolkit therefore rebases the cache before each human turn
    and explicit refresh. Frame versions remain monotonically increasing across
    replacements so native-coordinate frame binding stays unambiguous.
    """
    global FRAME_CACHE

    if strict is None:
        strict = TOOLKIT_FRESH_FRAME_STRICT
    if HOST_DESKTOP_CONTROL is None:
        raise RuntimeError("No Solari Desktop control handle is attached")

    old_cache = FRAME_CACHE
    version_base = old_cache.version if old_cache is not None else 0
    fresh_cache: RFBFrameCache | None = None
    started = time.monotonic()
    try:
        fresh_cache = await _start_frame_cache_for_desktop(
            HOST_DESKTOP_CONTROL,
            version_base=version_base,
        )
        snapshot = await fresh_cache.snapshot()
        FRAME_CACHE = fresh_cache
        if old_cache is not None and old_cache is not fresh_cache:
            await old_cache.stop()
        age_ms = round((time.monotonic() - snapshot.updated_at) * 1000)
        print(
            f"[toolkit-rfb] fresh stream reason={reason!r} "
            f"v{version_base}->v{snapshot.version} "
            f"resolution={snapshot.image.width}x{snapshot.image.height} "
            f"frame_age={age_ms}ms refresh_ms={round((time.monotonic()-started)*1000)}",
            flush=True,
        )
        return snapshot
    except Exception as exc:
        if fresh_cache is not None and fresh_cache is not FRAME_CACHE:
            try:
                await fresh_cache.stop()
            except Exception:
                pass
        print(
            f"[toolkit-rfb] fresh stream FAILED reason={reason!r}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        if strict or old_cache is None:
            raise RuntimeError(
                f"Could not obtain a trustworthy fresh framebuffer for {reason}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return await old_cache.snapshot()


async def _mcp_attach_session(mcp, session_id: str) -> str:
    """Attach/resume a desktop through MCP and return the server-confirmed id."""
    connect_args = session_args_for("solari_connect", session_id)
    if not connect_args:
        connect_args = {"sessionId": session_id}

    connect_result = await mcp.call_tool("solari_connect", connect_args)
    if getattr(connect_result, "is_error", False):
        detail = result_text(connect_result)[:500]
        raise RuntimeError(f"solari_connect failed: {detail or 'unknown error'}")

    return (
        session_id_from_result(connect_result)
        or session_id_from_arguments(connect_args)
        or session_id
    )


def _desktop_handle_id(desktop: Any) -> str:
    """Extract a desktop/session identifier across SDK versions."""
    for name in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
        value = getattr(desktop, name, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(desktop, dict):
        for name in ("sessionId", "session_id", "sandboxId", "sandbox_id", "id"):
            value = desktop.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def _persist_vdi_env(values: dict[str, str]) -> None:
    """Persist non-secret lifecycle identifiers using legacy's selected state backend."""
    if not values:
        return
    mode = os.getenv("VDI_PERSIST_MODE", "auto").strip().lower() or "auto"
    if mode == "auto":
        mode = "config" if LOADED_CONFIG_FILE is not None else "env"
    if mode in {"none", "off", "disabled"}:
        return
    if mode == "config":
        try:
            persist_runtime_values(values)
        except OSError as exc:
            print(f"[Host lifecycle warning: could not persist VDI config state: {type(exc).__name__}]", flush=True)
        return
    if mode != "env":
        print(f"[Host lifecycle warning: unknown VDI_PERSIST_MODE={mode!r}; identifiers not persisted]", flush=True)
        return
    path = VDI_PERSIST_ENV_FILE.expanduser()
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        existing = ""
    lines = existing.splitlines()
    for key, value in values.items():
        if not value:
            continue
        replacement = f"{key}={value}"
        for index, line in enumerate(lines):
            if line.startswith(key + "="):
                lines[index] = replacement
                break
        else:
            lines.append(replacement)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"[Host lifecycle warning: could not persist VDI env identifiers: {type(exc).__name__}]", flush=True)


def _configure_vdi_bridge(desktop_sdk: DesktopClient, desktop: Any) -> None:
    """Give the host-owned VDI bridge the active SDK/desktop handle."""
    if vdi_bridge is None:
        return
    # Current vdi_vpn_mcp uses these module globals; support a future explicit
    # configure hook without requiring it.
    if hasattr(vdi_bridge, "_CLIENT"):
        vdi_bridge._CLIENT = desktop_sdk
    if hasattr(vdi_bridge, "_DESKTOP"):
        vdi_bridge._DESKTOP = desktop
    configure = getattr(vdi_bridge, "configure_desktop", None)
    if callable(configure):
        try:
            configure(desktop_sdk, desktop)
        except TypeError:
            configure(desktop)


async def _connect_desktop_candidate(
    mcp,
    desktop_sdk: DesktopClient,
    session_id: str,
    *,
    info: DesktopCandidateInfo | None = None,
):
    """Attach one candidate, open its control channel, and start RFB."""
    confirmed_id = await _mcp_attach_session(mcp, session_id)
    desktop = await desktop_sdk.connect(confirmed_id)
    await desktop.connect()
    await _wait_for_desktop_ready(desktop)
    cache = await _start_frame_cache_for_desktop(desktop)
    _configure_vdi_bridge(desktop_sdk, desktop)
    if info is not None:
        print(
            f"[Host ACTIVE DESKTOP: label={_candidate_label_text(info)} "
            f"state={_candidate_state_text(info)} sessionId={confirmed_id}]",
            flush=True,
        )
    print(f"[Host attached live desktop session; framebuffer version={cache.version}]", flush=True)
    return confirmed_id, cache, desktop


async def _create_desktop_before_prompt(mcp, desktop_sdk: DesktopClient) -> tuple[str, RFBFrameCache, Any]:
    """Create and attach a GUI desktop when no existing desktop is available."""
    print(
        f"[Host no desktop sessions found; creating one before chat starts "
        f"(template={AUTO_DESKTOP_TEMPLATE!r}, label={AUTO_DESKTOP_LABEL!r})]",
        flush=True,
    )

    # Prefer the reusable VDI snapshot when configured.  Keep compatibility
    # fallbacks for older SDKs that use camelCase or do not accept metadata.
    create_variants: list[dict[str, Any]] = []
    base_kwargs: dict[str, Any] = {"template": VDI_TEMPLATE_ID}
    if VDI_IMAGE_SNAPSHOT_ID:
        create_variants.extend(
            [
                {**base_kwargs, "from_snapshot": VDI_IMAGE_SNAPSHOT_ID},
                {**base_kwargs, "fromSnapshot": VDI_IMAGE_SNAPSHOT_ID},
            ]
        )
    create_variants.append(base_kwargs)
    desktop = None
    last_type_error: Exception | None = None
    for kwargs in create_variants:
        try:
            desktop = await desktop_sdk.create(
                **kwargs,
                metadata={"label": AUTO_DESKTOP_LABEL},
            )
            break
        except TypeError as exc:
            last_type_error = exc
            try:
                desktop = await desktop_sdk.create(**kwargs)
                break
            except TypeError as inner_exc:
                last_type_error = inner_exc
                continue
    if desktop is None:
        if last_type_error is not None:
            raise last_type_error
        raise RuntimeError("DesktopClient.create() returned no desktop")
    created_label: str | None = AUTO_DESKTOP_LABEL

    session_id = _desktop_handle_id(desktop)
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("DesktopClient.create() did not return a usable sessionId")

    # Register/reattach the SDK-created VM with the MCP side so all existing MCP
    # action tools can use the same signed session capability.
    confirmed_session_id = await _mcp_attach_session(mcp, session_id)

    # Open the Desktop SDK control channel once. Host-owned atomic navigation
    # uses this channel for true keyboard chords instead of translating them
    # through the MCP solari_key schema.
    await desktop.connect()
    await _wait_for_desktop_ready(desktop)

    _configure_vdi_bridge(desktop_sdk, desktop)
    _persist_vdi_env({"VDI_DESKTOP_ID": confirmed_session_id})

    cache = await _start_frame_cache_for_desktop(desktop)

    if created_label:
        print(f"[Host created desktop successfully: label={created_label!r}]", flush=True)
    else:
        print("[Host created desktop successfully]", flush=True)
    print(f"[Host attached live desktop session; framebuffer version={cache.version}]", flush=True)
    return confirmed_session_id, cache, desktop


async def resolve_existing_desktop(mcp, desktop_sdk: DesktopClient) -> tuple[str, RFBFrameCache, Any]:
    """Resolve/create, attach, obtain streamUrl, and verify the RFB stream host-side."""
    if "solari_list" not in TOOL_SCHEMAS or "solari_connect" not in TOOL_SCHEMAS:
        raise RuntimeError("Solari MCP does not expose solari_list/solari_connect")

    print("[Host resolving existing desktop -- no model call]", flush=True)

    # Prefer the operator-selected VDI desktop instead of whichever desktop
    # happens to appear first in solari_list.  This prevents the chatbot from
    # attaching to an unrelated browser/testing VM.
    if VDI_PREFER_SAVED_DESKTOP and VDI_DESKTOP_ID:
        print("[Host trying VDI_DESKTOP_ID from environment]", flush=True)
        try:
            return await _connect_desktop_candidate(mcp, desktop_sdk, VDI_DESKTOP_ID)
        except Exception as exc:
            print(
                f"[Host saved VDI desktop unavailable: {type(exc).__name__}; "
                "falling back to desktop discovery]",
                flush=True,
            )

    list_result = await mcp.call_tool("solari_list", {})
    if getattr(list_result, "is_error", False):
        raise RuntimeError(f"solari_list failed: {result_text(list_result)[:500]}")

    candidates = desktop_candidates_from_list(list_result)
    infos = desktop_candidate_infos_from_list(list_result, candidates)
    print(f"[Host found {len(candidates)} desktop candidate(s)]", flush=True)

    # New startup behavior: never reach the user prompt without a desktop.
    if not candidates:
        return await _create_desktop_before_prompt(mcp, desktop_sdk)

    # When multiple desktops exist, make the selection completely explicit.
    # Session IDs are capability credentials; they are printed here only because
    # this local CLI's operator explicitly requested them for disambiguation.
    if len(candidates) >= 2:
        print("[Host multiple desktop sessions detected]", flush=True)
        for index, candidate in enumerate(candidates, start=1):
            info = infos.get(candidate)
            print(
                f"  [{index}] label={_candidate_label_text(info)} "
                f"state={_candidate_state_text(info)} sessionId={candidate}",
                flush=True,
            )
        print("[Host selection policy: first attachable desktop in solari_list order]", flush=True)

    failures: list[str] = []
    ordered_candidates = list(candidates)
    if VDI_DESKTOP_ID and VDI_DESKTOP_ID in ordered_candidates:
        ordered_candidates.remove(VDI_DESKTOP_ID)
        ordered_candidates.insert(0, VDI_DESKTOP_ID)

    for candidate in ordered_candidates:
        info = infos.get(candidate)
        try:
            result = await _connect_desktop_candidate(
                mcp,
                desktop_sdk,
                candidate,
                info=info if len(candidates) >= 2 else None,
            )
            _persist_vdi_env({"VDI_DESKTOP_ID": result[0]})
            return result

        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            label_fragment = f" label={info.label!r}" if info and info.label else ""
            print(
                f"[Host rejected desktop candidate{label_fragment}: "
                f"{type(exc).__name__}: {exc}]",
                flush=True,
            )

    if VDI_CREATE_ON_ATTACH_FAILURE:
        print(
            "[Host no attachable desktop passed health/RFB preflight; provisioning a fresh desktop]",
            flush=True,
        )
        try:
            return await _create_desktop_before_prompt(mcp, desktop_sdk)
        except Exception as exc:
            failures.append(f"fresh-create {type(exc).__name__}: {exc}")

    raise RuntimeError("Could not attach any desktop candidate: " + " | ".join(failures[-3:]))


# ============================================================================
# OCR localization
# ============================================================================

def normalize_text(text: str) -> str:
    # Accent-fold first so OCR "mas" and UI "más" compare as the same text.
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"\s+", " ", folded.lower().strip())
    return re.sub(r"[^\w\s]", "", folded)


def text_similarity(detected: str, target: str) -> float:
    detected = normalize_text(detected)
    target = normalize_text(target)
    if not detected or not target:
        return 0.0
    if detected == target:
        return 1.0

    # Reject the exact failure mode seen in the Altice run: tiny OCR fragments
    # such as "e", "R" or "a" must never qualify for labels like "Ver más"
    # or "Galaxy Z Fold8 Ultra".
    if len(detected) < 3 and len(target) > len(detected):
        return 0.0

    detected_words = detected.split()
    target_words = target.split()
    char_coverage = len(detected) / max(len(target), 1)
    word_coverage = len(detected_words) / max(len(target_words), 1)

    # A multi-word target requires substantial textual coverage.
    if len(target_words) >= 2 and char_coverage < 0.55 and word_coverage < 0.60:
        return 0.0

    sequence = SequenceMatcher(None, detected, target).ratio()

    if target in detected:
        coverage = len(target) / max(len(detected), 1)
        if coverage < 0.60:
            return 0.0
        return max(sequence, 0.82 + 0.18 * coverage)

    if detected in target:
        coverage = len(detected) / max(len(target), 1)
        if coverage < 0.60:
            return 0.0
        return max(sequence, 0.80 + 0.20 * coverage)

    # Non-substring fuzzy matches also need meaningful coverage for longer labels.
    if len(target) >= 6 and char_coverage < 0.50:
        return 0.0

    return sequence


def get_region_crop(image: Image.Image, region: str) -> tuple[Image.Image, int, int]:
    width, height = image.size
    boxes = {
        "top": (0, 0, width, int(height * 0.5)),
        "bottom": (0, int(height * 0.5), width, height),
        "left": (0, 0, int(width * 0.5), height),
        "right": (int(width * 0.5), 0, width, height),
        "center": (
            int(width * 0.15),
            int(height * 0.15),
            int(width * 0.85),
            int(height * 0.85),
        ),
    }
    box = boxes.get(region, (0, 0, width, height))
    return image.crop(box), box[0], box[1]


def preprocess_for_ocr(image: Image.Image) -> Image.Image:
    enlarged = image.convert("RGB").resize(
        (image.width * OCR_SCALE, image.height * OCR_SCALE)
    )
    gray = ImageOps.autocontrast(ImageOps.grayscale(enlarged))
    gray = ImageEnhance.Contrast(gray).enhance(1.7)
    return gray.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))


def candidates_for_pass(
    processed: Image.Image,
    target: str,
    offset_x: int,
    offset_y: int,
    psm: int,
) -> list[OCRCandidate]:
    data = pytesseract.image_to_data(
        processed,
        config=f"--psm {psm}",
        output_type=Output.DICT,
    )

    lines: dict[tuple, list[dict[str, Any]]] = {}
    for index, raw_text in enumerate(data["text"]):
        text = (raw_text or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except Exception:
            confidence = -1
        if confidence < 5:
            continue

        line_key = (
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )
        lines.setdefault(line_key, []).append(
            {
                "text": text,
                "left": int(data["left"][index]) / OCR_SCALE + offset_x,
                "top": int(data["top"][index]) / OCR_SCALE + offset_y,
                "width": int(data["width"][index]) / OCR_SCALE,
                "height": int(data["height"][index]) / OCR_SCALE,
                "confidence": confidence,
            }
        )

    target_words = max(1, len(normalize_text(target).split()))
    max_ngram = target_words + 2
    output: list[OCRCandidate] = []

    for words in lines.values():
        words.sort(key=lambda item: item["left"])
        for start in range(len(words)):
            for size in range(1, min(max_ngram, len(words) - start) + 1):
                group = words[start : start + size]
                detected = " ".join(item["text"] for item in group)
                similarity = text_similarity(detected, target)
                if similarity < 0.45:
                    continue

                left = min(item["left"] for item in group)
                top = min(item["top"] for item in group)
                right = max(item["left"] + item["width"] for item in group)
                bottom = max(item["top"] + item["height"] for item in group)
                confidence = sum(item["confidence"] for item in group) / len(group)
                final_score = similarity * 0.88 + max(0, min(confidence / 100, 1)) * 0.12

                output.append(
                    OCRCandidate(
                        text=detected,
                        left=int(left),
                        top=int(top),
                        width=max(1, int(right - left)),
                        height=max(1, int(bottom - top)),
                        text_score=similarity,
                        ocr_confidence=confidence,
                        final_score=final_score,
                    )
                )

    return output


def locate_text(
    image: Image.Image,
    target: str,
    region: str,
) -> tuple[OCRCandidate | None, list[OCRCandidate], str | None]:
    crop, offset_x, offset_y = get_region_crop(image, region)
    processed = preprocess_for_ocr(crop)

    candidates = (
        candidates_for_pass(processed, target, offset_x, offset_y, 11)
        + candidates_for_pass(processed, target, offset_x, offset_y, 6)
    )
    candidates.sort(key=lambda candidate: candidate.final_score, reverse=True)

    unique: list[OCRCandidate] = []
    for candidate in candidates:
        if not any(
            abs(candidate.center_x - existing.center_x) <= 8
            and abs(candidate.center_y - existing.center_y) <= 8
            for existing in unique
        ):
            unique.append(candidate)

    if not unique:
        return None, [], "No OCR candidate matched the target"

    best = unique[0]
    if best.text_score < MIN_MATCH_SCORE:
        return (
            None,
            unique[:10],
            f"Best OCR similarity {best.text_score:.3f} is below {MIN_MATCH_SCORE:.2f}",
        )

    if len(unique) >= 2:
        second = unique[1]
        if (
            best.text_score >= 0.92
            and second.text_score >= 0.92
            and (
                abs(best.center_x - second.center_x) > 30
                or abs(best.center_y - second.center_y) > 20
            )
            and abs(best.final_score - second.final_score) <= AMBIGUITY_MARGIN
        ):
            return None, unique[:10], "Multiple strong matches; retry with a region"

    return best, unique[:10], None


def to_desktop_xy(image: Image.Image, x: int, y: int) -> tuple[int, int]:
    desktop_width = DESKTOP_WIDTH if DESKTOP_WIDTH > 0 else image.width
    desktop_height = DESKTOP_HEIGHT if DESKTOP_HEIGHT > 0 else image.height
    return (
        max(0, min(round(x * desktop_width / image.width), desktop_width - 1)),
        max(0, min(round(y * desktop_height / image.height), desktop_height - 1)),
    )


def save_debug_image(image: Image.Image, candidate: OCRCandidate, target: str) -> str:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    left, top = candidate.left, candidate.top
    right, bottom = left + candidate.width, top + candidate.height
    draw.rectangle([left, top, right, bottom], outline="red", width=3)
    x, y = candidate.center_x, candidate.center_y
    draw.line([x - 15, y, x + 15, y], fill="red", width=3)
    draw.line([x, y - 15, x, y + 15], fill="red", width=3)

    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", target)
    path = OUTPUT_DIR / f"click_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    debug.save(path)
    return str(path.resolve())


def normalized_to_frame_xy(width: int, height: int, x: float, y: float) -> tuple[int, int]:
    """Map provider-independent normalized coordinates onto an exact image."""
    nx = max(0.0, min(float(x), float(VISUAL_COORD_MAX)))
    ny = max(0.0, min(float(y), float(VISUAL_COORD_MAX)))
    px = round((nx / VISUAL_COORD_MAX) * max(0, width - 1))
    py = round((ny / VISUAL_COORD_MAX) * max(0, height - 1))
    return px, py


def normalized_to_desktop_xy(image: Image.Image, x: float, y: float) -> tuple[int, int, int, int]:
    """Return (frame_x, frame_y, desktop_x, desktop_y)."""
    frame_x, frame_y = normalized_to_frame_xy(image.width, image.height, x, y)
    desktop_x, desktop_y = to_desktop_xy(image, frame_x, frame_y)
    return frame_x, frame_y, desktop_x, desktop_y


def save_point_debug_image(
    image: Image.Image,
    *,
    frame_x: int,
    frame_y: int,
    norm_x: float,
    norm_y: float,
    target: str,
    frame_version: int,
) -> str:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    radius = 18
    draw.ellipse(
        [frame_x - radius, frame_y - radius, frame_x + radius, frame_y + radius],
        outline="red",
        width=3,
    )
    draw.line([frame_x - 24, frame_y, frame_x + 24, frame_y], fill="red", width=3)
    draw.line([frame_x, frame_y - 24, frame_x, frame_y + 24], fill="red", width=3)
    label = f"{target} n=({norm_x:.1f},{norm_y:.1f}) f=({frame_x},{frame_y}) v{frame_version}"
    text_y = max(0, frame_y - 42)
    draw.text((max(0, frame_x - 120), text_y), label, fill="red")

    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", target)[:80] or "visual_target"
    path = OUTPUT_DIR / f"point_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_v{frame_version}.png"
    debug.save(path)
    return str(path.resolve())



# ============================================================================
# Host-side desktop action helpers
# ============================================================================

SESSION_FIELD_NAMES = ("sessionId", "session_id", "sandboxId", "sandbox_id")

def inject_active_session(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Force the real host-attached session into an MCP tool call.

    Any model-supplied session-like value is discarded. This prevents values such
    as "current" or "desktop" from reaching Solari.
    """
    clean = dict(arguments)
    for key in SESSION_FIELD_NAMES:
        clean.pop(key, None)

    if ACTIVE_DESKTOP_SESSION_ID:
        clean.update(session_args_for(tool_name, ACTIVE_DESKTOP_SESSION_ID))
    return clean


def _build_key_arguments(keys: list[str]) -> dict[str, Any]:
    props = schema_properties("solari_key")
    args = session_args_for("solari_key", ACTIVE_DESKTOP_SESSION_ID)
    normalized = [str(key).upper() for key in keys]

    if "keys" in props:
        spec = props.get("keys") or {}
        args["keys"] = normalized if spec.get("type") == "array" else "+".join(normalized)
        return args

    for name in ("shortcut", "combo", "sequence"):
        if name in props:
            args[name] = "+".join(normalized)
            return args

    if "key" in props:
        args["key"] = normalized[-1]
        modifiers = normalized[:-1]
        if "modifiers" in props:
            spec = props.get("modifiers") or {}
            args["modifiers"] = modifiers if spec.get("type") == "array" else "+".join(modifiers)
        for prop, aliases in {
            "ctrl": {"CTRL", "CONTROL"},
            "control": {"CTRL", "CONTROL"},
            "alt": {"ALT", "OPTION"},
            "shift": {"SHIFT"},
            "meta": {"META", "CMD", "COMMAND", "SUPER"},
            "cmd": {"CMD", "COMMAND"},
            "super": {"SUPER", "META"},
        }.items():
            if prop in props:
                args[prop] = any(item in aliases for item in modifiers)
        return args

    required = [
        key for key in (TOOL_SCHEMAS.get("solari_key") or {}).get("required", [])
        if key not in SESSION_FIELD_NAMES
    ]
    if len(required) == 1:
        args[required[0]] = "+".join(normalized)
        return args

    raise RuntimeError(f"Unable to infer solari_key argument schema: {props}")


def _build_type_arguments(text: str) -> dict[str, Any]:
    props = schema_properties("solari_type")
    args = session_args_for("solari_type", ACTIVE_DESKTOP_SESSION_ID)
    for name in ("text", "value", "input", "content"):
        if name in props:
            args[name] = text
            return args

    required = [
        key for key in (TOOL_SCHEMAS.get("solari_type") or {}).get("required", [])
        if key not in SESSION_FIELD_NAMES
    ]
    string_required = [key for key in required if (props.get(key) or {}).get("type") == "string"]
    if len(string_required) == 1:
        args[string_required[0]] = text
        return args

    raise RuntimeError(f"Unable to infer solari_type argument schema: {props}")


async def _host_key(mcp, keys: list[str]) -> Any:
    args = _build_key_arguments(keys)
    result = await mcp.call_tool("solari_key", args)
    if getattr(result, "is_error", False):
        raise RuntimeError(result_text(result) or f"solari_key failed for {keys}")
    return result


async def _host_type(mcp, text: str) -> Any:
    args = _build_type_arguments(text)
    result = await mcp.call_tool("solari_type", args)
    if getattr(result, "is_error", False):
        raise RuntimeError(result_text(result) or "solari_type failed")
    return result


def _normalize_navigation_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    if "://" not in url:
        url = "https://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Only http:// and https:// URLs are supported")
    return url


def _frame_difference_metrics(before: Image.Image, after: Image.Image) -> tuple[float, float]:
    """Return (mean grayscale absolute difference, changed-pixel fraction)."""
    if before.size != after.size:
        return 255.0, 1.0
    scale_w = min(320, before.width)
    scale_h = max(1, round(before.height * scale_w / before.width))
    thumb_size = (scale_w, scale_h)
    a = before.convert("RGB").resize(thumb_size)
    b = after.convert("RGB").resize(thumb_size)
    diff = ImageChops.difference(a, b).convert("L")
    mean_diff = float(ImageStat.Stat(diff).mean[0])
    hist = diff.histogram()
    threshold = max(1, min(255, POINT_STALE_PIXEL_THRESHOLD))
    changed = sum(hist[threshold + 1 :])
    total = max(1, diff.width * diff.height)
    return mean_diff, changed / total


async def _wait_for_meaningful_navigation_change(
    before_snapshot: FrameSnapshot,
    *,
    timeout: float,
) -> tuple[FrameSnapshot, bool, float, float]:
    """Keep watching after Enter until the page really differs from the omnibox frame."""
    if FRAME_CACHE is None:
        return before_snapshot, False, 0.0, 0.0
    deadline = time.monotonic() + max(0.1, timeout)
    last = before_snapshot
    last_mean = 0.0
    last_fraction = 0.0
    after_version = before_snapshot.version
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        post = await FRAME_CACHE.wait_for_change(
            after_version,
            timeout=min(max(0.1, NAVIGATION_POLL_SLICE), max(0.1, remaining)),
            stable_seconds=min(0.10, max(0.02, NAVIGATION_STABLE_SECONDS / 3.0)),
        )
        last = post
        after_version = max(after_version, post.version)
        mean_diff, changed_fraction = _frame_difference_metrics(before_snapshot.image, post.image)
        last_mean, last_fraction = mean_diff, changed_fraction
        meaningful = (
            changed_fraction >= NAVIGATION_MIN_CHANGED_FRACTION
            or mean_diff >= max(3.0, POINT_STALE_MEAN_DIFF / 2.0)
        )
        if meaningful:
            settle_until = min(deadline, time.monotonic() + NAVIGATION_STABLE_SECONDS)
            settled_version = post.version
            while time.monotonic() < settle_until:
                await asyncio.sleep(min(0.10, max(0.01, settle_until - time.monotonic())))
                newer = await FRAME_CACHE.snapshot()
                if newer.version != settled_version:
                    last, settled_version = newer, newer.version
            mean_diff, changed_fraction = _frame_difference_metrics(before_snapshot.image, last.image)
            return last, True, mean_diff, changed_fraction
        if remaining <= 0:
            break
    return last, False, last_mean, last_fraction


def _sdk_key_name(key: str) -> str:
    value = str(key).strip().lower()
    aliases = {
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "control": "ctrl",
        "ctl": "ctrl",
        "cmd": "meta",
        "command": "meta",
        "page_down": "PageDown",
        "pagedown": "PageDown",
        "page_up": "PageUp",
        "pageup": "PageUp",
        "backspace": "Backspace",
        "delete": "Delete",
        "tab": "Tab",
        "home": "Home",
        "end": "End",
        "left": "Left",
        "right": "Right",
        "up": "Up",
        "down": "Down",
        "space": "space",
    }
    return aliases.get(value, value)


async def _host_nav_press(*keys: str) -> None:
    """Press one key/chord through the native Solari Desktop keyboard channel."""
    if HOST_DESKTOP_CONTROL is None:
        raise RuntimeError("Desktop SDK control handle is not available for host navigation")
    normalized = [_sdk_key_name(key) for key in keys if str(key).strip()]
    if not normalized:
        return
    keyboard = HOST_DESKTOP_CONTROL.keyboard
    if len(normalized) == 1:
        await keyboard.press(normalized[0])
        return
    hotkey = getattr(keyboard, "hotkey", None)
    if callable(hotkey):
        await hotkey(*normalized)
        return
    # Compatibility fallback for SDKs exposing only press(list).
    await keyboard.press(normalized)


async def _host_nav_paste(text: str) -> None:
    """Put exact text on the guest clipboard and paste it with a host-synthesized Ctrl+V chord."""
    if HOST_DESKTOP_CONTROL is None:
        raise RuntimeError("Desktop SDK control handle is not available for host navigation")
    await HOST_DESKTOP_CONTROL.clipboard.set(text)
    await asyncio.sleep(max(0.05, NAVIGATION_INTERSTEP_DELAY / 2.0))
    await _host_nav_press("ctrl", "v")


async def _host_open_browser_url(url: str) -> tuple[bool, str | None, str | None]:
    """Open a URL directly in a new browser tab without keyboard shortcuts.

    The default Solari desktop uses Chrome, but the fallbacks make the public
    example work with closely related workstation templates too.
    """
    if HOST_DESKTOP_CONTROL is None:
        return False, None, "Desktop SDK control handle is not available"

    configured = os.getenv("SOLARI_BROWSER_EXECUTABLE", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "firefox"])
    seen: set[str] = set()
    errors: list[str] = []
    for executable in candidates:
        if not executable or executable in seen:
            continue
        seen.add(executable)
        args = ["--new-tab", url] if "chrome" in executable or "chromium" in executable else [url]
        try:
            await HOST_DESKTOP_CONTROL.open(executable, args)
            return True, executable, None
        except Exception as exc:
            errors.append(f"{executable}: {type(exc).__name__}: {exc}")
    return False, None, "; ".join(errors[-3:])


async def _host_omnibox_navigate(url: str) -> None:
    """Navigate the current browser tab using host-synthesized shortcuts + clipboard paste."""
    try:
        await _host_nav_press("Escape")
    except Exception:
        pass
    await _host_nav_press("ctrl", "l")
    await asyncio.sleep(max(0.12, NAVIGATION_INTERSTEP_DELAY))
    await _host_nav_paste(url)
    await asyncio.sleep(max(NAVIGATION_TYPE_SETTLE_DELAY, NAVIGATION_INTERSTEP_DELAY))
    await _host_nav_press("Return")


async def _atomic_navigation(mcp, url: str, *, new_tab: bool) -> tuple[dict[str, Any], list[VisionImage]]:
    """Navigate with direct browser open for new tabs and clipboard omnibox recovery."""
    if FRAME_CACHE is None or not ACTIVE_DESKTOP_SESSION_ID:
        return {"success": False, "reason": "No active live desktop is attached."}, []
    url = _normalize_navigation_url(url)
    source = await FRAME_CACHE.snapshot()
    max_attempts = max(1, NAVIGATION_RETRIES + 1)
    last_post = source
    last_mean_diff = 0.0
    last_changed_fraction = 0.0
    print(f"[Atomic navigation: {'new tab -> ' if new_tab else ''}{url}]", flush=True)

    # New tabs are opened directly by the browser process. This avoids Ctrl+T,
    # Ctrl+L, and literal URL typing entirely.
    if new_tab:
        before_open = await FRAME_CACHE.snapshot()
        opened, executable, open_error = await _host_open_browser_url(url)
        if opened:
            post, meaningful, mean_diff, changed_fraction = await _wait_for_meaningful_navigation_change(
                before_open, timeout=max(NAVIGATION_TIMEOUT, 12.0)
            )
            last_post, last_mean_diff, last_changed_fraction = post, mean_diff, changed_fraction
            print(
                f"[Atomic navigation direct-open: browser={executable} before=v{before_open.version} "
                f"after=v{post.version} meaningful={meaningful} diff_mean={mean_diff:.1f} "
                f"changed_fraction={changed_fraction:.3f}]",
                flush=True,
            )
            if meaningful:
                return ({
                    "success": True,
                    "url": url,
                    "new_tab": True,
                    "method": "browser_open",
                    "browser_executable": executable,
                    "attempts": 1,
                    "source_frame_version": source.version,
                    "post_frame_version": post.version,
                    "meaningful_frame_change": True,
                    "changed_fraction": round(changed_fraction, 4),
                }, [frame_to_vision_image(post, "post_navigation")])
        else:
            print(f"[Atomic navigation direct-open unavailable: {open_error}]", flush=True)
        # Fall through to the explicit-modifier omnibox path if direct open did
        # not create a visible new tab/page.

    for attempt in range(1, max_attempts + 1):
        before_enter = await FRAME_CACHE.snapshot()
        try:
            await _host_omnibox_navigate(url)
        except Exception as exc:
            print(f"[Atomic navigation omnibox attempt {attempt} raised {type(exc).__name__}: {exc}]", flush=True)
            await asyncio.sleep(max(0.20, NAVIGATION_INTERSTEP_DELAY))
            continue

        post, meaningful, mean_diff, changed_fraction = await _wait_for_meaningful_navigation_change(
            before_enter, timeout=max(NAVIGATION_TIMEOUT, 10.0)
        )
        last_post, last_mean_diff, last_changed_fraction = post, mean_diff, changed_fraction
        print(
            f"[Atomic navigation omnibox attempt {attempt}/{max_attempts}: "
            f"before=v{before_enter.version} after=v{post.version} meaningful={meaningful} "
            f"diff_mean={mean_diff:.1f} changed_fraction={changed_fraction:.3f}]",
            flush=True,
        )
        if meaningful:
            return ({
                "success": True,
                "url": url,
                "new_tab": new_tab,
                "method": "clipboard_omnibox",
                "attempts": attempt,
                "source_frame_version": source.version,
                "post_frame_version": post.version,
                "meaningful_frame_change": True,
                "changed_fraction": round(changed_fraction, 4),
            }, [frame_to_vision_image(post, "post_navigation")])
        await asyncio.sleep(max(0.20, NAVIGATION_INTERSTEP_DELAY))

    # Final recovery: if same-tab omnibox navigation did not visibly work, open
    # the target in a fresh browser tab rather than concatenating/retyping again.
    if not new_tab:
        before_fallback = await FRAME_CACHE.snapshot()
        opened, executable, open_error = await _host_open_browser_url(url)
        if opened:
            post, meaningful, mean_diff, changed_fraction = await _wait_for_meaningful_navigation_change(
                before_fallback, timeout=max(NAVIGATION_TIMEOUT, 12.0)
            )
            last_post, last_mean_diff, last_changed_fraction = post, mean_diff, changed_fraction
            print(
                f"[Atomic navigation fallback-new-tab: browser={executable} meaningful={meaningful} "
                f"before=v{before_fallback.version} after=v{post.version}]",
                flush=True,
            )
            if meaningful:
                return ({
                    "success": True,
                    "url": url,
                    "new_tab": True,
                    "requested_new_tab": False,
                    "fallback_new_tab": True,
                    "method": "browser_open_fallback",
                    "browser_executable": executable,
                    "attempts": max_attempts + 1,
                    "source_frame_version": source.version,
                    "post_frame_version": post.version,
                    "meaningful_frame_change": True,
                    "changed_fraction": round(changed_fraction, 4),
                }, [frame_to_vision_image(post, "post_navigation")])
        elif open_error:
            print(f"[Atomic navigation fallback-new-tab unavailable: {open_error}]", flush=True)

    current = await FRAME_CACHE.snapshot()
    if current.version >= last_post.version:
        last_post = current
    current_image = frame_to_vision_image(last_post, "navigation_failed_current")
    print(
        f"[Atomic navigation FAILED after {max_attempts} omnibox attempt(s): url={url} frame=v{last_post.version} "
        f"diff_mean={last_mean_diff:.1f} changed_fraction={last_changed_fraction:.3f}]",
        flush=True,
    )
    return ({
        "success": False,
        "url": url,
        "new_tab": new_tab,
        "attempts": max_attempts,
        "reason": (
            "Host navigation could not verify a meaningful transition using direct browser open or the "
            "explicit-modifier clipboard omnibox path. Inspect the attached current frame; do not repeat "
            "raw Ctrl+L/type/Enter repair unless the visible state specifically requires it."
        ),
        "post_frame_version": last_post.version,
        "frame_changed": last_post.version > source.version,
        "meaningful_frame_change": False,
        "changed_fraction": round(last_changed_fraction, 4),
    }, [current_image])


async def desktop_navigate(mcp, url: str) -> tuple[dict[str, Any], list[VisionImage]]:
    return await _atomic_navigation(mcp, url, new_tab=False)


async def desktop_new_tab(mcp, url: str) -> tuple[dict[str, Any], list[VisionImage]]:
    return await _atomic_navigation(mcp, url, new_tab=True)


# ============================================================================
# Host desktop tools
# ============================================================================

async def _send_double_click(
    mcp,
    click_x: int,
    click_y: int,
    *,
    target: str,
    source_frame_version: int | None,
    debug_path: str | None = None,
) -> tuple[dict[str, Any], list[VisionImage]]:
    """Send two host-resolved clicks as one atomic desktop action.

    The model never receives the native coordinates.  Keeping the pair in one
    host function is important: a second model tool call could otherwise
    re-localize against a changed frame and turn an intended double-click into
    two unrelated clicks.
    """
    if FRAME_CACHE is None or not ACTIVE_DESKTOP_SESSION_ID:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []

    before_version = FRAME_CACHE.version
    for click_number in (1, 2):
        click_args = session_args_for(RAW_CLICK_TOOL, ACTIVE_DESKTOP_SESSION_ID)
        if not click_args:
            click_args = {"sessionId": ACTIVE_DESKTOP_SESSION_ID}
        click_args.update({"x": click_x, "y": click_y})
        click_result = await mcp.call_tool(RAW_CLICK_TOOL, click_args)
        if getattr(click_result, "is_error", False):
            return (
                {
                    "success": False,
                    "target": target,
                    "clicks_sent": click_number - 1,
                    "reason": result_text(click_result) or "solari_click returned an error",
                    "debug_image": debug_path,
                },
                [],
            )
        if click_number == 1:
            await asyncio.sleep(DOUBLE_CLICK_INTERVAL_SECONDS)

    post = await FRAME_CACHE.wait_for_change(
        before_version,
        timeout=RFB_POST_ACTION_TIMEOUT,
        stable_seconds=RFB_STABLE_SECONDS,
    )
    changed = post.version > before_version
    interval_ms = round(DOUBLE_CLICK_INTERVAL_SECONDS * 1000, 1)
    print(
        f"[Double-click input sent: target={target!r} point=({click_x},{click_y}) "
        f"interval_ms={interval_ms} source_frame=v{source_frame_version} "
        f"v{before_version}->v{post.version} changed={changed}]",
        flush=True,
    )
    return (
        {
            "success": True,
            "input_sent": True,
            "target": target,
            "click_count": 2,
            "double_click_interval_ms": interval_ms,
            "source_frame_version": source_frame_version,
            "post_frame_version": post.version,
            "frame_changed": changed,
            "semantic_success_unverified": True,
            "verification_instruction": (
                "Inspect the attached post-action frame and verify that the intended item opened or activated; "
                "frame_changed alone is not proof."
            ),
            "debug_image": debug_path,
        },
        [frame_to_clean_vision_image(post, "post_double_click")],
    )

async def _ground_target_on_snapshot(
    source: FrameSnapshot,
    target: str,
    region: str,
) -> GroundingResult:
    """Ground one semantic target against exactly one immutable framebuffer."""
    work_image, offset_x, offset_y = get_region_crop(source.image, region)
    result = await VISUAL_GROUNDER.ground(work_image, target, source.version)
    if not result.found:
        return result

    def shift_box(box):
        if box is None:
            return None
        return (box[0] + offset_x, box[1] + offset_y, box[2] + offset_x, box[3] + offset_y)

    def shift_point(point):
        if point is None:
            return None
        return (point[0] + offset_x, point[1] + offset_y)

    if not (offset_x or offset_y):
        return replace(result, source_frame_version=source.version)

    shifted_bbox = shift_box(result.bbox)
    shifted_geometry = shift_box(result.geometry_bbox)
    shifted_verifiers = tuple(shift_box(box) for box in result.verifier_boxes if box is not None)
    shifted_proposal = shift_box(result.proposal_bbox)
    shifted_click = shift_point(result.click_point)
    shifted = replace(
        result,
        bbox=shifted_bbox,
        click_point=shifted_click,
        geometry_bbox=shifted_geometry,
        verifier_boxes=shifted_verifiers,
        proposal_bbox=shifted_proposal,
        source_frame_version=source.version,
    )
    # Certification must survive only if every bound field stayed internally consistent.
    if result.geometry_verified and result.click_safe and not shifted.actionable:
        return GroundingResult(
            False, None, None, 0.0, "unresolved-v4r2-geometry", result.label,
            result.detail + "; region_offset_invalidated_geometry=true", source.version, result.ai_calls,
        )
    return shifted


async def _resolve_semantic_target(
    target: str,
    region: str,
) -> tuple[FrameSnapshot | None, FrameSnapshot | None, GroundingResult | None, str | None]:
    """Resolve a target and prove its pixels are still safe before input.

    A verified previous grounding is reused with zero vision calls when its local
    and global pixels remain stable. If pixels change while a new grounding is
    running, legacy automatically re-grounds on the newer frame once by default.
    """
    if FRAME_CACHE is None:
        return None, None, None, "No live desktop framebuffer is attached."
    if not target.strip():
        return None, None, None, "target is required"
    if region not in {"full", "top", "bottom", "left", "right", "center"}:
        region = "full"

    cache_key = normalize_text(target) + "|" + region
    current = await FRAME_CACHE.snapshot()
    cached = SEMANTIC_GROUNDING_CACHE.get(cache_key)
    max_age = max(0.0, float(os.getenv("VISUAL_TARGET_CANDIDATE_REUSE_MAX_AGE", "60")))
    if cached is not None:
        cached_source, cached_result, completed_at = cached
        age = time.monotonic() - completed_at
        if max_age <= 0 or age <= max_age:
            stable, frame_mean, frame_fraction, context_mean, context_fraction = grounding_frame_is_stable(
                cached_source.image, current.image, cached_result
            )
            if stable:
                print(
                    f"[toolkit-ground] reuse target={target!r} v{cached_source.version}->v{current.version} "
                    f"ai_calls=0 context_diff={context_mean:.2f}/{context_fraction:.3f}",
                    flush=True,
                )
                reused_result = replace(
                    cached_result,
                    detail=cached_result.detail + "; stable-cache-reuse",
                    ai_calls=0,
                )
                return cached_source, current, reused_result, None

    attempts = max(0, int(os.getenv("VISUAL_TARGET_STALE_REGROUND_ATTEMPTS", "1"))) + 1
    source = current
    for attempt in range(attempts):
        result = await _ground_target_on_snapshot(source, target, region)
        if not result.found or result.safe_point is None:
            return source, await FRAME_CACHE.snapshot(), result, "Visual grounding could not verify the requested target."
        current = await FRAME_CACHE.snapshot()
        if current.version == source.version:
            SEMANTIC_GROUNDING_CACHE[cache_key] = (source, result, time.monotonic())
            return source, current, result, None
        stable, frame_mean, frame_fraction, context_mean, context_fraction = grounding_frame_is_stable(
            source.image, current.image, result
        )
        if stable:
            print(
                f"[toolkit-ground] framebuffer advanced v{source.version}->v{current.version} but grounded context is stable "
                f"frame_diff={frame_mean:.2f}/{frame_fraction:.3f} "
                f"context_diff={context_mean:.2f}/{context_fraction:.3f}",
                flush=True,
            )
            SEMANTIC_GROUNDING_CACHE[cache_key] = (source, result, time.monotonic())
            return source, current, result, None
        if attempt + 1 < attempts:
            print(
                f"[toolkit-ground] target context changed during grounding v{source.version}->v{current.version}; "
                "re-grounding on current frame",
                flush=True,
            )
            source = current
            continue
        return source, current, result, "Target context materially changed during grounding; no click was sent."
    return source, current, None, "Visual grounding failed."


async def _solari_display_size() -> tuple[int, int]:
    if HOST_DESKTOP_CONTROL is None:
        raise RuntimeError("No Solari Desktop control handle is attached")
    raw = await HOST_DESKTOP_CONTROL.display.size()
    if not isinstance(raw, dict):
        raise RuntimeError(f"Solari display.size() returned unexpected type {type(raw).__name__}")
    try:
        width = int(raw.get("w") if raw.get("w") is not None else raw.get("width"))
        height = int(raw.get("h") if raw.get("h") is not None else raw.get("height"))
    except Exception as exc:
        raise RuntimeError(f"Solari display.size() did not return integer dimensions: {raw!r}") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Solari display.size() returned invalid dimensions {width}x{height}")
    return width, height


def _rfb_to_solari_xy(
    image: Image.Image,
    x_px: int,
    y_px: int,
    display_width: int,
    display_height: int,
) -> tuple[int, int]:
    """Map an exact RFB pixel to Solari's reported display coordinate space."""
    if image.width <= 1 or image.height <= 1:
        raise ValueError("Framebuffer is too small for coordinate mapping")
    x = round(x_px * max(0, display_width - 1) / max(1, image.width - 1))
    y = round(y_px * max(0, display_height - 1) / max(1, image.height - 1))
    return (
        max(0, min(x, display_width - 1)),
        max(0, min(y, display_height - 1)),
    )


async def _verify_solari_cursor_at(x: int, y: int) -> tuple[bool, dict[str, int] | None, str]:
    if HOST_DESKTOP_CONTROL is None:
        return False, None, "No Solari Desktop control handle is attached"
    try:
        await HOST_DESKTOP_CONTROL.mouse.move(x, y, humanize=False)
        await asyncio.sleep(0.05)
        raw = await HOST_DESKTOP_CONTROL.display.cursor()
        cx = int(raw.get("x")); cy = int(raw.get("y"))
    except Exception as exc:
        return False, None, f"Solari cursor verification failed: {type(exc).__name__}: {exc}"
    ok = abs(cx - x) <= NATIVE_CURSOR_TOLERANCE and abs(cy - y) <= NATIVE_CURSOR_TOLERANCE
    return ok, {"x": cx, "y": cy}, "" if ok else f"cursor mismatch requested=({x},{y}) reported=({cx},{cy})"


async def _agent_native_input(
    target: str,
    frame_version: int,
    x_px: int,
    y_px: int,
    *,
    double: bool,
) -> tuple[dict[str, Any], list[VisionImage]]:
    """Execute a point selected directly by the chat model on a presented frame.

    No OCR, CV, semantic grounding, bbox processing, grid mapping, or secondary
    model call occurs here. The host performs transport/safety validation only.
    """
    if not ACTIVE_DESKTOP_SESSION_ID or FRAME_CACHE is None or HOST_DESKTOP_CONTROL is None:
        return {"success": False, "reason": "No live Solari desktop/control framebuffer is attached."}, []

    try:
        requested_version = int(frame_version)
        frame_x = int(x_px)
        frame_y = int(y_px)
    except Exception:
        return {"success": False, "reason": "frame_version, x_px, and y_px must be integers."}, []

    reference = PRESENTED_FRAMES.get(requested_version)
    if reference is None:
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "reason": f"Framebuffer v{requested_version} is no longer in host history. Use the attached current frame and its frame_version.",
            "requested_frame_version": requested_version,
            "current_frame_version": current.version,
            "host_visual_localization_calls": 0,
        }, [frame_to_clean_vision_image(current, "native_frame_missing")])

    width, height = reference.image.size
    if not (0 <= frame_x < width and 0 <= frame_y < height):
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "reason": f"Native point ({frame_x},{frame_y}) is outside framebuffer v{requested_version} bounds {width}x{height}.",
            "rfb_resolution": [width, height],
            "host_visual_localization_calls": 0,
        }, [frame_to_clean_vision_image(current, "native_point_out_of_bounds")])

    current = await FRAME_CACHE.snapshot()
    if current.image.size != reference.image.size:
        return ({
            "success": False,
            "reason": "Framebuffer dimensions changed since the referenced image; no input was sent.",
            "referenced_resolution": list(reference.image.size),
            "current_resolution": list(current.image.size),
            "current_frame_version": current.version,
            "host_visual_localization_calls": 0,
        }, [frame_to_clean_vision_image(current, "native_resolution_changed")])

    stale_metrics = None
    if current.version != reference.version:
        mean_diff, changed_fraction, box = _point_region_change_metrics(
            reference.image, current.image, x=frame_x, y=frame_y
        )
        stale_metrics = {
            "mean_diff": round(mean_diff, 3),
            "changed_fraction": round(changed_fraction, 6),
            "region": list(box),
        }
        if (
            changed_fraction >= POINT_STALE_CHANGED_FRACTION
            or mean_diff >= POINT_STALE_MEAN_DIFF
        ):
            return ({
                "success": False,
                "reason": (
                    f"The clicked neighborhood changed between framebuffer v{reference.version} and v{current.version}; "
                    "inspect the attached current frame and choose a new native point."
                ),
                "requested_point_rfb": {"x": frame_x, "y": frame_y},
                "stale_metrics": stale_metrics,
                "host_visual_localization_calls": 0,
            }, [frame_to_clean_vision_image(current, "native_point_stale")])
        print(
            f"[toolkit-native] frame advanced v{reference.version}->v{current.version} but requested point context is stable "
            f"mean={mean_diff:.2f} fraction={changed_fraction:.3f}",
            flush=True,
        )

    display_width, display_height = await _solari_display_size()
    click_x, click_y = _rfb_to_solari_xy(
        reference.image, frame_x, frame_y, display_width, display_height
    )

    cursor_report = None
    if NATIVE_CURSOR_VERIFY:
        cursor_ok, cursor_report, cursor_error = await _verify_solari_cursor_at(click_x, click_y)
        if not cursor_ok:
            current = await FRAME_CACHE.snapshot()
            return ({
                "success": False,
                "reason": cursor_error,
                "target": target,
                "rfb_resolution": [width, height],
                "agent_point_rfb": {"x": frame_x, "y": frame_y},
                "solari_display_resolution": [display_width, display_height],
                "mapped_solari_point": {"x": click_x, "y": click_y},
                "reported_cursor": cursor_report,
                "host_visual_localization_calls": 0,
            }, [frame_to_clean_vision_image(current, "native_cursor_mismatch")])

    before_version = FRAME_CACHE.version
    try:
        if double:
            await HOST_DESKTOP_CONTROL.mouse.double_click(click_x, click_y)
        else:
            await HOST_DESKTOP_CONTROL.mouse.click(click_x, click_y, humanize=False)
    except Exception as exc:
        return ({
            "success": False,
            "reason": f"Solari Desktop native mouse input failed: {type(exc).__name__}: {exc}",
            "target": target,
            "host_visual_localization_calls": 0,
        }, [])

    _invalidate_semantic_grounding_cache("toolkit-agent-native-input")
    _invalidate_grid_fallbacks("toolkit-agent-native-input")
    post = await FRAME_CACHE.wait_for_change(before_version, timeout=3.0, stable_seconds=0.10)
    changed = post.version > before_version
    action = "double-click" if double else "click"
    scale_x = display_width / width
    scale_y = display_height / height
    print(
        f"[toolkit-native] {action} target={target!r} frame=v{reference.version} "
        f"rfb={width}x{height} agent_point=({frame_x},{frame_y}) "
        f"display={display_width}x{display_height} mapped=({click_x},{click_y}) "
        f"scale=({scale_x:.6f},{scale_y:.6f}) cursor={cursor_report} "
        f"v{before_version}->v{post.version} changed={changed} host_cv_calls=0",
        flush=True,
    )
    return ({
        "success": True,
        "input_sent": True,
        "target": target,
        "mode": "agent-native-coordinate",
        "source_frame_version": reference.version,
        "rfb_resolution": [width, height],
        "agent_point_rfb": {"x": frame_x, "y": frame_y},
        "solari_display_resolution": [display_width, display_height],
        "mapped_solari_point": {"x": click_x, "y": click_y},
        "cursor_verified": bool(NATIVE_CURSOR_VERIFY),
        "reported_cursor": cursor_report,
        "stale_metrics": stale_metrics,
        "frame_changed": changed,
        "post_frame_version": post.version,
        "host_visual_localization_calls": 0,
        "semantic_success_unverified": True,
        "verification_instruction": "Inspect the attached post-action framebuffer and verify the intended visible effect yourself.",
    }, [frame_to_clean_vision_image(post, "post_agent_native_double_click" if double else "post_agent_native_click")])


async def desktop_click_native(
    target: str,
    frame_version: int,
    x_px: int,
    y_px: int,
) -> tuple[dict[str, Any], list[VisionImage]]:
    return await _agent_native_input(target, frame_version, x_px, y_px, double=False)


async def desktop_double_click_native(
    target: str,
    frame_version: int,
    x_px: int,
    y_px: int,
) -> tuple[dict[str, Any], list[VisionImage]]:
    return await _agent_native_input(target, frame_version, x_px, y_px, double=True)


async def _direct_point_for_target(
    source: FrameSnapshot,
    target: str,
) -> tuple[DirectVisionPoint | None, FrameSnapshot, str | None]:
    if FRAME_CACHE is None:
        return None, source, "No live desktop framebuffer is attached."
    point = await DIRECT_LOCATOR.locate(source.image, target, source.version)
    if not point.found or not point.in_bounds or point.x_px is None or point.y_px is None:
        current = await FRAME_CACHE.snapshot()
        return point, current, "Direct vision did not return a valid native framebuffer point."
    current = await FRAME_CACHE.snapshot()
    if current.version != source.version:
        mean_diff, changed_fraction, _box = _point_region_change_metrics(
            source.image, current.image, x=point.x_px, y=point.y_px
        )
        stale = (
            changed_fraction >= POINT_STALE_CHANGED_FRACTION
            or mean_diff >= POINT_STALE_MEAN_DIFF
        )
        if stale:
            return point, current, (
                f"Target neighborhood changed during direct vision localization "
                f"(mean={mean_diff:.2f}, fraction={changed_fraction:.3f}); no click was sent."
            )
    return point, current, None


async def _direct_input(
    target: str,
    *,
    double: bool,
) -> tuple[dict[str, Any], list[VisionImage]]:
    if not ACTIVE_DESKTOP_SESSION_ID or FRAME_CACHE is None or HOST_DESKTOP_CONTROL is None:
        return {"success": False, "reason": "No live Solari desktop/control framebuffer is attached."}, []
    source = await FRAME_CACHE.snapshot()
    started = time.monotonic()
    point, current, error = await _direct_point_for_target(source, target)
    if point is None or not point.found or point.x_px is None or point.y_px is None:
        return ({
            "success": False,
            "reason": error or "Direct vision target unresolved.",
            "target": target,
            "frame_version": source.version,
            "vision_model": getattr(point, "model", DIRECT_LOCATOR.model),
            "confidence": getattr(point, "confidence", 0.0),
            "description": getattr(point, "description", ""),
            "mode": "direct-native-pixel",
        }, [frame_to_clean_vision_image(current, "direct_vision_unresolved")])
    if error:
        return ({
            "success": False,
            "reason": error,
            "target": target,
            "frame_version": source.version,
            "vision_point": {"x_px": point.x_px, "y_px": point.y_px},
            "mode": "direct-native-pixel",
        }, [frame_to_clean_vision_image(current, "direct_vision_stale")])

    display_width, display_height = await _solari_display_size()
    click_x, click_y = _rfb_to_solari_xy(
        source.image, point.x_px, point.y_px, display_width, display_height
    )
    cursor_report = None
    if NATIVE_CURSOR_VERIFY:
        cursor_ok, cursor_report, cursor_error = await _verify_solari_cursor_at(click_x, click_y)
        if not cursor_ok:
            return ({
                "success": False,
                "reason": cursor_error,
                "target": target,
                "rfb_resolution": [source.image.width, source.image.height],
                "solari_display_resolution": [display_width, display_height],
                "vision_point_rfb": {"x": point.x_px, "y": point.y_px},
                "mapped_solari_point": {"x": click_x, "y": click_y},
                "reported_cursor": cursor_report,
                "mode": "direct-native-pixel",
            }, [frame_to_clean_vision_image(current, "direct_cursor_mismatch")])

    debug_path = save_point_debug_image(
        source.image,
        frame_x=point.x_px,
        frame_y=point.y_px,
        norm_x=point.x_px,
        norm_y=point.y_px,
        target=f"direct direct {target}",
        frame_version=source.version,
    )
    before_version = FRAME_CACHE.version
    try:
        if double:
            await HOST_DESKTOP_CONTROL.mouse.double_click(click_x, click_y)
        else:
            await HOST_DESKTOP_CONTROL.mouse.click(click_x, click_y, humanize=False)
    except Exception as exc:
        return ({
            "success": False,
            "reason": f"Solari Desktop direct mouse input failed: {type(exc).__name__}: {exc}",
            "target": target,
            "debug_image": debug_path,
        }, [])

    _invalidate_semantic_grounding_cache("direct-direct-input")
    _invalidate_grid_fallbacks("direct-direct-input")
    post = await FRAME_CACHE.wait_for_change(before_version, timeout=3.0, stable_seconds=0.10)
    changed = post.version > before_version
    elapsed = time.monotonic() - started
    action = "double-click" if double else "click"
    scale_x = display_width / source.image.width
    scale_y = display_height / source.image.height
    print(
        f"[toolkit-native] {action} target={target!r} model={point.model} "
        f"frame=v{source.version} rfb={source.image.width}x{source.image.height} "
        f"vision=({point.x_px},{point.y_px}) display={display_width}x{display_height} "
        f"mapped=({click_x},{click_y}) scale=({scale_x:.6f},{scale_y:.6f}) "
        f"cursor={cursor_report} conf={point.confidence:.2f} v{before_version}->v{post.version} "
        f"changed={changed} latency={elapsed:.2f}s",
        flush=True,
    )
    return ({
        "success": True,
        "input_sent": True,
        "target": target,
        "mode": "direct-native-pixel",
        "vision_model": point.model,
        "confidence": round(point.confidence, 3),
        "description": point.description,
        "source_frame_version": source.version,
        "rfb_resolution": [source.image.width, source.image.height],
        "vision_point_rfb": {"x": point.x_px, "y": point.y_px},
        "solari_display_resolution": [display_width, display_height],
        "mapped_solari_point": {"x": click_x, "y": click_y},
        "cursor_verified": bool(NATIVE_CURSOR_VERIFY),
        "reported_cursor": cursor_report,
        "frame_changed": changed,
        "post_frame_version": post.version,
        "localization_latency_seconds": round(elapsed, 3),
        "semantic_success_unverified": True,
        "verification_instruction": "Inspect the attached post-action frame. Input delivery and framebuffer change do not alone prove semantic success.",
        "debug_image": debug_path,
    }, [frame_to_clean_vision_image(post, "post_direct_double_click" if double else "post_direct_click")])


async def desktop_click_target(
    mcp,
    target: str,
    region: str = "full",
) -> tuple[dict[str, Any], list[VisionImage]]:
    # direct experimental mode deliberately ignores the old region/CV pipeline.
    # Qwen receives the exact full raw framebuffer and returns one native pixel.
    return await _direct_input(target, double=False)


async def desktop_double_click_target(
    mcp,
    target: str,
    region: str = "full",
) -> tuple[dict[str, Any], list[VisionImage]]:
    return await _direct_input(target, double=True)



def _normalized_region_to_pixel_box(image: Image.Image, x1: float, y1: float, x2: float, y2: float) -> tuple[int, int, int, int]:
    values = [float(x1), float(y1), float(x2), float(y2)]
    if any(value < 0 or value > VISUAL_COORD_MAX for value in values):
        raise ValueError(f"region coordinates must be within 0..{VISUAL_COORD_MAX}")
    left_n, right_n = sorted((values[0], values[2]))
    top_n, bottom_n = sorted((values[1], values[3]))
    if right_n - left_n < 2 or bottom_n - top_n < 2:
        raise ValueError("precision crop region is too small")
    left = max(0, min(image.width - 1, round(left_n / VISUAL_COORD_MAX * (image.width - 1))))
    top = max(0, min(image.height - 1, round(top_n / VISUAL_COORD_MAX * (image.height - 1))))
    right = max(left + 1, min(image.width, round(right_n / VISUAL_COORD_MAX * (image.width - 1)) + 1))
    bottom = max(top + 1, min(image.height, round(bottom_n / VISUAL_COORD_MAX * (image.height - 1)) + 1))
    return left, top, right, bottom


def _region_change_metrics(reference: Image.Image, current: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, float]:
    a = reference.crop(box).convert("RGB")
    b = current.crop(box).convert("RGB")
    if a.size != b.size:
        return 255.0, 1.0
    diff = ImageChops.difference(a, b).convert("L")
    mean_diff = float(ImageStat.Stat(diff).mean[0])
    hist = diff.histogram()
    threshold = max(1, min(255, POINT_STALE_PIXEL_THRESHOLD))
    changed_pixels = sum(hist[threshold + 1:])
    return mean_diff, changed_pixels / max(1, diff.width * diff.height)


def _crop_to_precision_vision_image(full_snapshot: FrameSnapshot, box: tuple[int, int, int, int], label: str) -> VisionImage:
    crop = full_snapshot.image.crop(box).convert("RGB")
    scale = max(1.0, PRECISION_CROP_SCALE)
    enlarged = crop.resize(
        (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
        Image.Resampling.LANCZOS,
    )
    buffer = io.BytesIO()
    enlarged.save(buffer, format="JPEG", quality=max(50, min(100, PRECISION_CROP_JPEG_QUALITY)), optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    saved_path = None
    if SAVE_STREAM_FRAMES:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:80] or "precision"
        path = OUTPUT_DIR / f"precision_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_v{full_snapshot.version}.jpg"
        path.write_bytes(buffer.getvalue())
        saved_path = str(path.resolve())
    return VisionImage(
        label=label,
        mime_type="image/jpeg",
        base64_data=encoded,
        saved_path=saved_path,
        frame_version=full_snapshot.version,
        width=enlarged.width,
        height=enlarged.height,
        is_full_frame=False,
        source_bbox=box,
        source_frame_width=full_snapshot.image.width,
        source_frame_height=full_snapshot.image.height,
    )


async def desktop_inspect_region_legacy(turn_state: TurnState, target: str, x1: float, y1: float, x2: float, y2: float, frame_version: int) -> tuple[dict[str, Any], list[VisionImage]]:
    """Return an enlarged current crop for exact-value reading; no OCR is used."""
    if FRAME_CACHE is None:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []
    try:
        requested_version = int(frame_version)
        reference = PRESENTED_FRAMES.get(requested_version)
        if reference is None:
            current = await FRAME_CACHE.snapshot()
            return ({
                "success": False,
                "reason": f"Frame v{requested_version} is no longer available; localize on the attached current frame.",
                "current_frame_version": current.version,
            }, [frame_to_vision_image(current, "precision_frame_expired")])
        box = _normalized_region_to_pixel_box(reference.image, x1, y1, x2, y2)
    except (TypeError, ValueError) as exc:
        return {"success": False, "reason": str(exc)}, []

    current = await FRAME_CACHE.snapshot()
    if current.image.size != reference.image.size:
        return ({"success": False, "reason": "Desktop resolution changed; choose the region on a current full frame."},
                [frame_to_vision_image(current, "precision_resolution_changed")])

    if current.version != requested_version:
        stale_mean, stale_fraction = _region_change_metrics(reference.image, current.image, box)
        if stale_fraction >= POINT_STALE_CHANGED_FRACTION or stale_mean >= POINT_STALE_MEAN_DIFF:
            print(
                f"[Precision crop rejected: stale region frame=v{requested_version} current=v{current.version} "
                f"mean_diff={stale_mean:.1f} changed_fraction={stale_fraction:.3f}]",
                flush=True,
            )
            return ({
                "success": False,
                "reason": "The requested evidence region changed after the referenced frame. Re-localize on the attached current frame.",
                "requested_frame_version": requested_version,
                "current_frame_version": current.version,
            }, [frame_to_vision_image(current, "precision_stale_region")])

    # Crop the exact full frame the model localized against. If the current desktop
    # advanced, the region-change check above already proved this area remained
    # materially unchanged. This keeps precision evidence tied to one immutable frame.
    precision = _crop_to_precision_vision_image(reference, box, target or "precision_region")
    turn_state.precision_inspections += 1
    turn_state.precision_last_frame = requested_version
    turn_state.precision_last_target = target or "precision region"
    print(
        f"[Precision visual inspection: target={target!r} frame=v{requested_version} current=v{current.version} bbox={box} "
        f"crop={precision.width}x{precision.height} check={turn_state.precision_inspections}]",
        flush=True,
    )
    return ({
        "success": True,
        "target": target,
        "frame_version": requested_version,
        "current_frame_version": current.version,
        "source_bbox_pixels": list(box),
        "precision_inspection_count": turn_state.precision_inspections,
        "instruction": (
            "Read this enlarged crop carefully. Preserve exact digits, punctuation, and currency markers as shown. "
            "If any character is ambiguous, request another/larger precision crop rather than guessing."
        ),
    }, [precision])


TEXTUAL_POINT_HINT_RE = re.compile(
    r"\b(search\s*box|text\s*box|textbox|input(?:\s*field)?|text\s*field|address\s*bar|omnibox|placeholder|labeled)\b",
    re.IGNORECASE,
)


def _point_region_change_metrics(
    reference: Image.Image,
    current: Image.Image,
    *,
    x: int,
    y: int,
) -> tuple[float, float, tuple[int, int, int, int]]:
    radius = max(16, POINT_STALE_REGION_RADIUS)
    left = max(0, x - radius)
    top = max(0, y - radius)
    right = min(reference.width, x + radius + 1)
    bottom = min(reference.height, y + radius + 1)
    box = (left, top, right, bottom)
    a = reference.crop(box).convert("RGB")
    b = current.crop(box).convert("RGB")
    diff = ImageChops.difference(a, b).convert("L")
    mean_diff = float(ImageStat.Stat(diff).mean[0])
    hist = diff.histogram()
    threshold = max(1, min(255, POINT_STALE_PIXEL_THRESHOLD))
    changed_pixels = sum(hist[threshold + 1 :])
    total = max(1, diff.width * diff.height)
    return mean_diff, changed_pixels / total, box



def _parse_grid_cell(cell: str, cols: int, rows: int) -> tuple[int, int]:
    value = (cell or "").strip().upper()
    match = re.fullmatch(r"([A-Z])(\d{1,2})", value)
    if not match:
        raise ValueError(f"Invalid grid cell {cell!r}; expected format like B7 or D3")
    row = ord(match.group(1)) - ord("A")
    col = int(match.group(2)) - 1
    if not (0 <= row < rows and 0 <= col < cols):
        raise ValueError(
            f"Grid cell {value} is outside this grid (rows A-{chr(ord('A') + rows - 1)}, columns 1-{cols})"
        )
    return row, col


def _grid_cell_bbox(base_bbox: tuple[int, int, int, int], cols: int, rows: int, cell: str) -> tuple[int, int, int, int]:
    row, col = _parse_grid_cell(cell, cols, rows)
    left0, top0, right0, bottom0 = base_bbox
    width = right0 - left0
    height = bottom0 - top0
    left = left0 + round(col * width / cols)
    right = left0 + round((col + 1) * width / cols)
    top = top0 + round(row * height / rows)
    bottom = top0 + round((row + 1) * height / rows)
    right = max(left + 1, right)
    bottom = max(top + 1, bottom)
    return left, top, right, bottom


def _remember_zoom(context: ZoomContext) -> None:
    ZOOM_CONTEXTS[context.zoom_id] = context
    while len(ZOOM_CONTEXTS) > max(1, ZOOM_CONTEXT_LIMIT):
        oldest = next(iter(ZOOM_CONTEXTS))
        ZOOM_CONTEXTS.pop(oldest, None)


def _target_ocr_candidates(description: str) -> list[str]:
    """Extract conservative visible-label candidates from a visual target description."""
    text = (description or "").strip()
    if not text:
        return []
    out: list[str] = []
    for quoted in re.findall(r"['\"]([^'\"]{2,80})['\"]", text):
        out.append(quoted.strip())
    # "OK button on ..." -> "OK"; "Got it button ..." -> "Got it".
    match = re.match(
        r"^(.{2,60}?)\s+(?:button|link|menu\s*item|menu|tab|field|input|textbox|text\s*box|label)\b",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        prefix = match.group(1).strip(" :-")
        if not re.search(r"\b(icon|magnifier|magnifying|hamburger|checkbox|radio|image|carousel|glyph)\b", prefix, re.I):
            out.append(prefix)
    # Deduplicate and reject generic UI words.
    generic = {"button", "link", "menu", "tab", "field", "input", "search", "icon", "close", "popup"}
    unique: list[str] = []
    for candidate in out:
        norm = normalize_text(candidate)
        if not norm or norm in generic:
            continue
        if sum(ch.isalnum() for ch in norm) < OCR_MIN_TARGET_ALNUM_CHARS:
            continue
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _grid_source_context(
    *,
    frame_version: int | None,
    zoom_id: str | None,
) -> tuple[FrameSnapshot, tuple[int, int, int, int], int, int, int]:
    if zoom_id:
        context = ZOOM_CONTEXTS.get(str(zoom_id))
        if context is None:
            raise ValueError(f"Unknown/expired zoom_id {zoom_id!r}; request a fresh zoom")
        reference = PRESENTED_FRAMES.get(context.frame_version)
        if reference is None:
            raise ValueError(f"The source frame for zoom {zoom_id!r} expired; refresh and re-localize")
        return reference, context.source_bbox, ZOOM_GRID_COLS, ZOOM_GRID_ROWS, context.depth

    if frame_version is None:
        raise ValueError("frame_version is required when using the full desktop grid")
    reference = PRESENTED_FRAMES.get(int(frame_version))
    if reference is None:
        raise ValueError(f"Frame v{frame_version} is no longer in visual history; refresh and re-localize")
    return reference, (0, 0, reference.image.width, reference.image.height), FULL_GRID_COLS, FULL_GRID_ROWS, 0


def _clean_precision_from_bbox(snapshot: FrameSnapshot, box: tuple[int, int, int, int], target: str) -> VisionImage:
    crop = snapshot.image.crop(box).convert("RGB")
    scale = max(2, GRID_ZOOM_SCALE)
    enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    return _encode_vision_pil(
        enlarged,
        label="precision_cell",
        frame_version=snapshot.version,
        is_full_frame=False,
        source_bbox=box,
        source_frame_width=snapshot.image.width,
        source_frame_height=snapshot.image.height,
        grid_kind="precision",
    )


async def desktop_zoom_region(
    target: str,
    cell: str,
    frame_version: int | None = None,
    source_zoom_id: str | None = None,
    fallback_token: str | None = None,
) -> tuple[dict[str, Any], list[VisionImage]]:
    if FRAME_CACHE is None:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []
    current = await FRAME_CACHE.snapshot()
    if not fallback_token:
        return ({
            "success": False,
            "reason": "Legacy grid zoom is host-gated. Retry desktop_click_target for this target and use the returned fallback_token only if semantic grounding is unresolved.",
        }, [frame_to_clean_vision_image(current, "zoom_unauthorized")])
    try:
        reference, base_bbox, cols, rows, depth = _grid_source_context(
            frame_version=frame_version,
            zoom_id=source_zoom_id,
        )
        box = _grid_cell_bbox(base_bbox, cols, rows, cell)
    except (TypeError, ValueError) as exc:
        return {"success": False, "reason": str(exc)}, [frame_to_clean_vision_image(current, "zoom_invalid")]
    authorized, auth_reason = _validate_grid_fallback_authorization(fallback_token, target, reference.version)
    if not authorized:
        return {"success": False, "reason": auth_reason}, [frame_to_clean_vision_image(current, "zoom_unauthorized")]

    if current.image.size != reference.image.size:
        return ({"success": False, "reason": "Desktop resolution changed; re-localize on the current frame."},
                [frame_to_vision_image(current, "zoom_resolution_changed")])
    if current.version != reference.version:
        mean_diff, changed_fraction = _region_change_metrics(reference.image, current.image, box)
        if changed_fraction >= GRID_STALE_CHANGED_FRACTION or mean_diff >= GRID_STALE_MEAN_DIFF:
            return ({
                "success": False,
                "reason": "That grid region changed after the referenced image. Re-localize on the attached current frame.",
                "requested_frame_version": reference.version,
                "current_frame_version": current.version,
                "local_change_mean": round(mean_diff, 2),
                "local_changed_fraction": round(changed_fraction, 4),
            }, [frame_to_vision_image(current, "zoom_stale")])

    crop = reference.image.crop(box).convert("RGB")
    scale = max(2, GRID_ZOOM_SCALE)
    enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    gridded = _draw_grid_overlay(enlarged, ZOOM_GRID_COLS, ZOOM_GRID_ROWS)
    zoom_id = f"z{reference.version}_{int(time.time() * 1000) % 100000000}_{depth + 1}"
    _remember_zoom(ZoomContext(zoom_id, reference.version, box, depth + 1, time.monotonic()))
    vision = _encode_vision_pil(
        gridded,
        label="zoom_grid",
        frame_version=reference.version,
        is_full_frame=False,
        source_bbox=box,
        source_frame_width=reference.image.width,
        source_frame_height=reference.image.height,
        grid_kind="zoom",
        grid_cols=ZOOM_GRID_COLS,
        grid_rows=ZOOM_GRID_ROWS,
        zoom_id=zoom_id,
    )
    print(f"[Grid zoom: target={target!r} cell={cell.upper()} zoom_id={zoom_id} source_bbox={box} depth={depth + 1}]", flush=True)
    return ({
        "success": True,
        "target": target,
        "cell": cell.upper(),
        "zoom_id": zoom_id,
        "fallback_token": fallback_token,
        "frame_version": reference.version,
        "source_bbox_pixels": list(box),
        "fine_grid": {
            "rows": f"A-{chr(ord('A') + ZOOM_GRID_ROWS - 1)}",
            "columns": f"1-{ZOOM_GRID_COLS}",
        },
        "instruction": "Choose a fine-grid cell. For a tiny target you may zoom again before clicking.",
    }, [vision])


async def desktop_inspect_cell(
    turn_state: TurnState,
    target: str,
    cell: str,
    frame_version: int | None = None,
    zoom_id: str | None = None,
) -> tuple[dict[str, Any], list[VisionImage]]:
    if FRAME_CACHE is None:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []
    try:
        reference, base_bbox, cols, rows, _depth = _grid_source_context(frame_version=frame_version, zoom_id=zoom_id)
        box = _grid_cell_bbox(base_bbox, cols, rows, cell)
    except (TypeError, ValueError) as exc:
        current = await FRAME_CACHE.snapshot()
        return {"success": False, "reason": str(exc)}, [frame_to_vision_image(current, "inspect_invalid")]

    current = await FRAME_CACHE.snapshot()
    if current.version != reference.version:
        mean_diff, changed_fraction = _region_change_metrics(reference.image, current.image, box)
        if changed_fraction >= GRID_STALE_CHANGED_FRACTION or mean_diff >= GRID_STALE_MEAN_DIFF:
            return ({
                "success": False,
                "reason": "The evidence cell changed after the referenced image. Re-localize before reading an exact value.",
                "requested_frame_version": reference.version,
                "current_frame_version": current.version,
            }, [frame_to_vision_image(current, "inspect_stale")])

    precision = _clean_precision_from_bbox(reference, box, target)
    turn_state.precision_inspections += 1
    turn_state.precision_last_frame = reference.version
    turn_state.precision_last_target = target
    print(
        f"[Precision cell inspection: target={target!r} cell={cell.upper()} frame=v{reference.version} "
        f"zoom_id={zoom_id!r} bbox={box} check={turn_state.precision_inspections}]",
        flush=True,
    )
    return ({
        "success": True,
        "target": target,
        "cell": cell.upper(),
        "zoom_id": zoom_id,
        "frame_version": reference.version,
        "source_bbox_pixels": list(box),
        "precision_inspection_count": turn_state.precision_inspections,
        "instruction": "Read exact visible text/numbers/currency from the clean enlarged crop. Do not infer missing characters.",
    }, [precision])


async def _host_active_x11_window_name() -> str:
    """Return the active Linux/X11 top-level window name, or empty if unavailable."""
    if HOST_DESKTOP_CONTROL is None:
        return ""
    commands = getattr(HOST_DESKTOP_CONTROL, "commands", None)
    runner = getattr(commands, "run", None) if commands is not None else None
    if not callable(runner):
        return ""
    try:
        result = await runner(
            "/bin/sh",
            args=["-c", "W=$(xdotool getactivewindow 2>/dev/null) || exit 0; xdotool getwindowname \"$W\" 2>/dev/null || true"],
            env={"DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/desktop"},
            user="desktop",
        )
        if isinstance(result, dict):
            return str(result.get("stdout", "") or "").strip()
        return str(getattr(result, "stdout", "") or "").strip()
    except Exception:
        return ""


def _normalize_hotkey_scope(scope: str) -> str:
    value = str(scope or "").strip().lower().replace("-", "_")
    aliases = {"remote": "remote_rdp", "rdp": "remote_rdp", "windows": "remote_rdp", "host": "local", "linux": "local"}
    return aliases.get(value, value)


def _normalized_hotkey_tuple(keys: list[str]) -> tuple[str, ...]:
    return tuple(
        str(k).strip().casefold().replace("pagedown", "page_down").replace("pageup", "page_up")
        for k in keys if str(k).strip()
    )


_REMOTE_RDP_UNFORWARDABLE_CHORDS: set[tuple[str, ...]] = {
    ("alt", "tab"),
    ("alt", "shift", "tab"),
}


async def _sdk_hotkey(keys: list[str]) -> None:
    if HOST_DESKTOP_CONTROL is None:
        raise RuntimeError("Desktop SDK control handle is not available")
    normalized = [_sdk_key_name(k) for k in keys if str(k).strip()]
    if not normalized:
        raise ValueError("keys cannot be empty")
    # Route all keys/chords through the same host path so Ctrl+A, Backspace,
    # navigation keys, and browser shortcuts share identical semantics.
    await _host_nav_press(*normalized)


_GENERIC_HOTKEYS: set[tuple[str, ...]] = {
    ("ctrl", "l"), ("ctrl", "t"), ("ctrl", "a"),
    ("ctrl", "c"), ("ctrl", "x"), ("ctrl", "v"),
    ("ctrl", "z"), ("ctrl", "y"),
    ("alt", "tab"), ("alt", "left"), ("alt", "right"),
    ("shift", "tab"),
    ("enter",), ("esc",), ("escape",), ("tab",),
    ("page_down",), ("page_up",), ("home",), ("end",),
    ("left",), ("right",), ("up",), ("down",),
    ("backspace",), ("delete",), ("space",),
}


def _hotkey_explicitly_requested(turn_state: TurnState, keys: list[str]) -> bool:
    request = (turn_state.user_request or "").casefold()
    normalized = tuple(str(k).strip().casefold().replace("pagedown", "page_down").replace("pageup", "page_up") for k in keys if str(k).strip())
    if not normalized:
        return False
    variants = {
        "+".join(normalized),
        " + ".join(normalized),
        "-".join(normalized),
        " ".join(normalized),
    }
    if len(normalized) == 1:
        variants.add(normalized[0])
    return any(variant in request for variant in variants)


def _hotkey_authorized(turn_state: TurnState, keys: list[str]) -> tuple[bool, str]:
    normalized = tuple(str(k).strip().casefold().replace("pagedown", "page_down").replace("pageup", "page_up") for k in keys if str(k).strip())
    if normalized in _GENERIC_HOTKEYS:
        return True, ""
    if _hotkey_explicitly_requested(turn_state, keys):
        return True, ""
    chord = "+".join(normalized) or "<empty>"
    return (
        False,
        f"Hotkey {chord!r} is not a host-approved generic shortcut and was not explicitly requested by the human. "
        "Use visible UI controls instead of inventing application-specific shortcuts.",
    )


async def desktop_hotkey(
    turn_state: TurnState,
    keys: list[str],
    purpose: str = "",
    scope: str = "",
) -> tuple[dict[str, Any], list[VisionImage]]:
    if FRAME_CACHE is None:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []
    if not isinstance(keys, list) or not keys:
        return {"success": False, "reason": "keys must be a non-empty array"}, []
    scope = _normalize_hotkey_scope(scope)
    normalized = _normalized_hotkey_tuple([str(k) for k in keys])
    chord = "+".join(normalized) or "<empty>"
    target_key, family_key = _hotkey_action_keys(scope or "missing", [str(k) for k in keys])
    target = f"{scope or 'missing'} hotkey {chord}" + (f" for {purpose}" if purpose else "")

    if scope not in {"local", "remote_rdp"}:
        _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="scope_rejected")
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "scope_required": True,
            "reason": "desktop_hotkey requires explicit scope=local or scope=remote_rdp in Toolkit.",
            "instruction": "Use local only for the Linux/Solari desktop; use remote_rdp only for keys intended inside the focused Windows Remmina session.",
        }, [frame_to_clean_vision_image(current, "hotkey_scope_rejected")])

    allowed_loop, loop_reason, _ = _semantic_loop_guard(
        turn_state,
        "desktop_hotkey",
        target,
        target_key=target_key,
        family_key=family_key,
        same_limit=TOOLKIT_HOTKEY_REPEAT_LIMIT,
    )
    if not allowed_loop:
        turn_state.semantic_loop_rejections += 1
        current = await FRAME_CACHE.snapshot()
        print(f"[toolkit-loop-guard] rejected hotkey key={target_key!r}: {loop_reason}", flush=True)
        return ({
            "success": False,
            "semantic_loop_detected": True,
            "reason": loop_reason,
            "instruction": "Do not keep retrying the same shortcut. Inspect the current screen and use a different interaction path.",
        }, [frame_to_clean_vision_image(current, "hotkey_loop_rejected")])

    if scope == "remote_rdp" and normalized in _REMOTE_RDP_UNFORWARDABLE_CHORDS:
        _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="remote_unavailable")
        current = await FRAME_CACHE.snapshot()
        reason = (
            "Remote Alt+Tab is unavailable through the current Remmina automation path. A normal Alt+Tab is intercepted by the local XFCE window manager, "
            "and direct synthetic X11 delivery to the winserver window was verified to produce no remote framebuffer change."
        )
        print(f"[toolkit-hotkey] rejected remote_rdp chord={chord!r} reason=host-window-manager-intercept", flush=True)
        return ({
            "success": False,
            "remote_hotkey_unavailable": True,
            "scope": scope,
            "keys": keys,
            "reason": reason,
            "instruction": (
                "Do NOT retry Alt+Tab and do NOT use scope=local as a workaround. Switch remote Windows applications through the visible remote taskbar. "
                "For an unlabeled taskbar button use evidence_basis=spatial_control, describe only its visible position, click one slot once, and verify the resulting window."
            ),
        }, [frame_to_clean_vision_image(current, "remote_alt_tab_rejected")])

    if scope == "remote_rdp":
        active_name = await _host_active_x11_window_name()
        if normalize_text(active_name) != "winserver":
            _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="wrong_focus")
            current = await FRAME_CACHE.snapshot()
            print(f"[toolkit-hotkey] rejected remote_rdp chord={chord!r} active_x11={active_name!r}", flush=True)
            return ({
                "success": False,
                "remote_focus_required": True,
                "scope": scope,
                "keys": keys,
                "active_local_window": active_name or "unknown",
                "reason": "The local active X11 window is not the Remmina winserver session, so sending remote-scoped keys could affect the wrong application.",
                "instruction": "Click visibly inside the winserver RDP canvas to focus it, verify that the remote Windows screen is active, then retry the remote_rdp key if still needed.",
            }, [frame_to_clean_vision_image(current, "remote_hotkey_focus_rejected")])

    authorized, reason = _hotkey_authorized(turn_state, [str(k) for k in keys])
    if not authorized:
        _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="unauthorized")
        current = await FRAME_CACHE.snapshot()
        print(f"[toolkit-hotkey] rejected keys={keys!r} scope={scope} reason=not-user-authorized-app-shortcut", flush=True)
        return {"success": False, "reason": reason, "keys": keys, "scope": scope}, [frame_to_clean_vision_image(current, "hotkey_unauthorized")]

    before = await FRAME_CACHE.snapshot()
    try:
        await _sdk_hotkey([str(k) for k in keys])
        _invalidate_semantic_grounding_cache("desktop-hotkey-input")
        _invalidate_grid_fallbacks("desktop-hotkey-input")
    except Exception as exc:
        _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="error")
        current = await FRAME_CACHE.snapshot()
        return {"success": False, "reason": f"Desktop hotkey failed: {type(exc).__name__}: {exc}", "scope": scope}, [frame_to_vision_image(current, "hotkey_error")]
    post = await FRAME_CACHE.wait_for_change(before.version, timeout=2.5, stable_seconds=0.10)
    current = post if post.version >= before.version else await FRAME_CACHE.snapshot()
    changed = current.version > before.version
    _record_semantic_action(turn_state, "desktop_hotkey", target, target_key, family_key=family_key, outcome="sent_changed" if changed else "sent_unchanged")
    print(f"[Desktop hotkey: keys={keys!r} scope={scope!r} purpose={purpose!r} v{before.version}->v{current.version} changed={changed}]", flush=True)
    return ({
        "success": True,
        "input_sent": True,
        "keys": keys,
        "scope": scope,
        "purpose": purpose,
        "frame_changed": changed,
        "semantic_success_unverified": True,
        "verification_instruction": "Inspect the attached current frame and verify the intended UI effect; frame_changed alone is not proof.",
    }, [frame_to_clean_vision_image(current, "post_hotkey")])


def _text_retry_key(scope: str, target: str, text: str) -> str:
    semantic = _semantic_target_key(target)
    digest = hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"type:{scope}:{semantic}:{digest}"


async def _host_solari_type_literal(text: str) -> tuple[bool, str]:
    """Type non-secret literal text through Solari Desktop keyboard.type()."""
    if HOST_DESKTOP_CONTROL is None:
        return False, "Desktop SDK control handle is not available"
    try:
        await HOST_DESKTOP_CONTROL.keyboard.type(str(text))
        return True, "solari-sdk"
    except Exception as exc:
        return False, f"Solari keyboard.type failed: {type(exc).__name__}: {exc}"


async def desktop_type_text(
    turn_state: TurnState,
    text: str,
    target: str,
    scope: str,
    purpose: str = "",
) -> tuple[dict[str, Any], list[VisionImage]]:
    if FRAME_CACHE is None or HOST_DESKTOP_CONTROL is None:
        return {"success": False, "reason": "No active desktop control channel is attached."}, []

    scope = _normalize_hotkey_scope(scope)
    target = str(target or "").strip()
    literal = str(text)
    if scope not in {"local", "remote_rdp"}:
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "scope_required": True,
            "reason": "desktop_type_text requires explicit scope=local or scope=remote_rdp in Toolkit.",
            "instruction": "Use remote_rdp only for a focused Windows/Remmina field and local for Linux/Solari controls.",
        }, [frame_to_clean_vision_image(current, "type_scope_rejected")])
    if not target:
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "target_required": True,
            "reason": "desktop_type_text requires the semantic name of the focused field/control in Toolkit.",
        }, [frame_to_clean_vision_image(current, "type_target_rejected")])
    if scope == "remote_rdp":
        active_name = await _host_active_x11_window_name()
        if normalize_text(active_name) != "winserver":
            current = await FRAME_CACHE.snapshot()
            print(f"[toolkit-type] rejected remote_rdp target={target!r} active_x11={active_name!r}", flush=True)
            return ({
                "success": False,
                "remote_focus_required": True,
                "scope": scope,
                "target": target,
                "active_local_window": active_name or "unknown",
                "reason": "The active local X11 window is not the Remmina winserver session, so remote text could land in the wrong application.",
                "instruction": "Focus the visible winserver RDP canvas first, then retry only if the intended remote field is visibly focused.",
            }, [frame_to_clean_vision_image(current, "type_remote_focus_rejected")])

    retry_key = _text_retry_key(scope, target, literal)
    prior_nochange = int(turn_state.text_nochange_counts.get(retry_key, 0))
    if prior_nochange >= TOOLKIT_TEXT_NOCHANGE_LIMIT:
        current = await FRAME_CACHE.snapshot()
        turn_state.semantic_loop_rejections += 1
        print(
            f"[toolkit-type-guard] blocked target={target!r} scope={scope} identical_nochange={prior_nochange}",
            flush=True,
        )
        return ({
            "success": False,
            "typing_retry_exhausted": True,
            "scope": scope,
            "target": target,
            "identical_nochange_attempts": prior_nochange,
            "reason": (
                f"The same literal text has already been sent to the same field {prior_nochange} times with no visible framebuffer change. "
                "Toolkit will not keep spending model/tool rounds on the same ineffective input."
            ),
            "instruction": (
                "Do not retry the same text into this field again. Re-establish focus with a visibly grounded click, use a different interaction strategy, "
                "or report that text entry could not be verified. This individual input failure does not authorize BLOCKED for the whole goal."
            ),
        }, [frame_to_clean_vision_image(current, "type_retry_exhausted")])

    before = await FRAME_CACHE.snapshot()
    ok, method = await _host_solari_type_literal(literal)
    if not ok:
        current = await FRAME_CACHE.snapshot()
        return ({
            "success": False,
            "reason": method,
            "scope": scope,
            "target": target,
            "input_method": "solari-sdk",
        }, [frame_to_clean_vision_image(current, "type_error")])

    _invalidate_semantic_grounding_cache("desktop-type-input")
    _invalidate_grid_fallbacks("desktop-type-input")
    post = await FRAME_CACHE.wait_for_change(before.version, timeout=2.5, stable_seconds=0.10)
    current = post if post.version >= before.version else await FRAME_CACHE.snapshot()
    changed = current.version > before.version
    if changed:
        turn_state.text_nochange_counts.pop(retry_key, None)
    else:
        turn_state.text_nochange_counts[retry_key] = prior_nochange + 1
    attempts = int(turn_state.text_nochange_counts.get(retry_key, 0))
    action_key = f"type:{scope}:{_semantic_target_key(target)}"
    _record_semantic_action(
        turn_state,
        "desktop_type_text",
        target,
        action_key,
        family_key=f"type:{scope}",
        outcome="sent_changed" if changed else "sent_unchanged",
    )
    print(
        f"[Desktop type: chars={len(literal)} scope={scope!r} target={target!r} method={method!r} purpose={purpose!r} "
        f"v{before.version}->v{current.version} changed={changed} identical_nochange={attempts}]",
        flush=True,
    )
    if not changed:
        remaining = max(0, TOOLKIT_TEXT_NOCHANGE_LIMIT - attempts)
        return ({
            "success": False,
            "input_sent": True,
            "no_visible_change": True,
            "characters": len(literal),
            "scope": scope,
            "target": target,
            "purpose": purpose,
            "input_method": method,
            "identical_nochange_attempts": attempts,
            "identical_nochange_retries_remaining": remaining,
            "instruction": (
                "The text input produced no visible framebuffer change. Do not blindly repeat it. Re-check that the intended field really has focus. "
                + ("One final identical retry remains before the host guard stops this input." if remaining else "The host guard will reject another identical retry.")
            ),
        }, [frame_to_clean_vision_image(current, "post_type_nochange")])

    return ({
        "success": True,
        "input_sent": True,
        "characters": len(literal),
        "scope": scope,
        "target": target,
        "purpose": purpose,
        "input_method": method,
        "frame_changed": True,
        "semantic_success_unverified": True,
        "verification_instruction": "Inspect the attached current frame to confirm the literal text appears in the intended focused control.",
    }, [frame_to_clean_vision_image(current, "post_type")])


async def security_vault_status() -> dict[str, Any]:
    try:
        status = await asyncio.to_thread(VAULT.safe_status)
        return {
            "success": bool(status.get("ready")),
            "secret_values_returned": False,
            **status,
        }
    except Exception as exc:
        return {
            "success": False,
            "ready": False,
            "secret_values_returned": False,
            "reason": f"Vault status failed: {type(exc).__name__}: {exc}",
        }


async def security_vault_search(query: str, limit: int = 10) -> dict[str, Any]:
    try:
        items = await asyncio.to_thread(VAULT.search, str(query or ""), limit=int(limit or 10))
        return {
            "success": True,
            "query": str(query or ""),
            "items": items,
            "secret_values_returned": False,
        }
    except Exception as exc:
        return {
            "success": False,
            "reason": f"Vault search failed: {type(exc).__name__}: {exc}",
            "secret_values_returned": False,
        }


async def security_vault_type_secret(
    field: str,
    credential: str,
    frame_version: int,
    x_px: int,
    y_px: int,
    *,
    submit: bool,
) -> tuple[dict[str, Any], list[VisionImage]]:
    """Focus a field, fetch a vault value host-side, and type it without model exposure."""
    if not VAULT_ENABLED:
        return {
            "success": False,
            "reason": "Security vault is disabled by host configuration.",
            "secret_exposed_to_model": False,
        }, []
    if FRAME_CACHE is None or HOST_DESKTOP_CONTROL is None:
        return {
            "success": False,
            "reason": "No active desktop control channel is attached.",
            "secret_exposed_to_model": False,
        }, []
    field = str(field or "").strip().lower()
    if field not in {"username", "password", "totp"}:
        return {"success": False, "reason": "Unsupported secure field.", "secret_exposed_to_model": False}, []
    credential = str(credential or "").strip()
    if not credential:
        return {"success": False, "reason": "credential cannot be empty", "secret_exposed_to_model": False}, []

    # Use the exact same frame/bounds/staleness/display/cursor validation as
    # normal agent-native clicking. Its post-click image is pre-secret and is
    # intentionally discarded on success.
    focus_result, focus_images = await _agent_native_input(
        f"secure {field} input field for {credential}",
        frame_version,
        x_px,
        y_px,
        double=False,
    )
    if not focus_result.get("success"):
        return {
            "success": False,
            "reason": "The requested credential field could not be safely focused.",
            "focus": focus_result,
            "secret_exposed_to_model": False,
        }, focus_images

    reference = PRESENTED_FRAMES.get(int(frame_version))
    frame_height = reference.image.height if reference is not None else (await FRAME_CACHE.snapshot()).image.height
    _register_sensitive_redaction(int(y_px), frame_height, f"vault-{field}")

    getter = {
        "username": VAULT.get_username,
        "password": VAULT.get_password,
        "totp": VAULT.get_totp,
    }[field]
    secret = ""
    before_version = FRAME_CACHE.version
    try:
        # Blocking CLI work stays off the asyncio loop. The secret exists only
        # in host memory and is never included in a model/tool message.
        secret = await asyncio.to_thread(getter, credential)
        await HOST_DESKTOP_CONTROL.keyboard.type(secret)
        if submit:
            await _host_nav_press("enter")
        _invalidate_semantic_grounding_cache("security-vault-input")
        _invalidate_grid_fallbacks("security-vault-input")
        # Observe host-side only. Do not encode or attach the secret-filled
        # framebuffer as a tool image. Subsequent images are redacted centrally.
        try:
            await FRAME_CACHE.wait_for_change(before_version, timeout=2.5, stable_seconds=0.10)
        except Exception:
            pass
    except Exception as exc:
        print(
            f"[toolkit-vault] secure typing failed field={field} credential={credential!r} error={type(exc).__name__}",
            flush=True,
        )
        return {
            "success": False,
            "reason": f"Secure {field} entry failed: {type(exc).__name__}",
            "credential": credential,
            "field": field,
            "secret_exposed_to_model": False,
            "visual_evidence_withheld": True,
        }, []
    finally:
        # Best-effort drop of the local reference. Python strings are immutable,
        # but the value is never persisted or copied into conversation state.
        secret = ""

    print(
        f"[toolkit-vault] secure {field} typed credential={credential!r} submit={bool(submit)} secret_exposed_to_model=False",
        flush=True,
    )
    return {
        "success": True,
        "credential": credential,
        "field": field,
        "input_sent": True,
        "submitted": bool(submit),
        "secret_exposed_to_model": False,
        "secret_returned_in_tool_result": False,
        "visual_evidence_withheld": True,
        "security_redaction_active": True,
        "next_step": (
            "The host submitted the field. Inspect a later security-redacted framebuffer to verify the resulting UI state."
            if submit else
            "Continue the login flow. Use another secure vault tool for any password or OTP field rather than asking for the secret."
        ),
    }, []


def _position_in_box(box: tuple[int, int, int, int], position: str) -> tuple[int, int]:
    left, top, right, bottom = box
    x_fracs = {"left": 0.25, "center": 0.50, "right": 0.75}
    y_fracs = {"top": 0.25, "center": 0.50, "bottom": 0.75}
    value = (position or "center").strip().lower()
    aliases = {
        "top-left": ("left", "top"), "top": ("center", "top"), "top-right": ("right", "top"),
        "left": ("left", "center"), "center": ("center", "center"), "right": ("right", "center"),
        "bottom-left": ("left", "bottom"), "bottom": ("center", "bottom"), "bottom-right": ("right", "bottom"),
    }
    if value not in aliases:
        raise ValueError(f"Unsupported cell position {position!r}")
    x_name, y_name = aliases[value]
    x = round(left + (right - left - 1) * x_fracs[x_name])
    y = round(top + (bottom - top - 1) * y_fracs[y_name])
    return x, y


async def desktop_click_grid(
    mcp,
    target: str,
    cell: str,
    frame_version: int | None = None,
    zoom_id: str | None = None,
    fallback_token: str | None = None,
    position: str = "center",
) -> tuple[dict[str, Any], list[VisionImage]]:
    if FRAME_CACHE is None or not ACTIVE_DESKTOP_SESSION_ID:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []

    current = await FRAME_CACHE.snapshot()
    if not fallback_token:
        return ({
            "success": False,
            "reason": "Legacy grid click is host-gated. semantic grounding must fail first and return fallback_token for this same target.",
        }, [frame_to_clean_vision_image(current, "grid_click_unauthorized")])
    try:
        reference, base_bbox, cols, rows, _depth = _grid_source_context(frame_version=frame_version, zoom_id=zoom_id)
        box = _grid_cell_bbox(base_bbox, cols, rows, cell)
    except (TypeError, ValueError) as exc:
        return {"success": False, "reason": str(exc)}, [frame_to_clean_vision_image(current, "grid_click_invalid")]
    authorized, auth_reason = _validate_grid_fallback_authorization(fallback_token, target, reference.version)
    if not authorized:
        return {"success": False, "reason": auth_reason}, [frame_to_clean_vision_image(current, "grid_click_unauthorized")]

    # Enforce OCR-first opportunistically only after the fallback authorization itself is valid.
    for candidate_text in _target_ocr_candidates(target):
        candidate, _candidates, error = locate_text(current.image, candidate_text, "full")
        if candidate is not None and error is None and candidate.text_score >= 0.92:
            print(
                f"[Grid click rerouted to OCR: requested={target!r} visible_text={candidate_text!r} "
                f"matched={candidate.text!r}]",
                flush=True,
            )
            result, images = await desktop_click_target(mcp, candidate_text, "full")
            if isinstance(result, dict):
                result["rerouted_from_grid"] = True
                result["requested_visual_target"] = target
            return result, images

    if current.image.size != reference.image.size:
        return ({"success": False, "reason": "Desktop resolution changed; re-localize on the current frame."},
                [frame_to_vision_image(current, "grid_click_resolution_changed")])
    if current.version != reference.version:
        mean_diff, changed_fraction = _region_change_metrics(reference.image, current.image, box)
        if changed_fraction >= GRID_STALE_CHANGED_FRACTION or mean_diff >= GRID_STALE_MEAN_DIFF:
            return ({
                "success": False,
                "reason": "That grid cell changed after the referenced image. Re-localize on the attached current frame.",
                "requested_frame_version": reference.version,
                "current_frame_version": current.version,
                "local_change_mean": round(mean_diff, 2),
                "local_changed_fraction": round(changed_fraction, 4),
            }, [frame_to_vision_image(current, "grid_click_stale")])

    try:
        x, y = _position_in_box(box, position)
    except ValueError as exc:
        return {"success": False, "reason": str(exc)}, [frame_to_vision_image(current, "grid_click_position_invalid")]
    click_x, click_y = to_desktop_xy(reference.image, x, y)
    debug_path = save_point_debug_image(
        reference.image,
        frame_x=x,
        frame_y=y,
        norm_x=0,
        norm_y=0,
        target=f"{target} cell={cell}",
        frame_version=reference.version,
    )
    before_version = FRAME_CACHE.version
    click_args = session_args_for(RAW_CLICK_TOOL, ACTIVE_DESKTOP_SESSION_ID) or {"sessionId": ACTIVE_DESKTOP_SESSION_ID}
    click_args.update({"x": click_x, "y": click_y})
    click_result = await mcp.call_tool(RAW_CLICK_TOOL, click_args)
    if getattr(click_result, "is_error", False):
        return {"success": False, "reason": result_text(click_result) or "solari_click returned an error"}, []
    _invalidate_semantic_grounding_cache("grid-click-input")
    _invalidate_grid_fallbacks("grid-click-input")
    post = await FRAME_CACHE.wait_for_change(before_version, timeout=2.5, stable_seconds=0.10)
    current_post = post if post.version >= before_version else await FRAME_CACHE.snapshot()
    changed = current_post.version > before_version
    print(
        f"[Grid click input sent: target={target!r} cell={cell.upper()} zoom_id={zoom_id!r} "
        f"frame=v{reference.version} pixel=({click_x},{click_y}) v{before_version}->v{current_post.version} changed={changed}]",
        flush=True,
    )
    return ({
        "success": True,
        "input_sent": True,
        "target": target,
        "cell": cell.upper(),
        "zoom_id": zoom_id,
        "position": position,
        "source_frame_version": reference.version,
        "clicked_coordinates_host_only": {"x": click_x, "y": click_y},
        "frame_changed": changed,
        "semantic_success_unverified": True,
        "verification_instruction": "Inspect the attached post-action frame and verify the intended UI effect. Do not infer success from frame_changed alone.",
        "debug_image": debug_path,
    }, [frame_to_vision_image(current_post, "post_grid_click")])


async def desktop_double_click_grid(
    mcp,
    target: str,
    cell: str,
    frame_version: int | None = None,
    zoom_id: str | None = None,
    fallback_token: str | None = None,
    position: str = "center",
) -> tuple[dict[str, Any], list[VisionImage]]:
    """Resolve one visual grid cell and emit an atomic double-click."""
    if FRAME_CACHE is None or not ACTIVE_DESKTOP_SESSION_ID:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []

    current = await FRAME_CACHE.snapshot()
    if not fallback_token:
        return ({
            "success": False,
            "reason": "Legacy grid double-click is host-gated. semantic grounding must fail first and return fallback_token for this same target.",
        }, [frame_to_clean_vision_image(current, "double_click_grid_unauthorized")])
    try:
        reference, base_bbox, cols, rows, _depth = _grid_source_context(
            frame_version=frame_version, zoom_id=zoom_id
        )
        box = _grid_cell_bbox(base_bbox, cols, rows, cell)
    except (TypeError, ValueError) as exc:
        return {"success": False, "reason": str(exc)}, [frame_to_clean_vision_image(current, "double_click_grid_invalid")]
    authorized, auth_reason = _validate_grid_fallback_authorization(fallback_token, target, reference.version)
    if not authorized:
        return {"success": False, "reason": auth_reason}, [frame_to_clean_vision_image(current, "double_click_grid_unauthorized")]

    # Keep the same OCR-first guarantee as single grid clicks, after authorization. A target such
    # as "Windows Server via Enterprise VPN profile" should not depend on the model
    # estimating a cell when host OCR can resolve it exactly.
    for candidate_text in _target_ocr_candidates(target):
        candidate, _candidates, error = locate_text(current.image, candidate_text, "full")
        if candidate is not None and error is None and candidate.text_score >= 0.92:
            print(
                f"[Grid double-click rerouted to OCR: requested={target!r} visible_text={candidate_text!r} "
                f"matched={candidate.text!r}]",
                flush=True,
            )
            result, images = await desktop_double_click_target(mcp, candidate_text, "full")
            if isinstance(result, dict):
                result["rerouted_from_grid"] = True
                result["requested_visual_target"] = target
            return result, images

    if current.image.size != reference.image.size:
        return (
            {"success": False, "reason": "Desktop resolution changed; re-localize on the current frame."},
            [frame_to_vision_image(current, "double_click_grid_resolution_changed")],
        )
    if current.version != reference.version:
        mean_diff, changed_fraction = _region_change_metrics(reference.image, current.image, box)
        if changed_fraction >= GRID_STALE_CHANGED_FRACTION or mean_diff >= GRID_STALE_MEAN_DIFF:
            return (
                {
                    "success": False,
                    "reason": "That grid cell changed after the referenced image. Re-localize on the attached current frame.",
                    "requested_frame_version": reference.version,
                    "current_frame_version": current.version,
                    "local_change_mean": round(mean_diff, 2),
                    "local_changed_fraction": round(changed_fraction, 4),
                },
                [frame_to_vision_image(current, "double_click_grid_stale")],
            )

    try:
        x, y = _position_in_box(box, position)
    except ValueError as exc:
        return {"success": False, "reason": str(exc)}, [
            frame_to_vision_image(current, "double_click_grid_position_invalid")
        ]

    click_x, click_y = to_desktop_xy(reference.image, x, y)
    debug_path = save_point_debug_image(
        reference.image,
        frame_x=x,
        frame_y=y,
        norm_x=0,
        norm_y=0,
        target=f"double-click {target} cell={cell}",
        frame_version=reference.version,
    )
    result, images = await _send_double_click(
        mcp,
        click_x,
        click_y,
        target=target,
        source_frame_version=reference.version,
        debug_path=debug_path,
    )
    if result.get("input_sent"):
        _invalidate_semantic_grounding_cache("grid-double-click-input")
        _invalidate_grid_fallbacks("grid-double-click-input")
    result.update(
        {
            "cell": cell.upper(),
            "zoom_id": zoom_id,
            "position": position,
            "source_frame_version": reference.version,
            "clicked_coordinates_host_only": {"x": click_x, "y": click_y},
        }
    )
    return result, images


async def desktop_click_point(
    mcp,
    target: str,
    x: float,
    y: float,
    frame_version: int,
) -> tuple[dict[str, Any], list[VisionImage]]:
    """Click a visual target using normalized coordinates from a model-seen frame."""
    global FRAME_CACHE

    if not ACTIVE_DESKTOP_SESSION_ID or FRAME_CACHE is None:
        return {"success": False, "reason": "No live desktop framebuffer is attached."}, []

    try:
        norm_x = float(x)
        norm_y = float(y)
        requested_version = int(frame_version)
    except (TypeError, ValueError):
        return {"success": False, "reason": "x, y, and frame_version must be numeric."}, []

    if not (0 <= norm_x <= VISUAL_COORD_MAX and 0 <= norm_y <= VISUAL_COORD_MAX):
        return (
            {
                "success": False,
                "reason": f"Normalized coordinates must be within 0..{VISUAL_COORD_MAX}.",
                "received": {"x": norm_x, "y": norm_y},
            },
            [],
        )

    # Use the exact model-presented frame for debugging/geometry when available.
    # The click itself is issued to the current desktop using normalized geometry,
    # which remains independent of provider-side internal vision resizing.
    reference = PRESENTED_FRAMES.get(requested_version)
    current = await FRAME_CACHE.snapshot()

    if reference is None:
        current_image = frame_to_vision_image(current, "point_frame_expired")
        return (
            {
                "success": False,
                "reason": (
                    f"Frame v{requested_version} is no longer in the host visual frame history. "
                    "Re-localize the target using the attached current frame and retry."
                ),
                "requested_frame_version": requested_version,
                "current_frame_version": current.version,
            },
            [current_image],
        )

    if (reference.image.width, reference.image.height) != (current.image.width, current.image.height):
        current_image = frame_to_vision_image(current, "point_resolution_changed")
        return (
            {
                "success": False,
                "reason": "Desktop resolution changed after the referenced frame; re-localize on the current frame.",
                "referenced_resolution": [reference.image.width, reference.image.height],
                "current_resolution": [current.image.width, current.image.height],
            },
            [current_image],
        )

    if TEXTUAL_POINT_HINT_RE.search(target or ""):
        current_image = frame_to_vision_image(current, "point_textual_target_rejected")
        return (
            {
                "success": False,
                "reason": (
                    "desktop_click_point is restricted to icon-only/non-textual controls. "
                    "This target appears to be a text-bearing field/control. Use desktop_click_target "
                    "with the exact visible label or placeholder (for example Buscar), then type."
                ),
                "target": target,
                "requested_frame_version": requested_version,
                "current_frame_version": current.version,
            },
            [current_image],
        )

    frame_x, frame_y, click_x, click_y = normalized_to_desktop_xy(
        reference.image, norm_x, norm_y
    )

    stale_mean = 0.0
    stale_fraction = 0.0
    stale_box = None
    if current.version != requested_version:
        stale_mean, stale_fraction, stale_box = _point_region_change_metrics(
            reference.image,
            current.image,
            x=frame_x,
            y=frame_y,
        )
        stale = (
            stale_fraction >= POINT_STALE_CHANGED_FRACTION
            or stale_mean >= POINT_STALE_MEAN_DIFF
        )
        if stale:
            current_image = frame_to_vision_image(current, "point_stale_region_rejected")
            print(
                f"[Visual click rejected: stale target region frame=v{requested_version} current=v{current.version} "
                f"mean_diff={stale_mean:.1f} changed_fraction={stale_fraction:.3f}]",
                flush=True,
            )
            return (
                {
                    "success": False,
                    "reason": (
                        "The visual neighborhood around this coordinate changed after the referenced frame. "
                        "The point was not clicked. Re-localize the icon/control on the attached current frame."
                    ),
                    "target": target,
                    "requested_frame_version": requested_version,
                    "current_frame_version": current.version,
                    "local_change_mean": round(stale_mean, 2),
                    "local_changed_fraction": round(stale_fraction, 4),
                    "region": list(stale_box),
                },
                [current_image],
            )

    debug_path = save_point_debug_image(
        reference.image,
        frame_x=frame_x,
        frame_y=frame_y,
        norm_x=norm_x,
        norm_y=norm_y,
        target=target or "visual target",
        frame_version=requested_version,
    )

    print(
        f"[Visual click: target={target!r} frame=v{requested_version} current=v{current.version} "
        f"normalized=({norm_x:.1f},{norm_y:.1f}) framebuffer=({frame_x},{frame_y}) "
        f"desktop=({click_x},{click_y}) resolution={reference.image.width}x{reference.image.height}]",
        flush=True,
    )

    before_version = FRAME_CACHE.version
    click_args = session_args_for(RAW_CLICK_TOOL, ACTIVE_DESKTOP_SESSION_ID)
    if not click_args:
        click_args = {"sessionId": ACTIVE_DESKTOP_SESSION_ID}
    click_args.update({"x": click_x, "y": click_y})

    click_result = await mcp.call_tool(RAW_CLICK_TOOL, click_args)
    if getattr(click_result, "is_error", False):
        return (
            {
                "success": False,
                "target": target,
                "reason": result_text(click_result) or "solari_click returned an error",
                "debug_image": debug_path,
            },
            [],
        )

    post = await FRAME_CACHE.wait_for_change(before_version)
    changed = post.version > before_version
    post_images = [frame_to_vision_image(post, "post_point_click")] if changed else []

    print(
        f"[Post-visual-click live frame: before=v{before_version} after=v{post.version} changed={changed}]",
        flush=True,
    )

    return (
        {
            "success": True,
            "target": target,
            "coordinate_space": {
                "min": 0,
                "max": VISUAL_COORD_MAX,
                "origin": "top-left",
            },
            "normalized_coordinates": {"x": norm_x, "y": norm_y},
            "referenced_frame_version": requested_version,
            "current_frame_at_click": current.version,
            "framebuffer_coordinates": {"x": frame_x, "y": frame_y},
            "desktop_coordinates": {"x": click_x, "y": click_y},
            "frame_changed": changed,
            "post_frame_version": post.version,
            "referenced_region_change_mean": round(stale_mean, 2),
            "referenced_region_changed_fraction": round(stale_fraction, 4),
            "debug_image": debug_path,
        },
        post_images,
    )


async def desktop_refresh_view() -> tuple[str, list[VisionImage]]:
    if FRAME_CACHE is None:
        return "TOOL ERROR: no live desktop framebuffer is attached.", []
    try:
        snapshot = (
            await _refresh_frame_cache_from_desktop("desktop_refresh_view")
            if TOOLKIT_FRESH_REFRESH_VIEW
            else await FRAME_CACHE.snapshot()
        )
    except Exception as exc:
        return (
            "TOOL ERROR: could not obtain a trustworthy fresh desktop framebuffer: "
            f"{type(exc).__name__}: {exc}",
            [],
        )
    image = frame_to_clean_vision_image(snapshot, "refresh")
    return (
        json.dumps(
            {
                "success": True,
                "frame_version": snapshot.version,
                "resolution": [snapshot.image.width, snapshot.image.height],
                "frame_age_ms": round((time.monotonic() - snapshot.updated_at) * 1000),
                "native_coordinate_space": {
                    "origin": "top-left",
                    "x_min": 0,
                    "x_max": snapshot.image.width - 1,
                    "y_min": 0,
                    "y_max": snapshot.image.height - 1,
                    "width": snapshot.image.width,
                    "height": snapshot.image.height,
                    "frame_version": snapshot.version,
                },
            }
        ),
        [image],
    )


async def add_user_message_with_desktop(user_message: str) -> None:
    """Attach the newest in-memory framebuffer at input submission time."""
    if FRAME_CACHE is None:
        messages.append(
            {
                "role": "user",
                "content": user_message + "\n\n[Host note: no live desktop framebuffer is available.]",
            }
        )
        return

    try:
        snapshot = (
            await _refresh_frame_cache_from_desktop("human-message")
            if TOOLKIT_FRESH_USER_FRAME
            else await FRAME_CACHE.snapshot()
        )
    except Exception as exc:
        messages.append(
            {
                "role": "user",
                "content": (
                    user_message
                    + "\n\n[Host error: a trustworthy fresh desktop framebuffer could not be acquired for this human turn. "
                    + f"{type(exc).__name__}: {exc}. Do not infer the current GUI from older images.]"
                ),
            }
        )
        print(
            f"[toolkit-rfb] user-turn frame attachment aborted: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return
    image = frame_to_clean_vision_image(snapshot, "user_turn")
    age_ms = round((time.monotonic() - snapshot.updated_at) * 1000)

    if image.saved_path:
        print(
            f"[Attached live frame v{snapshot.version}: {image.saved_path} last_change_age={age_ms}ms]",
            flush=True,
        )
    else:
        print(f"[Attached live frame v{snapshot.version}: last_change_age={age_ms}ms]", flush=True)

    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        user_message
                        + f"\n\n[Host note: attached live RFB frame version {snapshot.version}; "
                        + f"frame age at submission was {age_ms} ms. This is the authoritative CURRENT GUI visual epoch; "
                        + "older screen-state narration is superseded.]\n"
                        + geometry_note(image)
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.mime_type};base64,{image.base64_data}"
                    },
                },
            ],
        }
    )


def goal_context_text(turn_state: TurnState) -> str:
    """Compact host-owned reminder injected into every active-goal inference."""
    memory = _goal_memory_summary(turn_state)
    recent_actions = turn_state.action_history[-8:]
    if recent_actions:
        recent_action_text = " -> ".join(
            f"{record.target_key}[{record.outcome}]" + (f"{{{record.family_key}}}" if record.family_key else "")
            for record in recent_actions
        )
    else:
        recent_action_text = "(none yet)"
    return (
        "[Host persistent goal state]\n"
        f"AUTHORITATIVE USER REQUEST: {turn_state.user_request}\n"
        f"COMPACT OBJECTIVE: {turn_state.objective or turn_state.user_request}\n"
        f"SUCCESS CONDITION: {turn_state.success_condition or 'Satisfy the exact user request.'}\n"
        f"TARGET: {turn_state.target or '(not separately specified)'}\n"
        f"STATUS: {turn_state.status}\n"
        f"PRECISION VISUAL CHECKS THIS GOAL: {turn_state.precision_inspections}\n"
        f"SEMANTIC LOOP REJECTIONS: {turn_state.semantic_loop_rejections}\n"
        f"RECENT SEMANTIC ACTION TARGETS: {recent_action_text}\n\n"
        "DURABLE GOAL MEMORY (authoritative for facts/checkpoints already observed this goal):\n"
        f"{memory}\n\n"
        "The human request above is fixed. Do not redefine or substitute it. "
        "Durable goal memory survives old framebuffer-image compaction. If a needed fact/checkpoint is present there, "
        "reuse it instead of reopening the same source solely to see it again. If a screen contains instructions, values, "
        "or workflow details you will need later and they are NOT yet in durable memory, call goal_remember BEFORE leaving that screen. "
        "Unrelated findings do not count as success. Continue using tools while STATUS is IN_PROGRESS. "
        "A rejected or rate-limited individual tool call does NOT mean the goal itself is BLOCKED; adapt and use another "
        "available path whenever possible. Before ending the turn, call finish_goal with SUCCESS or NOT_FOUND and "
        "concrete desktop evidence. BLOCKED is host-authorized only: call finish_goal BLOCKED only if a host/tool message "
        "explicitly says host_authorized_blocked=true. Do not infer BLOCKED from a failed click/navigation/typing attempt. "
        "For desktop-grounded discovery, use only facts actually observed in this turn's desktop/tool evidence or durable goal memory. "
        "Never fill verification gaps with assumed release dates, inventory, product availability, or other general model knowledge. "
        "If desktop evidence is incomplete, keep working or state that verification was incomplete. "
        "For price/currency goals, inspect the exact product/value area with desktop_inspect_cell before SUCCESS."
    )


def request_messages_for_turn(turn_state: TurnState) -> list[dict[str, Any]] | None:
    if turn_state.mode != "GOAL":
        return None
    temporary = list(messages)
    temporary.append({"role": "user", "content": goal_context_text(turn_state)})
    return temporary


def start_goal(turn_state: TurnState, arguments: dict[str, Any]) -> dict[str, Any]:
    objective = str(arguments.get("objective", "")).strip()
    success_condition = str(arguments.get("success_condition", "")).strip()
    target = str(arguments.get("target", "")).strip() or None

    if not objective or not success_condition:
        return {
            "ok": False,
            "error": "start_goal requires non-empty objective and success_condition",
            "authoritative_user_request": turn_state.user_request,
        }

    # The original request remains authoritative. The model's fields are only a
    # compact operational representation and may never replace it.
    turn_state.mode = "GOAL"
    turn_state.objective = objective
    turn_state.success_condition = success_condition
    turn_state.target = target
    turn_state.status = "IN_PROGRESS"
    turn_state.result = None
    turn_state.evidence = None
    turn_state.host_action_budget_exhausted = False
    turn_state.budget_warning_sent = False
    turn_state.host_blocked = False
    turn_state.host_block_reason = None
    turn_state.precision_inspections = 0
    turn_state.precision_last_frame = None
    turn_state.precision_last_target = None
    turn_state.semantic_memory.clear()
    turn_state.action_history.clear()
    turn_state.semantic_loop_rejections = 0
    turn_state.text_nochange_counts.clear()

    print(f"[Persistent goal started: {objective}]", flush=True)
    return {
        "ok": True,
        "mode": "GOAL",
        "status": "IN_PROGRESS",
        "authoritative_user_request": turn_state.user_request,
        "objective": objective,
        "success_condition": success_condition,
        "target": target,
        "instruction": "Continue working. Do not answer finally until finish_goal is accepted.",
    }



_SEMANTIC_UI_STOPWORDS = {
    "click", "double", "select", "open", "close", "launch", "activate", "choose",
    "button", "icon", "taskbar", "toolbar", "desktop", "shortcut", "window", "dialog",
    "file", "folder", "menu", "submenu", "row", "field", "control", "tab", "titlebar",
    "saved", "profile", "application", "app", "the", "a", "an", "on", "in", "at",
    "rdp", "remote", "maximize", "minimize", "restore",
}


_VISUAL_DESCRIPTOR_TOKENS = {
    "black", "white", "red", "green", "blue", "orange", "yellow", "gray", "grey", "purple", "pink",
    "dark", "light", "small", "large", "tiny", "square", "circle", "round", "triangle", "arrow", "chevron",
    "mouse", "gear", "folder", "document", "paper", "image", "picture", "logo", "symbol", "shape", "area",
    "page", "banner", "background", "panel", "region", "section",
    "left", "right", "top", "bottom", "center", "upper", "lower",
}


_TASKBAR_PROBE_WORDS = {
    "unidentified", "unlabeled", "unknown", "running", "window", "button", "slot", "position",
    "left", "right", "before", "after", "next", "adjacent", "remote", "windows", "taskbar",
}


def _canonical_semantic_token(token: str) -> str:
    token = normalize_text(token)
    if token.startswith("reimburs"):
        return "reimburse"
    if token.startswith("instruct"):
        return "instruction"
    if token.startswith("account"):
        return "account"
    if token.startswith("invoice"):
        return "invoice"
    return token


def _semantic_identity_tokens(text: str) -> list[str]:
    # UI labels and filenames commonly use '-', '_' or '.' as word separators.
    # Split them before stemming so REIMBURSEMENT-INSTRUCTIONS and
    # "reimbursement instructions" canonicalize identically.
    separated = re.sub(r"[-_.]+", " ", str(text))
    normalized = normalize_text(separated)
    raw = re.findall(r"[a-z0-9][a-z0-9]*", normalized)
    output: list[str] = []
    for raw_token in raw:
        token = _canonical_semantic_token(raw_token)
        if not token or token in _SEMANTIC_UI_STOPWORDS or len(token) < 2:
            continue
        if token not in output:
            output.append(token)
    return output


def _semantic_target_key(target: str) -> str:
    tokens = _semantic_identity_tokens(target)
    if tokens:
        return "|".join(sorted(tokens))
    fallback = normalize_text(target)
    return fallback or "visual-target"


def _goal_memory_summary(turn_state: TurnState, *, max_chars: int = 5000) -> str:
    if not turn_state.semantic_memory:
        return "(none recorded yet)"
    lines: list[str] = []
    for fact in turn_state.semantic_memory.values():
        state = "COMPLETED" if fact.completed else "FACT"
        source = fact.source
        if fact.frame_version is not None:
            source += f" v{fact.frame_version}"
        lines.append(
            f"- [{state}] {fact.key}: {fact.value} | evidence: {fact.evidence} | source: {source}"
        )
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 20] + "\n...[truncated]"


def goal_remember(turn_state: TurnState, arguments: dict[str, Any]) -> dict[str, Any]:
    if not turn_state.goal_active:
        return {
            "ok": False,
            "error": "goal_remember requires an active IN_PROGRESS persistent goal.",
        }
    key_raw = str(arguments.get("key", "")).strip()
    value = str(arguments.get("value", "")).strip()
    evidence = str(arguments.get("evidence", "")).strip()
    source = str(arguments.get("source", "")).strip().lower()
    completed = bool(arguments.get("completed", False))
    frame_version = arguments.get("frame_version")
    if not key_raw or len(value) < 6 or len(evidence) < 6:
        return {
            "ok": False,
            "error": "goal_remember requires a stable key plus concrete value and evidence; do not store vague acknowledgements.",
        }
    vague = normalize_text(value).strip(" .:-")
    if vague in {
        "instructions read", "instruction read", "document read", "file read", "read instructions",
        "reviewed", "verified", "done", "complete", "completed", "visible",
    }:
        return {
            "ok": False,
            "error": (
                "The proposed memory is too vague. Store the useful content/checkpoint itself, e.g. the actual workflow steps, "
                "invoice fields, or what was visibly verified."
            ),
        }
    if source not in {"desktop_frame", "tool_result", "user_request"}:
        return {"ok": False, "error": "source must be desktop_frame, tool_result, or user_request"}
    parsed_frame: int | None = None
    if source == "desktop_frame":
        try:
            parsed_frame = int(frame_version)
        except Exception:
            return {"ok": False, "error": "desktop_frame memory requires integer frame_version"}
        if parsed_frame not in PRESENTED_FRAMES:
            return {
                "ok": False,
                "error": (
                    f"frame_version v{parsed_frame} is not a host-presented frame in this turn. "
                    "Record facts only from a framebuffer you actually inspected."
                ),
            }
    key = re.sub(r"[^a-z0-9_.-]+", "_", normalize_text(key_raw)).strip("_")[:80]
    if not key:
        return {"ok": False, "error": "key did not contain a usable identifier"}
    if key not in turn_state.semantic_memory and len(turn_state.semantic_memory) >= TOOLKIT_GOAL_MEMORY_MAX_FACTS:
        oldest_key = next(iter(turn_state.semantic_memory))
        turn_state.semantic_memory.pop(oldest_key, None)
    fact = GoalMemoryFact(
        key=key,
        value=value[:3000],
        evidence=evidence[:1200],
        source=source,
        frame_version=parsed_frame,
        completed=completed,
    )
    turn_state.semantic_memory[key] = fact
    print(
        f"[toolkit-goal-memory] stored key={key!r} completed={completed} source={source} frame={parsed_frame}",
        flush=True,
    )
    return {
        "ok": True,
        "key": key,
        "completed": completed,
        "memory_count": len(turn_state.semantic_memory),
        "instruction": (
            "This fact/checkpoint is now durable for the rest of the goal. Do not reopen its source merely because an older screenshot is no longer attached."
        ),
    }


def _validate_visual_identity_contract(arguments: dict[str, Any]) -> tuple[bool, str]:
    if not TOOLKIT_TARGET_GROUNDING_STRICT:
        return True, ""
    target = str(arguments.get("target", "")).strip()
    evidence = str(arguments.get("visual_evidence", "")).strip()
    basis = str(arguments.get("evidence_basis", "")).strip().lower()
    ui_scope = str(arguments.get("ui_scope", "")).strip().lower()
    if ui_scope not in {"local", "remote_rdp"}:
        return False, "ui_scope must be local or remote_rdp for every native pointer action"
    if basis not in {"visible_text", "visible_icon", "spatial_control"}:
        return False, "evidence_basis must be visible_text, visible_icon, or spatial_control"
    if len(evidence) < 3:
        return False, "visual_evidence must describe concrete evidence visible in the exact current frame"

    target_lower = normalize_text(target)
    evidence_lower = normalize_text(evidence)
    taskbar_or_toolbar = "taskbar" in target_lower or "toolbar" in target_lower
    if taskbar_or_toolbar and any(word in (target_lower + " " + evidence_lower) for word in ("remote", "windows taskbar", "rdp")) and ui_scope != "remote_rdp":
        return False, "A remote/Windows taskbar target must use ui_scope=remote_rdp, not local."
    explicit_unlabeled_probe = (
        taskbar_or_toolbar
        and basis == "spatial_control"
        and any(word in target_lower.split() for word in ("unidentified", "unlabeled", "unknown", "slot", "button"))
        and ("taskbar" in evidence_lower or "toolbar" in evidence_lower or "button" in evidence_lower)
    )
    # Toolkit deliberately permits bounded exploration of an unlabeled taskbar
    # control when the model makes NO application-identity claim. The loop
    # guard keys such probes by pixel slot so renaming cannot create infinity.
    if explicit_unlabeled_probe:
        return True, ""

    target_tokens = set(_semantic_identity_tokens(target))
    evidence_tokens = set(_semantic_identity_tokens(evidence))
    if not target_tokens:
        return True, ""
    overlap = target_tokens & evidence_tokens
    has_icon_word = "icon" in target_lower
    identity_tokens = target_tokens - _VISUAL_DESCRIPTOR_TOKENS - _TASKBAR_PROBE_WORDS
    opaque_app_token = any(any(ch.isdigit() for ch in token) for token in identity_tokens)
    if basis != "visible_text" and identity_tokens and (taskbar_or_toolbar or (has_icon_word and opaque_app_token)):
        return False, (
            "A named application/item identity on this unlabeled icon/taskbar/toolbar target requires visible text in the current frame. "
            f"Unverified identity token(s): {', '.join(sorted(identity_tokens))}. "
            "For taskbar exploration, do not guess the application name: use evidence_basis=spatial_control and describe an unlabeled/unknown button by position only."
        )
    named_icon = has_icon_word or taskbar_or_toolbar
    if named_icon and basis == "visible_text" and not target_tokens.issubset(evidence_tokens):
        missing = sorted(target_tokens - evidence_tokens)
        return False, (
            "The named icon/taskbar identity is not supported by the visible text evidence. "
            f"Missing visible identity token(s): {', '.join(missing)}."
        )
    required_overlap = max(1, (len(target_tokens) + 1) // 2)
    if len(overlap) < required_overlap:
        return False, (
            "target identity is not sufficiently grounded in visual_evidence from this frame. "
            f"Target identity tokens={sorted(target_tokens)} evidence tokens={sorted(evidence_tokens)}. "
            "Use exact visible label text, a non-inferred visual description, or a spatial_control taskbar probe with no application-name claim."
        )
    return True, ""


def _pointer_action_keys(arguments: dict[str, Any]) -> tuple[str, str | None]:
    target = str(arguments.get("target", "visual target"))
    basis = str(arguments.get("evidence_basis", "")).strip().lower()
    target_lower = normalize_text(target)
    evidence_lower = normalize_text(str(arguments.get("visual_evidence", "")))
    if "taskbar" in target_lower and basis in {"spatial_control", "visible_icon"}:
        declared = str(arguments.get("ui_scope", "")).strip().lower()
        scope = declared if declared in {"local", "remote_rdp"} else ("remote_rdp" if any(word in (target_lower + " " + evidence_lower) for word in ("remote", "windows taskbar", "rdp")) else "local")
        try:
            x = int(arguments.get("x_px"))
        except Exception:
            x = -1
        bucket = int(round(x / TOOLKIT_TASKBAR_X_BUCKET_PX) * TOOLKIT_TASKBAR_X_BUCKET_PX) if x >= 0 else -1
        return f"taskbar-probe:{scope}:x{bucket}", f"taskbar-probe:{scope}"
    return _semantic_target_key(target), None


def _hotkey_action_keys(scope: str, keys: list[str]) -> tuple[str, str]:
    normalized = [str(k).strip().casefold().replace("pagedown", "page_down").replace("pageup", "page_up") for k in keys if str(k).strip()]
    chord = "+".join(normalized) or "<empty>"
    return f"hotkey:{scope}:{chord}", f"hotkey:{scope}"


def _semantic_loop_guard(
    turn_state: TurnState,
    tool_name: str,
    target: str,
    *,
    target_key: str | None = None,
    family_key: str | None = None,
    same_limit: int | None = None,
    family_limit: int | None = None,
) -> tuple[bool, str, str]:
    key = target_key or _semantic_target_key(target)
    if not turn_state.goal_active:
        return True, "", key
    recent = turn_state.action_history[-TOOLKIT_ACTION_HISTORY_WINDOW:]
    limit = TOOLKIT_SAME_TARGET_REPEAT_LIMIT if same_limit is None else max(1, int(same_limit))
    same_count = sum(1 for record in recent if record.target_key == key)
    if same_count >= limit:
        return False, (
            f"Semantic action loop detected: action {target!r} (canonical={key!r}) has already been attempted "
            f"{same_count} times in the recent goal history. Renaming the same control/action does not create progress. "
            "Use the current post-action evidence, durable goal memory, or choose a genuinely different next step."
        ), key
    if family_key and family_limit is not None:
        family_count = sum(1 for record in recent if record.family_key == family_key)
        if family_count >= max(1, int(family_limit)):
            return False, (
                f"Bounded exploration limit reached for action family {family_key!r}: {family_count} recent attempts. "
                "Stop probing renamed controls in this family. Use evidence from the probes already performed or choose another strategy."
            ), key
    predicted = [record.target_key for record in recent] + [key]
    for width in (2, 3):
        needed = width * TOOLKIT_SEMANTIC_CYCLE_REPEATS
        if len(predicted) < needed:
            continue
        tail = predicted[-needed:]
        pattern = tail[:width]
        if len(set(pattern)) < 2:
            continue
        if all(tail[i * width:(i + 1) * width] == pattern for i in range(TOOLKIT_SEMANTIC_CYCLE_REPEATS)):
            return False, (
                f"Semantic action cycle detected: {' -> '.join(pattern)} repeated {TOOLKIT_SEMANTIC_CYCLE_REPEATS} times. "
                "Do not repeat the open/close, focus, taskbar, or hotkey cycle. Take a different progress-making action."
            ), key
    return True, "", key


def _record_semantic_action(
    turn_state: TurnState,
    tool_name: str,
    target: str,
    target_key: str,
    *,
    family_key: str | None = None,
    outcome: str = "attempted",
) -> None:
    if not turn_state.goal_active:
        return
    turn_state.action_history.append(
        SemanticActionRecord(
            tool=tool_name,
            target=target,
            target_key=target_key,
            family_key=family_key,
            outcome=outcome,
        )
    )
    if len(turn_state.action_history) > TOOLKIT_ACTION_HISTORY_WINDOW * 2:
        del turn_state.action_history[:-TOOLKIT_ACTION_HISTORY_WINDOW]



def goal_requires_precision_visual_evidence(turn_state: TurnState) -> bool:
    text = " ".join(part for part in (
        turn_state.user_request,
        turn_state.objective or "",
        turn_state.success_condition or "",
    ) if part).lower()
    return bool(re.search(r"\b(price|prices|pricing|precio|precios|cost|costo|coste|amount|importe|currency|moneda)\b", text))


def finish_goal(turn_state: TurnState, arguments: dict[str, Any]) -> dict[str, Any]:
    if turn_state.mode != "GOAL" or turn_state.status != "IN_PROGRESS":
        return {
            "ok": False,
            "error": "No active IN_PROGRESS persistent goal exists.",
        }

    status = str(arguments.get("status", "")).strip().upper()
    result = str(arguments.get("result", "")).strip()
    evidence = str(arguments.get("evidence", "")).strip()

    if status not in {"SUCCESS", "NOT_FOUND", "BLOCKED"}:
        return {"ok": False, "error": "status must be SUCCESS, NOT_FOUND, or BLOCKED"}
    if status == "BLOCKED" and not turn_state.host_blocked:
        return {
            "ok": False,
            "error": (
                "BLOCKED is host-authorized only. No host-confirmed blocker exists. "
                "A failed click/navigation/typing/refresh attempt is not sufficient; adapt and continue."
            ),
            "host_authorized_blocked": False,
        }
    if status == "SUCCESS" and goal_requires_precision_visual_evidence(turn_state) and turn_state.precision_inspections < 1:
        return {
            "ok": False,
            "error": (
                "SUCCESS for a price/currency goal requires precision visual evidence. Use desktop_inspect_cell "
                "on the relevant coarse/fine grid cell, read the exact price and currency marker from the clean enlarged crop, then retry finish_goal."
            ),
            "precision_inspections": turn_state.precision_inspections,
            "goal_remains": "IN_PROGRESS",
        }
    if len(result) < 3 or len(evidence) < 3:
        return {
            "ok": False,
            "error": "finish_goal requires a concrete result and evidence; the goal remains IN_PROGRESS",
        }

    turn_state.status = status
    turn_state.result = result
    turn_state.evidence = evidence
    print(f"[Persistent goal resolved: {status}]", flush=True)

    return {
        "ok": True,
        "mode": "GOAL",
        "status": status,
        "authoritative_user_request": turn_state.user_request,
        "objective": turn_state.objective,
        "success_condition": turn_state.success_condition,
        "target": turn_state.target,
        "result": result,
        "evidence": evidence,
        "host_authorized_blocked": turn_state.host_blocked,
        "host_block_reason": turn_state.host_block_reason,
        "instruction": "Goal is resolved. Give the user a concise final answer grounded in this result/evidence.",
    }


# ============================================================================
# Generic MCP/model loop
# ============================================================================

def mcp_result_to_text(result) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []):
        if isinstance(block, TextContent) and block.text:
            parts.append(block.text)
    structured = getattr(result, "structured_content", None)
    if structured not in (None, {}, []):
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))

    text = "\n\n".join(parts) or "Tool completed with no textual output."
    prefix = "MCP TOOL ERROR:\n" if getattr(result, "is_error", False) else "MCP TOOL SUCCESS:\n"
    return (prefix + text)[:MAX_TOOL_RESULT_CHARS]


def parse_tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    raw = tool_call.get("function", {}).get("arguments", "") or "{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return parsed


def tool_may_change_gui(tool_name: str) -> bool:
    return tool_name in {
        "solari_type",
        "solari_key",
        "solari_open",
        "solari_open_app",
        "solari_mouse_move",
    }


def extract_reasoning_delta(delta: Any) -> Any:
    """Return provider reasoning activity without exposing/storing its text.

    OpenAI-compatible providers may expose reasoning activity through
    reasoning, reasoning_content, or extension fields in model_extra.
    We only need a truthy value to keep the watchdog informed that inference is
    alive; this function intentionally does not surface chain-of-thought.
    """
    for attr in ("reasoning", "reasoning_content"):
        value = getattr(delta, attr, None)
        if value:
            return value

    model_extra = getattr(delta, "model_extra", None)
    if isinstance(model_extra, dict):
        for key in ("reasoning", "reasoning_content"):
            value = model_extra.get(key)
            if value:
                return value
    return None


class ModelStreamStalled(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        had_meaningful_output: bool = False,
        had_text_output: bool = False,
        had_tool_output: bool = False,
        had_reasoning_output: bool = False,
        phase: str = "stream",
    ):
        super().__init__(message)
        self.had_meaningful_output = had_meaningful_output
        self.had_text_output = had_text_output
        self.had_tool_output = had_tool_output
        self.had_reasoning_output = had_reasoning_output
        self.phase = phase

    # Backward-compatible alias for any surrounding code/debugging.
    @property
    def had_output(self) -> bool:
        return self.had_meaningful_output


class ModelProviderUnavailable(RuntimeError):
    """Raised after transient provider/API retries are exhausted."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _provider_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _is_transient_provider_error(exc: BaseException) -> tuple[bool, int | None, str]:
    """Classify transient OpenAI-compatible provider/transport errors.

    We intentionally avoid provider-specific exception imports so this remains
    compatible across OpenAI SDK versions and OpenAI-compatible backends.
    """
    status = _provider_status_code(exc)
    name = type(exc).__name__
    lowered = name.lower()

    if status == 429 or (status is not None and 500 <= status <= 599):
        return True, status, f"HTTP {status}"

    # Network/transport failures commonly surfaced by the OpenAI SDK or httpx.
    transient_name_fragments = (
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connectionerror",
        "connecttimeout",
        "readtimeout",
        "writetimeout",
        "pooltimeout",
        "networkerror",
    )
    if any(fragment in lowered for fragment in transient_name_fragments):
        return True, status, name

    # Some compatible providers wrap queue overloads without a usable status
    # attribute. Keep this narrow to explicit transient/overload wording.
    text = str(exc).lower()
    transient_text = (
        "too many requests",
        "rate limit",
        "rate_limit",
        "queue_exceeded",
        "high traffic",
        "temporarily unavailable",
        "service unavailable",
        "connection reset",
        "connection refused",
    )
    if any(marker in text for marker in transient_text):
        return True, status, name

    return False, status, name


def _transient_retry_delay(attempt_number: int) -> float:
    # attempt_number is 1-based.
    delay = MODEL_TRANSIENT_RETRY_BASE_DELAY * (2 ** max(0, attempt_number - 1))
    return min(delay, MODEL_TRANSIENT_RETRY_MAX_DELAY)


def rfb_health_text() -> str:
    cache = FRAME_CACHE
    if cache is None:
        return "RFB desktop stream: unavailable"
    state = "connected" if cache.connected else "reconnecting/disconnected"
    return f"RFB desktop stream: {state}, framebuffer v{cache.version}"


async def _close_model_stream(stream) -> None:
    closer = getattr(stream, "close", None)
    if closer is None:
        return
    try:
        value = closer()
        if asyncio.iscoroutine(value):
            await value
    except Exception:
        pass


async def _stream_model_once(
    openai_tools,
    *,
    allow_tools: bool,
    request_messages: list[dict[str, Any]] | None,
    display_text: bool = True,
):
    actual_messages = compact_messages_for_request(
        request_messages if request_messages is not None else messages
    )
    validate_tool_message_protocol(actual_messages)
    request_args: dict[str, Any] = {
        "model": MODEL,
        "messages": actual_messages,
        "stream": True,
    }

    # extra_body is the escape hatch for provider-specific OpenAI-compatible
    # extensions. reasoning_effort is deliberately injected here too so this
    # script remains compatible with OpenAI SDK versions whose typed method
    # signature may not yet know that parameter.
    extra_body = dict(AI_EXTRA_BODY)
    if AI_REASONING_EFFORT:
        extra_body["reasoning_effort"] = AI_REASONING_EFFORT
    if extra_body:
        request_args["extra_body"] = extra_body

    if allow_tools:
        request_args.update(
            {
                "tools": openai_tools,
                "tool_choice": AI_TOOL_CHOICE,
            }
        )
        if AI_SEND_PARALLEL_TOOL_CALLS:
            # Our host executes tool calls sequentially. Asking the provider not
            # to parallelize makes behavior deterministic when the endpoint
            # supports the standard OpenAI flag. Set
            # AI_SEND_PARALLEL_TOOL_CALLS=0 for endpoints that reject it.
            request_args["parallel_tool_calls"] = False

    image_count = count_request_images(actual_messages)
    if MAX_IMAGES_PER_REQUEST > 0 and image_count > MAX_IMAGES_PER_REQUEST:
        raise RuntimeError(
            f"Internal image compaction error: request contains {image_count} images, "
            f"limit is {MAX_IMAGES_PER_REQUEST}"
        )

    # Getting response headers / constructing the streaming response has its own
    # generous bound. Once streaming begins, the meaningful-output watchdogs
    # below take over.
    try:
        stream = await asyncio.wait_for(
            openai_client.chat.completions.create(**request_args),
            timeout=min(MODEL_HTTP_TIMEOUT, MODEL_FIRST_OUTPUT_TIMEOUT),
        )
    except asyncio.TimeoutError as exc:
        raise ModelStreamStalled(
            f"{AI_PROVIDER_LABEL} request produced no streaming response within {MODEL_FIRST_OUTPUT_TIMEOUT:.0f}s",
            phase="request",
        ) from exc

    assistant_text = ""
    tool_parts: dict[int, dict[str, Any]] = {}
    printed = False
    received_meaningful_output = False
    received_text_output = False
    received_tool_output = False
    received_reasoning_output = False
    iterator = stream.__aiter__()
    started = time.monotonic()
    last_meaningful_at: float | None = None

    try:
        while True:
            now = time.monotonic()
            total_remaining = MODEL_STREAM_TOTAL_TIMEOUT - (now - started)
            if total_remaining <= 0:
                raise ModelStreamStalled(
                    f"{AI_PROVIDER_LABEL} response exceeded {MODEL_STREAM_TOTAL_TIMEOUT:.0f}s total timeout",
                    had_meaningful_output=received_meaningful_output,
                    had_text_output=received_text_output,
                    had_tool_output=received_tool_output,
                    had_reasoning_output=received_reasoning_output,
                    phase="total",
                )

            if last_meaningful_at is None:
                meaningful_deadline_remaining = MODEL_FIRST_OUTPUT_TIMEOUT - (now - started)
                timeout_label = (
                    f"no meaningful model output for {MODEL_FIRST_OUTPUT_TIMEOUT:.0f}s after stream start"
                )
                phase = "first_output"
            else:
                meaningful_deadline_remaining = MODEL_INTER_OUTPUT_TIMEOUT - (now - last_meaningful_at)
                timeout_label = (
                    f"no meaningful model output for {MODEL_INTER_OUTPUT_TIMEOUT:.0f}s during response"
                )
                phase = "inter_output"

            if meaningful_deadline_remaining <= 0:
                raise ModelStreamStalled(
                    f"{AI_PROVIDER_LABEL} response timeout: {timeout_label}",
                    had_meaningful_output=received_meaningful_output,
                    had_text_output=received_text_output,
                    had_tool_output=received_tool_output,
                    had_reasoning_output=received_reasoning_output,
                    phase=phase,
                )

            # Empty/keepalive SSE chunks can arrive frequently. We wait only up
            # to the *remaining meaningful-output deadline*, so those chunks do
            # not reset the watchdog.
            chunk_timeout = min(total_remaining, meaningful_deadline_remaining)
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=chunk_timeout)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise ModelStreamStalled(
                    f"{AI_PROVIDER_LABEL} response timeout: {timeout_label}",
                    had_meaningful_output=received_meaningful_output,
                    had_text_output=received_text_output,
                    had_tool_output=received_tool_output,
                    had_reasoning_output=received_reasoning_output,
                    phase=phase,
                ) from exc

            # IMPORTANT: a transport chunk by itself is not meaningful output.
            # Ignore keepalive/metadata chunks for timeout/retry state.
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            chunk_was_meaningful = False

            # Reasoning-capable providers may stream intermediate reasoning in
            # a separate extension field before content/tool deltas. Count it
            # as activity so the watchdog does not produce a false timeout, but
            # intentionally do not print, store, or resend that private text.
            if extract_reasoning_delta(delta):
                chunk_was_meaningful = True
                received_reasoning_output = True

            if delta.content:
                chunk_was_meaningful = True
                received_text_output = True
                if display_text:
                    if not printed:
                        print("AI: ", end="", flush=True)
                        printed = True
                    print(delta.content, end="", flush=True)
                assistant_text += delta.content

            if delta.tool_calls:
                chunk_was_meaningful = True
                received_tool_output = True
                for call in delta.tool_calls:
                    current = tool_parts.setdefault(
                        call.index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if call.id and not current["id"]:
                        current["id"] = call.id
                    if call.function:
                        current["function"]["name"] += call.function.name or ""
                        current["function"]["arguments"] += call.function.arguments or ""

            if chunk_was_meaningful:
                received_meaningful_output = True
                last_meaningful_at = time.monotonic()
    finally:
        await _close_model_stream(stream)

    if printed:
        print()

    return assistant_text, [tool_parts[index] for index in sorted(tool_parts)]


async def stream_model_response(
    openai_tools,
    *,
    allow_tools: bool = True,
    request_messages: list[dict[str, Any]] | None = None,
    display_text: bool = True,
):
    # No desktop action is executed until a complete model response is returned
    # from this function. Therefore timeouts, partial tool-call streams, and
    # transient provider failures are safe to discard and retry here.
    stall_retries = 0
    transient_retries = 0

    while True:
        try:
            return await _stream_model_once(
                openai_tools,
                allow_tools=allow_tools,
                request_messages=request_messages,
                display_text=display_text,
            )

        except ModelStreamStalled as exc:
            if stall_retries >= MODEL_STREAM_RETRIES:
                raise
            stall_retries += 1
            print(
                f"[{AI_PROVIDER_LABEL} response timeout; retrying inference "
                f"{stall_retries}/{MODEL_STREAM_RETRIES}: {exc}]",
                flush=True,
            )
            print(f"[{rfb_health_text()}]", flush=True)

        except Exception as exc:
            transient, status, reason = _is_transient_provider_error(exc)
            if not transient:
                raise

            if transient_retries >= MODEL_TRANSIENT_RETRIES:
                status_text = f" HTTP {status}" if status is not None else ""
                raise ModelProviderUnavailable(
                    f"{AI_PROVIDER_LABEL} transient provider error{status_text} "
                    f"persisted after {MODEL_TRANSIENT_RETRIES} retries: {exc}",
                    status_code=status,
                ) from exc

            transient_retries += 1
            delay = _transient_retry_delay(transient_retries)
            status_text = f" HTTP {status}" if status is not None else ""
            print(
                f"[{AI_PROVIDER_LABEL} transient API error{status_text}; retrying "
                f"{transient_retries}/{MODEL_TRANSIENT_RETRIES} in {delay:.1f}s: {reason}]",
                flush=True,
            )
            print(f"[{rfb_health_text()}]", flush=True)
            await asyncio.sleep(delay)


async def _execute_local_vdi_tool(tool_name: str) -> str:
    """Run a host-owned VDI MCP function and return redacted JSON text."""
    if vdi_bridge is None:
        return json.dumps(
            {
                "ok": False,
                "ready": False,
                "error": "VDI bridge is unavailable in this host environment",
                "detail": _VDI_IMPORT_ERROR,
            },
            ensure_ascii=False,
        )

    function = getattr(vdi_bridge, tool_name, None)
    if not callable(function):
        return json.dumps(
            {"ok": False, "ready": False, "error": f"VDI bridge does not expose {tool_name}"},
            ensure_ascii=False,
        )
    try:
        result = function()
        if inspect.isawaitable(result):
            result = await result
        # The bridge is expected to redact already; apply the agent-level
        # sanitizer once more before placing anything in model history/logs.
        return json.dumps(_redact(result), ensure_ascii=False, default=str)
    except Exception as exc:
        return json.dumps(
            {"ok": False, "ready": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


HOST_DENIED_LOW_LEVEL_TOOLS = {
    SCREENSHOT_TOOL,
    RAW_CLICK_TOOL,
    "solari_key",
    "solari_type",
    "solari_open_app",
}


DEPRECATED_HOST_VISUAL_TOOLS = {
    "desktop_click_target",
    "desktop_double_click_target",
    "desktop_click_grid",
    "desktop_double_click_grid",
    "desktop_zoom_region",
    "desktop_inspect_cell",
}


# Compatibility aliases for model/provider tool-name drift. These aliases are
# intentionally NOT advertised in the model tool schema; they only recover an
# already-emitted obsolete name and route it to the supported host keyboard
# primitive rather than allowing an unnecessary Windows On-Screen Keyboard
# workaround.
COMPAT_KEYBOARD_TOOL_ALIASES = {
    "vdi_type_text": "desktop_type_text",
    "desktop_type": "desktop_type_text",
    "desktop_key_native": "desktop_hotkey",
}


def _normalize_compat_keyboard_call(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any], str | None]:
    mapped = COMPAT_KEYBOARD_TOOL_ALIASES.get(tool_name)
    if mapped is None:
        return tool_name, arguments, None

    if mapped == "desktop_type_text":
        value = None
        for key in ("text", "value", "content", "string", "characters"):
            if key in arguments and arguments.get(key) is not None:
                value = arguments.get(key)
                break
        if value is None:
            return mapped, {}, (
                f"Obsolete keyboard tool {tool_name!r} was redirected to desktop_type_text, "
                "but no literal text argument was provided. Retry with "
                "desktop_type_text({{text: ...}}). Do not use the Windows On-Screen Keyboard."
            )
        return mapped, {
            "text": str(value),
            "target": str(arguments.get("target") or "focused control"),
            "scope": _normalize_hotkey_scope(str(arguments.get("scope") or "local")),
            "purpose": str(arguments.get("purpose") or "Entering information"),
        }, None

    keys = arguments.get("keys")
    if not isinstance(keys, list) or not keys:
        raw = arguments.get("key") or arguments.get("hotkey") or arguments.get("chord")
        if isinstance(raw, str) and raw.strip():
            keys = [part.strip() for part in raw.split("+") if part.strip()]
    if not isinstance(keys, list) or not keys:
        return mapped, {}, (
            f"Obsolete keyboard tool {tool_name!r} was redirected to desktop_hotkey, "
            "but no key/chord was provided. Retry with desktop_hotkey({{keys: [...] }}). "
            "Do not use the Windows On-Screen Keyboard."
        )
    return mapped, {
        "keys": [str(k) for k in keys],
        "scope": _normalize_hotkey_scope(str(arguments.get("scope") or "local")),
        "purpose": str(arguments.get("purpose") or "Using the keyboard"),
    }, None


async def execute_tool_call(mcp, tool_call, turn_state: TurnState) -> tuple[str, list[VisionImage]]:
    tool_name = tool_call["function"]["name"]
    try:
        arguments = parse_tool_arguments(tool_call)
    except Exception as exc:
        return f"TOOL ERROR: invalid arguments: {exc}", []

    original_tool_name = tool_name
    tool_name, arguments, alias_error = _normalize_compat_keyboard_call(tool_name, arguments)
    if original_tool_name != tool_name:
        print(
            f"[toolkit-keyboard] redirected obsolete tool {original_tool_name!r} -> {tool_name!r}",
            flush=True,
        )
        if alias_error:
            return "TOOL ERROR: " + alias_error, []

    if tool_name in HOST_DENIED_LOW_LEVEL_TOOLS:
        print(f"[Denied low-level/convenience desktop tool: {tool_name}]", flush=True)
        return (
            "TOOL ERROR: Toolkit agent-native mode requires the clean framebuffer plus desktop_click_native / "
            "desktop_double_click_native / desktop_hotkey / desktop_type_text. Raw screenshot/click/key/type and "
            "application-launch convenience tools are host-disabled for this experiment.",
            [],
        )

    if tool_name in HOST_OWNED_LIFECYCLE_TOOLS:
        print(f"[Denied host-owned lifecycle tool: {tool_name}]", flush=True)
        return (
            "TOOL ERROR: desktop/session lifecycle is host-owned. "
            "Do not create, connect, list, or kill Solari sessions. "
            "Operate the already-attached desktop using the GUI tools.",
            [],
        )

    if tool_name in LOCAL_VDI_TOOLS:
        if tool_name == "vdi_vpn_disconnect":
            print("[VDI host tool requested: disconnect]", flush=True)
        else:
            print(f"[VDI host tool requested: {tool_name}]", flush=True)
        return "VDI TOOL RESULT:\n" + await _execute_local_vdi_tool(tool_name), []

    if tool_name in LOCAL_SECURITY_TOOLS:
        if tool_name == "security_vault_status":
            result = await security_vault_status()
            return "SECURITY VAULT RESULT:\n" + json.dumps(result, ensure_ascii=False, default=str), []
        if tool_name == "security_vault_search":
            result = await security_vault_search(arguments.get("query", ""), arguments.get("limit", 10))
            return "SECURITY VAULT RESULT:\n" + json.dumps(result, ensure_ascii=False, default=str), []
        field = {
            "security_vault_type_username": "username",
            "security_vault_type_password": "password",
            "security_vault_type_totp": "totp",
        }[tool_name]
        if turn_state.mode == "UNDECIDED":
            turn_state.mode = "ACTION"
            turn_state.status = "NONE"
        submit_default = field == "totp"
        result, images = await security_vault_type_secret(
            field,
            arguments.get("credential", ""),
            arguments.get("frame_version"),
            arguments.get("x_px"),
            arguments.get("y_px"),
            submit=bool(arguments.get("submit", submit_default)),
        )
        return "SECURITY VAULT RESULT:\n" + json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "start_goal":
        result = start_goal(turn_state, arguments)
        return json.dumps(result, ensure_ascii=False, default=str), []

    if tool_name == "goal_remember":
        result = goal_remember(turn_state, arguments)
        return json.dumps(result, ensure_ascii=False, default=str), []

    if tool_name == "finish_goal":
        result = finish_goal(turn_state, arguments)
        return json.dumps(result, ensure_ascii=False, default=str), []

    if turn_state.mode == "UNDECIDED":
        # Any environment/tool action without start_goal is a bounded ACTION.
        turn_state.mode = "ACTION"
        turn_state.status = "NONE"

    if tool_name in {"desktop_click_native", "desktop_double_click_native"}:
        target = str(arguments.get("target", "visual target"))
        target_key, family_key = _pointer_action_keys(arguments)
        allowed, loop_reason, _ = _semantic_loop_guard(
            turn_state,
            tool_name,
            target,
            target_key=target_key,
            family_key=family_key,
            family_limit=TOOLKIT_TASKBAR_PROBE_FAMILY_LIMIT if family_key else None,
        )
        if not allowed:
            turn_state.semantic_loop_rejections += 1
            current = await FRAME_CACHE.snapshot() if FRAME_CACHE is not None else None
            images = [frame_to_clean_vision_image(current, "semantic_action_loop_rejected")] if current is not None else []
            print(f"[toolkit-loop-guard] rejected target={target!r} key={target_key!r} family={family_key!r}: {loop_reason}", flush=True)
            return json.dumps({
                "success": False,
                "semantic_loop_detected": True,
                "reason": loop_reason,
                "durable_goal_memory": _goal_memory_summary(turn_state, max_chars=2400),
                "instruction": "Do not rename/retry the same semantic control. Reuse the post-action evidence or choose a genuinely different progress-making action.",
            }, ensure_ascii=False), images

        grounded, grounding_reason = _validate_visual_identity_contract(arguments)
        if not grounded:
            _record_semantic_action(
                turn_state, tool_name, target, target_key, family_key=family_key, outcome="visual_identity_rejected"
            )
            current = await FRAME_CACHE.snapshot() if FRAME_CACHE is not None else None
            images = [frame_to_clean_vision_image(current, "visual_identity_rejected")] if current is not None else []
            print(
                f"[toolkit-visual-identity] rejected target={target!r} key={target_key!r} family={family_key!r}: {grounding_reason}",
                flush=True,
            )
            return json.dumps({
                "success": False,
                "visual_identity_rejected": True,
                "reason": grounding_reason,
                "attempt_recorded": True,
                "instruction": (
                    "Re-inspect the attached current frame. Use exact visible label evidence when identity matters. "
                    "For an unlabeled taskbar button, use evidence_basis=spatial_control and describe only its visible position; do not guess the application name. "
                    "Do not keep renaming the same slot after rejection because rejected attempts count toward loop protection."
                ),
            }, ensure_ascii=False), images

        native_fn = desktop_double_click_native if tool_name == "desktop_double_click_native" else desktop_click_native
        result, images = await native_fn(
            target,
            arguments.get("frame_version"),
            arguments.get("x_px"),
            arguments.get("y_px"),
        )
        outcome = "success" if isinstance(result, dict) and result.get("success") else "input_failed"
        _record_semantic_action(
            turn_state, tool_name, target, target_key, family_key=family_key, outcome=outcome
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name in DEPRECATED_HOST_VISUAL_TOOLS:
        current = await FRAME_CACHE.snapshot() if FRAME_CACHE is not None else None
        images = [frame_to_clean_vision_image(current, "deprecated_visual_tool_denied")] if current is not None else []
        return (
            json.dumps({
                "success": False,
                "reason": (
                    f"{tool_name} is disabled in Toolkit. Inspect the clean framebuffer yourself and use "
                    "desktop_click_native or desktop_double_click_native with native x_px/y_px."
                ),
                "host_visual_localization_calls": 0,
            }, ensure_ascii=False),
            images,
        )

    if tool_name == "desktop_click_target":
        result, images = await desktop_click_target(
            mcp,
            arguments.get("target", ""),
            arguments.get("region", "full"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_double_click_target":
        result, images = await desktop_double_click_target(
            mcp,
            arguments.get("target", ""),
            arguments.get("region", "full"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_click_grid":
        result, images = await desktop_click_grid(
            mcp,
            arguments.get("target", "visual target"),
            arguments.get("cell", ""),
            arguments.get("frame_version"),
            arguments.get("zoom_id"),
            arguments.get("fallback_token"),
            arguments.get("position", "center"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_double_click_grid":
        result, images = await desktop_double_click_grid(
            mcp,
            arguments.get("target", "visual target"),
            arguments.get("cell", ""),
            arguments.get("frame_version"),
            arguments.get("zoom_id"),
            arguments.get("fallback_token"),
            arguments.get("position", "center"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_zoom_region":
        result, images = await desktop_zoom_region(
            arguments.get("target", "visual target"),
            arguments.get("cell", ""),
            arguments.get("frame_version"),
            arguments.get("source_zoom_id"),
            arguments.get("fallback_token"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_inspect_cell":
        result, images = await desktop_inspect_cell(
            turn_state,
            arguments.get("target", "precision value"),
            arguments.get("cell", ""),
            arguments.get("frame_version"),
            arguments.get("zoom_id"),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_hotkey":
        result, images = await desktop_hotkey(
            turn_state,
            arguments.get("keys", []),
            arguments.get("purpose", ""),
            arguments.get("scope", ""),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_type_text":
        result, images = await desktop_type_text(
            turn_state,
            arguments.get("text", ""),
            arguments.get("target", ""),
            arguments.get("scope", ""),
            arguments.get("purpose", ""),
        )
        return json.dumps(result, ensure_ascii=False, default=str), images

    if tool_name == "desktop_refresh_view":
        return await desktop_refresh_view()

    print(f"[Using MCP tool: {tool_name}]", flush=True)

    # The host owns the active session. Discard any model-supplied session value
    # and inject the real one whenever this MCP schema supports a session field.
    arguments = inject_active_session(tool_name, arguments)

    before_version = FRAME_CACHE.version if FRAME_CACHE is not None else 0
    result = await mcp.call_tool(tool_name, arguments)
    print(f"[MCP tool completed: {tool_name}]", flush=True)

    images: list[VisionImage] = []
    if (
        not getattr(result, "is_error", False)
        and FRAME_CACHE is not None
        and tool_may_change_gui(tool_name)
    ):
        _invalidate_semantic_grounding_cache(f"gui-tool:{tool_name}")
        _invalidate_grid_fallbacks(f"gui-tool:{tool_name}")
        post = await FRAME_CACHE.wait_for_change(before_version)
        if post.version > before_version:
            images.append(frame_to_vision_image(post, f"post_{tool_name}"))
        print(
            f"[Live framebuffer after {tool_name}: v{before_version} -> v{post.version} "
            f"changed={post.version > before_version}]",
            flush=True,
        )

    return mcp_result_to_text(result), images


def contains_literal_tool_markup(text: str) -> bool:
    lowered = (text or "").lower()
    markers = (
        "<tool_call>",
        "</tool_call>",
        "<function=",
        "<parameter=",
        '"tool_calls"',
    )
    return any(marker in lowered for marker in markers)


def deterministic_goal_final(turn_state: TurnState) -> str:
    result = (turn_state.result or "").strip()
    evidence = (turn_state.evidence or "").strip()
    status = turn_state.status

    if status == "SUCCESS":
        if evidence and evidence not in result:
            return f"{result}\n\nEvidence: {evidence}"
        return result or "The requested goal was completed successfully."
    if status == "NOT_FOUND":
        base = result or "I could not find the requested result after checking the available paths."
        return f"{base}\n\nEvidence: {evidence}" if evidence else base
    if status == "BLOCKED":
        base = result or "I could not complete the requested goal because further progress was blocked."
        return f"{base}\n\nEvidence: {evidence}" if evidence else base
    return result or "The goal ended without a resolved result."


def _present_final_answer(text: str) -> None:
    if DEMO_UI.demo:
        DEMO_UI.final_answer(text)
    else:
        print(f"AI: {text}", flush=True)


def _present_friendly_error(text: str) -> None:
    if DEMO_UI.demo:
        DEMO_UI.friendly_error(text)
    else:
        print(f"AI: {text}", flush=True)


async def force_final_answer(
    openai_tools,
    reason: str,
    *,
    turn_state: TurnState | None = None,
) -> None:
    temporary = list(messages)
    if turn_state is not None and turn_state.mode == "GOAL":
        temporary.append({"role": "user", "content": goal_context_text(turn_state)})
    temporary.append(
        {
            "role": "user",
            "content": (
                "[Host orchestration instruction] "
                + reason
                + " No more tools are available for this inference. Answer the original user "
                  "using ONLY the existing tool results and desktop images. Do not add general/model knowledge "
                  "to explain missing evidence. Be explicit if the requested goal was not completed or verification "
                  "was incomplete. Output ordinary prose only; do not print tool-call markup."
            ),
        }
    )
    try:
        final_text, _ = await stream_model_response(
            openai_tools,
            allow_tools=False,
            request_messages=temporary,
            display_text=False,
        )
        if not final_text.strip() or contains_literal_tool_markup(final_text):
            if turn_state is not None and turn_state.mode == "GOAL":
                final_text = deterministic_goal_final(turn_state)
            else:
                final_text = reason
    except ModelStreamStalled as exc:
        final_text = (
            deterministic_goal_final(turn_state)
            if turn_state is not None and turn_state.mode == "GOAL"
            else "The model response stream stalled, so I stopped this turn safely. "
                 "The desktop session and live framebuffer are still connected."
        )
        print(f"[{AI_PROVIDER_LABEL} response timeout during final response: {exc}]", flush=True)
        print(f"[{rfb_health_text()}]", flush=True)
    except ModelProviderUnavailable as exc:
        final_text = (
            deterministic_goal_final(turn_state)
            if turn_state is not None and turn_state.mode == "GOAL"
            else "The AI provider remained temporarily unavailable after automatic retries. "
                 "The desktop session and live framebuffer are still connected."
        )
        print(f"[{AI_PROVIDER_LABEL} provider unavailable during final response: {exc}]", flush=True)
        print(f"[{rfb_health_text()}]", flush=True)

    _present_final_answer(final_text)
    messages.append({"role": "assistant", "content": final_text})


async def emit_resolved_goal_answer(openai_tools, turn_state: TurnState) -> None:
    temporary = list(messages)
    temporary.append({"role": "user", "content": goal_context_text(turn_state)})
    temporary.append(
        {
            "role": "user",
            "content": (
                "[Host orchestration instruction] The persistent goal is now resolved with status "
                f"{turn_state.status}. Give the human the final answer now. Use ONLY the recorded result "
                "and desktop/tool evidence from this turn. Do not add assumed release dates, inventory status, "
                "product availability, or other general/model knowledge that was not observed. Answer the original "
                "request directly, and do not perform more tools. "
                "Tools are disabled for this inference. Output ordinary prose only and NEVER print "
                "literal <tool_call>, <function=...>, or <parameter=...> markup."
            ),
        }
    )
    try:
        final_text, _ = await stream_model_response(
            openai_tools,
            allow_tools=False,
            request_messages=temporary,
            display_text=False,
        )
        if not final_text.strip() or contains_literal_tool_markup(final_text):
            print("[Resolved-goal final contained tool markup; using deterministic host final]", flush=True)
            final_text = deterministic_goal_final(turn_state)
    except ModelStreamStalled as exc:
        final_text = deterministic_goal_final(turn_state)
        print(f"[{AI_PROVIDER_LABEL} response timeout during resolved-goal answer: {exc}]", flush=True)
        print(f"[{rfb_health_text()}]", flush=True)
    except ModelProviderUnavailable as exc:
        final_text = deterministic_goal_final(turn_state)
        print(f"[{AI_PROVIDER_LABEL} provider unavailable during resolved-goal answer: {exc}]", flush=True)
        print(f"[{rfb_health_text()}]", flush=True)

    _present_final_answer(final_text)
    messages.append({"role": "assistant", "content": final_text})


async def _vdi_preflight() -> None:
    """Make the VDI path ready before the first user prompt."""
    if not VDI_PREPARE_ON_START:
        print("[Host VDI preflight disabled by VDI_PREPARE_ON_START]", flush=True)
        return
    if vdi_bridge is None:
        print(
            f"[Host VDI preflight unavailable: {_VDI_IMPORT_ERROR or 'module not installed'}]",
            flush=True,
        )
        return

    connect = getattr(vdi_bridge, "vdi_vpn_connect", None)
    if callable(connect):
        try:
            result = connect()
            if inspect.isawaitable(result):
                result = await result
            sanitized = _redact(result)
            ready = bool(sanitized.get("ready")) if isinstance(sanitized, dict) else False
            print(f"[Host VDI preflight] VPN/relay ready={ready}", flush=True)
        except Exception as exc:
            print(f"[Host VDI preflight warning: {type(exc).__name__}: {exc}]", flush=True)
    else:
        print("[Host VDI preflight warning: vdi_vpn_connect is not available]", flush=True)

    prepare = getattr(vdi_bridge, "vdi_remmina_prepare_profile", None)
    if callable(prepare):
        try:
            result = prepare()
            if inspect.isawaitable(result):
                result = await result
            sanitized = _redact(result)
            persisted = bool(sanitized.get("profile_persisted")) if isinstance(sanitized, dict) else False
            print(f"[Host VDI preflight] persistent Remmina profile ready={persisted}", flush=True)
        except Exception as exc:
            print(f"[Host VDI profile warning: {type(exc).__name__}: {exc}]", flush=True)


async def chat(
    mcp,
    openai_tools,
    user_message: str,
    *,
    attach_desktop: bool = True,
) -> None:
    DEMO_UI.begin_turn()
    if attach_desktop:
        await add_user_message_with_desktop(user_message)
    else:
        messages.append({"role": "user", "content": user_message})

    turn_state = TurnState(user_request=user_message)
    last_blocking_signature: str | None = None
    total_tool_calls = 0          # environmental actions; refresh does NOT count
    blocking_tool_calls = 0
    refresh_calls = 0
    round_index = 0
    budget_resolution_rounds = 0
    observation_signature_counts: dict[str, int] = {}
    observation_only_rounds = 0

    while True:
        round_index += 1
        round_limit = MAX_GOAL_TOOL_ROUNDS if turn_state.mode == "GOAL" else MAX_TOOL_ROUNDS
        if round_index > round_limit:
            if turn_state.goal_active:
                turn_state.host_blocked = True
                turn_state.host_block_reason = "The host hard model/tool-round safety ceiling was reached."
                turn_state.status = "BLOCKED"
                turn_state.result = (
                    "The host's hard model/tool-round safety ceiling was reached before the objective was completed."
                )
                turn_state.evidence = (
                    f"Reached {round_limit} model/tool rounds. This is a host safety ceiling, not evidence that "
                    "the requested information does not exist."
                )
                await emit_resolved_goal_answer(openai_tools, turn_state)
            else:
                await force_final_answer(
                    openai_tools,
                    f"The maximum tool budget of {round_limit} rounds was reached.",
                    turn_state=turn_state,
                )
            return

        request_messages = request_messages_for_turn(turn_state)
        # Demo mode shows a polished progress narrative after each model round;
        # raw streamed model text and all host diagnostics stay in the debug log.
        display_text = (not DEMO_UI.demo) and (SHOW_GOAL_PROGRESS or not turn_state.goal_active)

        try:
            assistant_text, tool_calls = await stream_model_response(
                openai_tools,
                allow_tools=True,
                request_messages=request_messages,
                display_text=display_text,
            )
        except ModelStreamStalled as exc:
            fallback = (
                f"The {AI_PROVIDER_LABEL} model response timed out after automatic retries, so I stopped this turn safely. "
                "This was not an RFB/desktop-stream failure; the desktop session remains available."
            )
            print(f"[{AI_PROVIDER_LABEL} response timeout after retries: {exc}]", flush=True)
            print(f"[{rfb_health_text()}]", flush=True)
            _present_friendly_error(fallback)
            messages.append({"role": "assistant", "content": fallback})
            return
        except ModelProviderUnavailable as exc:
            fallback = (
                f"The {AI_PROVIDER_LABEL} API remained temporarily unavailable after automatic retries, "
                "so I stopped this turn without changing the desktop session. "
                "The Solari desktop and live RFB framebuffer remain available for the next request."
            )
            print(f"[{AI_PROVIDER_LABEL} provider unavailable after retries: {exc}]", flush=True)
            print(f"[{rfb_health_text()}]", flush=True)
            _present_friendly_error(fallback)
            messages.append({"role": "assistant", "content": fallback})
            return

        if tool_calls and DEMO_UI.demo:
            # Ordinary assistant content is safe user-facing progress. Provider
            # reasoning extension fields are never surfaced by the host.
            if assistant_text.strip():
                DEMO_UI.progress(assistant_text)

        if not tool_calls:
            if turn_state.goal_active:
                turn_state.premature_finishes += 1
                messages.append({"role": "assistant", "content": assistant_text or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[Host goal gate] That response attempted to end the turn while the persistent "
                            "goal is still IN_PROGRESS, so it was not shown as the final answer. Continue "
                            "working toward the authoritative user request using tools, or call finish_goal "
                            "with SUCCESS/NOT_FOUND and concrete desktop evidence. BLOCKED is allowed only when "
                            "the host explicitly authorizes it. A host tool denial by itself is not a blocker."
                        ),
                    }
                )
                print(
                    f"[Goal gate: suppressed premature final response "
                    f"{turn_state.premature_finishes}/{MAX_PREMATURE_GOAL_FINALS}]",
                    flush=True,
                )
                if turn_state.premature_finishes >= MAX_PREMATURE_GOAL_FINALS:
                    turn_state.host_blocked = True
                    turn_state.host_block_reason = "Repeated premature final responses exhausted the host continuation limit."
                    turn_state.status = "BLOCKED"
                    turn_state.result = "The model repeatedly attempted to stop before resolving the persistent goal."
                    turn_state.evidence = (
                        f"The host suppressed {turn_state.premature_finishes} premature final responses "
                        "while status remained IN_PROGRESS."
                    )
                    await emit_resolved_goal_answer(openai_tools, turn_state)
                    return
                continue

            if turn_state.mode == "UNDECIDED":
                turn_state.mode = "CHAT"
            if DEMO_UI.demo:
                DEMO_UI.final_answer(assistant_text)
            messages.append({"role": "assistant", "content": assistant_text})
            return

        messages.append(
            {
                "role": "assistant",
                "content": assistant_text or "",
                "tool_calls": tool_calls,
            }
        )

        images_this_round: list[VisionImage] = []
        rejected_this_round = False
        observation_rejected_this_round = False
        hard_budget_rejected_this_round = False
        post_tool_host_notes: list[str] = []
        round_has_action = False

        for tool_call in tool_calls:
            tool_call_id = tool_call.get("id")
            if not tool_call_id:
                raise RuntimeError("Model returned a tool call without a tool_call id")

            name = tool_call["function"]["name"]
            raw_arguments = tool_call["function"].get("arguments", "") or "{}"
            try:
                canonical_arguments = json.dumps(
                    json.loads(raw_arguments), sort_keys=True, separators=(",", ":")
                )
            except Exception:
                canonical_arguments = raw_arguments
            signature = name + ":" + canonical_arguments

            if DEMO_UI.demo:
                try:
                    progress_args = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(progress_args, dict):
                        progress_args = {}
                except Exception:
                    progress_args = {}
                DEMO_UI.progress_for_tool(name, progress_args)

            state_tool = name in {"start_goal", "goal_remember", "finish_goal"}
            refresh_tool = name == "desktop_refresh_view"
            observation_tool = is_observation_only_tool(name)

            # Refresh is an observation, not an environment action. It has its own
            # quota and does not burn the persistent goal's action budget.
            if not state_tool and not refresh_tool and not observation_tool:
                total_tool_calls += 1

            goal_budget = (
                MAX_GOAL_TOOL_CALLS_PER_TURN
                if turn_state.mode == "GOAL"
                else MAX_TOTAL_TOOL_CALLS_PER_TURN
            )

            if (
                turn_state.mode == "GOAL"
                and not state_tool
                and not refresh_tool
                and not observation_tool
                and total_tool_calls >= GOAL_ACTION_WARNING_AT
                and not turn_state.budget_warning_sent
            ):
                turn_state.budget_warning_sent = True
                # IMPORTANT: defer this user-role host note until *after* every
                # tool result for the current assistant tool_calls message has
                # been appended. OpenAI-compatible APIs require assistant
                # tool_calls -> contiguous role="tool" replies with nothing
                # inserted between them.
                post_tool_host_notes.append(
                    (
                        f"[Host budget note] This persistent goal has used {total_tool_calls} environmental "
                        f"actions. The hard ceiling is {MAX_GOAL_TOOL_CALLS_PER_TURN}. Stay focused on the "
                        "authoritative objective; avoid unrelated exploration and reuse current evidence."
                    )
                )
                print(
                    f"[Goal action budget warning: {total_tool_calls}/{MAX_GOAL_TOOL_CALLS_PER_TURN}]",
                    flush=True,
                )

            if not state_tool and not refresh_tool and not observation_tool and total_tool_calls > goal_budget:
                turn_state.host_action_budget_exhausted = turn_state.mode == "GOAL"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": (
                            "TOOL CALL REJECTED: the hard environmental-action safety ceiling for this turn "
                            "has been reached. This rejection does NOT prove the objective is impossible. "
                            "Use existing evidence and call finish_goal with the most accurate status."
                        ),
                    }
                )
                rejected_this_round = True
                hard_budget_rejected_this_round = True
                continue

            if observation_tool:
                observation_signature_counts[signature] = observation_signature_counts.get(signature, 0) + 1
                if observation_signature_counts[signature] > MAX_REPEATED_OBSERVATION_CALLS:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                "TOOL CALL REJECTED: this identical observation was already requested several times. "
                                "Use the latest image, choose a different grid cell, perform a meaningful GUI "
                                "action, or answer with the evidence already available."
                            ),
                        }
                    )
                    rejected_this_round = True
                    observation_rejected_this_round = True
                    continue
            else:
                observation_signature_counts.clear()
                round_has_action = True

            if name == "desktop_refresh_view":
                refresh_calls += 1
                refresh_limit = (
                    MAX_GOAL_REFRESH_CALLS_PER_TURN
                    if turn_state.mode == "GOAL"
                    else MAX_REFRESH_CALLS_PER_TURN
                )
                if refresh_calls > refresh_limit:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                "TOOL CALL REJECTED: the refresh quota was reached. This does NOT block the "
                                "goal. Use the most recent framebuffer already provided or perform a meaningful "
                                "action that changes the screen."
                            ),
                        }
                    )
                    rejected_this_round = True
                    observation_rejected_this_round = True
                    continue

            if name in BLOCKING_TOOLS:
                blocking_tool_calls += 1
                if blocking_tool_calls > MAX_BLOCKING_TOOL_CALLS_PER_TURN:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                "TOOL CALL REJECTED: too many shell/code calls in one desktop turn. "
                                "This does NOT block a persistent goal; use GUI tools or prior results."
                            ),
                        }
                    )
                    rejected_this_round = True
                    continue

                if signature == last_blocking_signature:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": (
                                "TOOL CALL REJECTED: this identical blocking call was just executed. "
                                "Use the previous result or choose a different path."
                            ),
                        }
                    )
                    rejected_this_round = True
                    continue
                last_blocking_signature = signature
            else:
                last_blocking_signature = None

            try:
                tool_text, tool_images = await execute_tool_call(mcp, tool_call, turn_state)
                if not isinstance(tool_text, str):
                    tool_text = json.dumps(tool_text, ensure_ascii=False, default=str)
            except Exception as exc:
                # Always close the assistant tool_calls message with a result.
                # A failed OCR/MCP/GUI action must not leave an orphaned
                # tool_call that corrupts the next user turn.
                rejected_this_round = True
                tool_images = []
                tool_text = _redact(
                    f"TOOL ERROR: {type(exc).__name__}: {exc}"
                )
                print(
                    f"[Tool execution error: {type(exc).__name__}: {exc}]",
                    flush=True,
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_text,
                }
            )
            images_this_round.extend(tool_images)

        if images_this_round:
            messages.append(build_vision_message(images_this_round))
            print(
                f"[Sent {len(images_this_round)} live framebuffer image(s) to {MODEL}]",
                flush=True,
            )

        if round_has_action:
            observation_only_rounds = 0
        elif tool_calls:
            observation_only_rounds += 1
            if observation_only_rounds >= MAX_OBSERVATION_ONLY_ROUNDS:
                post_tool_host_notes.append(
                    "[Host observation-loop guard] Several rounds have only inspected the desktop without "
                    "changing it. Use the latest framebuffer and take a concrete GUI action, or resolve the "
                    "request with the evidence already available."
                )

        # Host/user orchestration notes are safe only after all role="tool"
        # replies for the immediately preceding assistant tool_calls message
        # have been recorded.
        for host_note in post_tool_host_notes:
            messages.append({"role": "user", "content": host_note})

        # Catch any future ordering regression here, at the host boundary,
        # before the next provider request.
        validate_tool_message_protocol(messages)

        if turn_state.goal_terminal:
            await emit_resolved_goal_answer(openai_tools, turn_state)
            return

        # IMPORTANT: a denied individual action no longer terminalizes a GOAL.
        # Only the hard action ceiling enters a short resolution-only phase.
        if hard_budget_rejected_this_round and turn_state.goal_active:
            turn_state.host_blocked = True
            turn_state.host_block_reason = (
                "The host hard environmental-action safety ceiling was reached; no further desktop actions "
                "will execute in this turn."
            )
            budget_resolution_rounds += 1
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Host hard-budget state] No further environmental actions will execute in this turn. "
                        "Do not emit another desktop action. Resolve the persistent goal now with finish_goal "
                        "using SUCCESS only if the exact success condition is already evidenced, NOT_FOUND only "
                        "if desktop evidence genuinely establishes absence, or BLOCKED because the host has now "
                        "authorized it (host_authorized_blocked=true)."
                    ),
                }
            )
            print(
                f"[Goal hard action ceiling reached; resolution round "
                f"{budget_resolution_rounds}/{MAX_GOAL_BUDGET_RESOLUTION_ROUNDS}]",
                flush=True,
            )
            if budget_resolution_rounds >= MAX_GOAL_BUDGET_RESOLUTION_ROUNDS:
                turn_state.host_blocked = True
                if not turn_state.host_block_reason:
                    turn_state.host_block_reason = "The host environmental-action ceiling prevented further actions."
                turn_state.status = "BLOCKED"
                turn_state.result = (
                    "The host's hard environmental-action safety ceiling was reached before the objective was resolved."
                )
                turn_state.evidence = (
                    f"The turn used {total_tool_calls - 1} allowed environmental actions before the next action "
                    f"was rejected at the configured ceiling of {MAX_GOAL_TOOL_CALLS_PER_TURN}."
                )
                await emit_resolved_goal_answer(openai_tools, turn_state)
                return
            continue

        if rejected_this_round and observation_rejected_this_round:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Host observation-loop note] Repeated screenshot/zoom/status calls were suppressed. "
                        "The latest visual evidence is still available. Choose a new cell or a concrete GUI "
                        "action instead of repeating the same observation."
                    ),
                }
            )
            print("[Observation-loop guard: suppressed repeated observation; continuing the turn]", flush=True)
            continue

        if rejected_this_round:
            if turn_state.goal_active:
                # Stay IN_PROGRESS and let the model adapt after a recoverable tool failure.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[Host tool-rejection note] One requested tool call was denied by a local safety/quota "
                            "rule. The persistent goal remains IN_PROGRESS. Adapt using the current framebuffer, "
                            "another GUI action, or existing evidence. BLOCKED is not authorized by this individual "
                            "rejection; continue unless a later host message explicitly authorizes BLOCKED."
                        ),
                    }
                )
                continue

            await force_final_answer(
                openai_tools,
                "A runaway or quota-limited tool pattern was detected and stopped by the host.",
                turn_state=turn_state,
            )
            return


# ============================================================================
# Main
# ============================================================================

async def main() -> None:
    global ACTIVE_DESKTOP_SESSION_ID, FRAME_CACHE, HOST_DESKTOP_CONTROL

    DEMO_UI.install_capture()
    DEMO_UI.start_working("Preparing secure workspace…")

    print("OpenAI-compatible + Solari live-RFB desktop agent toolkit agent-native-coordinate (enterprise)")
    print("====================================================")
    print(f"Provider: {AI_PROVIDER_LABEL}")
    print(f"Model:    {MODEL}")
    print(f"Endpoint: {BASE_URL}")
    print(f"MCP:      {MCP_SERVER}")
    print("Visuals:  persistent Solari streamUrl / RFB framebuffer")
    print()

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {SOLARI_API_KEY}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
    ) as mcp_http_client:
        async with streamable_http_client(
            MCP_SERVER,
            http_client=mcp_http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream, _get_session_id):
            async with DesktopClient(
                api_key=SOLARI_API_KEY,
                base_url=SOLARI_API_BASE,
            ) as desktop_sdk:
                # mcp>=1.29 exposes ClientSession rather than the old top-level
                # Client wrapper.  Initialize the Streamable HTTP session once
                # before using list_tools/call_tool.
                mcp = ClientSession(read_stream, write_stream)
                await mcp.__aenter__()
                await mcp.initialize()
                tool_result = await mcp.list_tools()
                openai_tools = convert_mcp_tools(tool_result.tools)

                ACTIVE_DESKTOP_SESSION_ID, FRAME_CACHE, HOST_DESKTOP_CONTROL = await resolve_existing_desktop(
                    mcp,
                    desktop_sdk,
                )
                await _vdi_preflight()

                print()
                print("Tools visible to model:")
                for tool in openai_tools:
                    print("  -", tool["function"]["name"])
                print()
                print("Host owns all Solari session lifecycle (list/connect/create/kill), screenshot/raw-click, session IDs, and the persistent RFB stream.")
                print("Lifecycle tools are hidden from the model and denied again at execution time.")
                print(
                    "Visual clicks: the chat model chooses native framebuffer pixels directly; "
                    "the host performs only frame/bounds/display/cursor validation and atomic input."
                )
                print("Navigation is performed by the model through the real desktop using desktop_hotkey + desktop_type_text; no hidden URL navigation shortcut is exposed.")
                print("Exact-value conclusions must be grounded in the current visible framebuffer evidence.")
                print("BLOCKED is host-authorized; desktop-grounded conclusions must use observed evidence only.")
                print("Turn modes: CHAT / ACTION / persistent GOAL with host-enforced completion gate.")
                print("Persistent goals use soft/hard budgets; individual tool rejections no longer terminalize the goal.")
                print("Persistent goals must resolve via finish_goal before a final answer is allowed.")
                print("Every human turn acquires a fresh signed RFB stream before attaching the current framebuffer.")
                print(
                    "VDI controls are host-owned: vdi_vpn_status/connect/disconnect and "
                    "vdi_desktop_state; credentials remain outside model context."
                )
                print(
                    f"Security vault: {'enabled' if VAULT_ENABLED else 'disabled'}; "
                    "password/TOTP values are host-typed and never returned to the model."
                )
                print(
                    f"Desktop readiness: health_timeout={DESKTOP_HEALTH_TIMEOUT:.0f}s, "
                    f"create_on_attach_failure={'enabled' if VDI_CREATE_ON_ATTACH_FAILURE else 'disabled'}."
                )
                print("AI requests use the standard OpenAI-compatible Chat Completions interface.")
                print(
                    f"{AI_PROVIDER_LABEL} watchdogs: "
                    f"first_output={MODEL_FIRST_OUTPUT_TIMEOUT:.0f}s, "
                    f"inter_output={MODEL_INTER_OUTPUT_TIMEOUT:.0f}s, "
                    f"total={MODEL_STREAM_TOTAL_TIMEOUT:.0f}s, "
                    f"stream_retries={MODEL_STREAM_RETRIES}; "
                    f"transient_retries={MODEL_TRANSIENT_RETRIES}; "
                    f"reasoning_effort={AI_REASONING_EFFORT or 'provider-default'}; "
                    f"max_images={MAX_IMAGES_PER_REQUEST if MAX_IMAGES_PER_REQUEST > 0 else 'unlimited'}; "
                    f"goal_progress={'shown' if SHOW_GOAL_PROGRESS else 'hidden'}; "
                    f"shell tools={'enabled' if ALLOW_SHELL_TOOLS else 'hidden'}"
                )
                print(
                    f"toolkit loop budgets: chat_rounds={MAX_TOOL_ROUNDS}, goal_rounds={MAX_GOAL_TOOL_ROUNDS}, "
                    f"repeat_observation_limit={MAX_REPEATED_OBSERVATION_CALLS}; "
                    "observation-only loops are guarded and continue with a host hint."
                )
                print(
                    "Double-click: agent-native point with host-validated atomic Solari double-click."
                )
                print("Type 'exit' or 'quit' to stop.\n")

                try:
                    DEMO_UI.stop_working()
                    if DEMO_UI.demo:
                        DEMO_UI.welcome()
                    else:
                        print("You: Hi")
                        # Preserve the historical debug-mode greeting.
                        await chat(mcp, openai_tools, "Hi", attach_desktop=False)

                    while True:
                        try:
                            user_input = DEMO_UI.prompt()
                            if user_input.lower() in {"exit", "quit"}:
                                if DEMO_UI.demo:
                                    DEMO_UI.goodbye()
                                else:
                                    print("Goodbye!")
                                break
                            if user_input:
                                await chat(mcp, openai_tools, user_input, attach_desktop=True)
                        except KeyboardInterrupt:
                            if DEMO_UI.demo:
                                DEMO_UI.goodbye()
                            else:
                                print("\nGoodbye!")
                            break
                        except Exception as exc:
                            print(f"\n[Chat error] {type(exc).__name__}: {exc}", flush=True)
                            if DEMO_UI.demo:
                                DEMO_UI.friendly_error("I couldn't complete that interaction. The technical details were saved to the session log.")
                finally:
                    DEMO_UI.stop_working()
                    if FRAME_CACHE is not None:
                        await FRAME_CACHE.stop()
                    try:
                        await asyncio.to_thread(VAULT.close)
                    except Exception:
                        pass
                await mcp.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
