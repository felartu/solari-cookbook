#!/usr/bin/env python3
"""Customer-facing console UI for Solari Enterprise VDI Toolkit.

All legacy ``print()`` diagnostics can be redirected to a session log while a
small Rich console surface shows only user prompts, concise model progress, a
spinner, final answers, and friendly errors.
"""
from __future__ import annotations

import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text


class _DebugStream(io.TextIOBase):
    def __init__(self, log, original, *, mirror: bool):
        self.log = log
        self.original = original
        self.mirror = mirror

    @property
    def encoding(self):  # pragma: no cover - compatibility property
        return getattr(self.original, "encoding", "utf-8")

    def isatty(self) -> bool:
        return bool(self.mirror and getattr(self.original, "isatty", lambda: False)())

    def writable(self) -> bool:
        return True

    def write(self, data: str) -> int:
        if not data:
            return 0
        self.log.write(data)
        self.log.flush()
        if self.mirror:
            self.original.write(data)
            self.original.flush()
        return len(data)

    def flush(self) -> None:
        self.log.flush()
        if self.mirror:
            self.original.flush()


class DemoUI:
    def __init__(self, *, root: str | Path, model: str = ""):
        self.root = Path(root)
        self.mode = os.getenv("TOOLKIT_UI_MODE", "demo").strip().lower() or "demo"
        if self.mode not in {"demo", "debug"}:
            self.mode = "demo"
        self.demo = self.mode == "demo"
        self.show_progress = os.getenv("TOOLKIT_UI_SHOW_PROGRESS", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.animation = os.getenv("TOOLKIT_UI_ANIMATION", "spinner").strip().lower() or "spinner"
        self.max_progress = max(50, int(os.getenv("TOOLKIT_UI_PROGRESS_MAX_CHARS", "150")))
        self.model = model

        log_value = os.getenv("TOOLKIT_UI_LOG_FILE", "").strip()
        if log_value:
            log_path = Path(log_value).expanduser()
            if not log_path.is_absolute():
                log_path = self.root / log_path
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = self.root / "logs" / f"toolkit-session-{stamp}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self._log = log_path.open("a", encoding="utf-8", buffering=1)

        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self.console = Console(
            file=self._stdout,
            force_terminal=None,
            highlight=False,
            soft_wrap=False,
        )
        self._captured = False
        self._status: Live | None = None
        self._turn_progress_seen: set[str] = set()

    def set_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in {"demo", "debug"}:
            raise ValueError("Toolkit UI mode must be 'demo' or 'debug'")
        if self._captured:
            raise RuntimeError("UI mode must be selected before output capture is installed")
        self.mode = mode
        self.demo = mode == "demo"

    def install_capture(self) -> None:
        if self._captured:
            return
        # Existing current/toolkit diagnostic prints become file-only in demo mode. In
        # debug mode they are mirrored exactly as before.
        sys.stdout = _DebugStream(self._log, self._stdout, mirror=not self.demo)
        sys.stderr = _DebugStream(self._log, self._stderr, mirror=not self.demo)
        self._captured = True

    def close(self) -> None:
        self.stop_working()
        if self._captured:
            sys.stdout = self._stdout
            sys.stderr = self._stderr
            self._captured = False
        try:
            self._log.close()
        except Exception:
            pass

    def debug(self, text: str) -> None:
        self._log.write(text.rstrip() + "\n")
        self._log.flush()

    def welcome(self) -> None:
        if not self.demo:
            return
        title = Text("Solari VDI Desktop Agent", style="bold cyan")
        subtitle = "Secure workspace ready"
        if self.model:
            subtitle += f"  •  {self.model}"
        self.console.print()
        self.console.rule(title, style="cyan")
        self.console.print(subtitle, style="dim")
        self.console.print("Tell me what you'd like me to do on the desktop.")
        self.console.print()

    def begin_turn(self) -> None:
        self._turn_progress_seen.clear()
        self.start_working("AI is working…")

    def start_working(self, message: str = "AI is working…") -> None:
        if not self.demo or self.animation == "none":
            return
        spinner_name = "dots" if self.animation in {"spinner", "dots"} else "dots"
        renderable = Spinner(spinner_name, text=message, style="cyan")
        if self._status is not None:
            self._status.update(renderable, refresh=True)
            return
        # Rich Status redirects sys.stdout/sys.stderr by default, which would
        # re-surface every hidden diagnostic while the spinner is active. Use
        # Live directly with redirection disabled so legacy prints remain in
        # the debug log only.
        self._status = Live(
            renderable,
            console=self.console,
            refresh_per_second=12.5,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._status.start()

    def stop_working(self) -> None:
        if self._status is not None:
            try:
                self._status.stop()
            finally:
                self._status = None

    @staticmethod
    def _strip_private_reasoning(text: str) -> str:
        text = text or ""
        # Some OpenAI-compatible endpoints leak their separator into content.
        # Only display text after the last closing marker; never expose the
        # preceding reasoning-like payload.
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1]
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S)
        text = re.sub(r"</?think>", "", text, flags=re.I)
        text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.I | re.S)
        text = re.sub(r"<function=.*?>|</function>|<parameter=.*?>|</parameter>", "", text, flags=re.I)
        return text.strip()

    def _clean_progress(self, text: str) -> str:
        text = self._strip_private_reasoning(text)
        text = re.sub(r"\s+", " ", text).strip()
        # Coordinates and frame/version details are implementation diagnostics,
        # not useful customer-facing progress.
        text = re.sub(r"\s*(?:at\s*)?\(\s*\d{1,4}\s*,\s*\d{1,4}\s*\)", "", text)
        text = re.sub(r"\bframe\s*v?\d+\b", "", text, flags=re.I)
        text = re.sub(r"\bv\d+\s*->\s*v\d+\b", "", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip(" -")
        if len(text) > self.max_progress:
            cut = text[: self.max_progress]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            text = cut.rstrip(".,;:") + "…"
        return text

    def progress(self, text: str) -> None:
        if not self.demo or not self.show_progress:
            return
        text = self._clean_progress(text)
        if not text:
            return
        key = re.sub(r"\W+", " ", text.casefold()).strip()
        if key in self._turn_progress_seen:
            return
        self._turn_progress_seen.add(key)
        self.debug(f"[UI progress] {text}")
        self.stop_working()
        self.console.print(Text("  • " + text, style="white"))
        self.start_working("AI is working…")

    def action_from_tool(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name == "desktop_click_native":
            target = str(arguments.get("target") or "the selected control").strip()
            return f"Selecting {target}…"
        if name == "desktop_double_click_native":
            target = str(arguments.get("target") or "the selected item").strip()
            return f"Opening {target}…"
        if name in {"desktop_hotkey", "desktop_key_native"}:
            purpose = str(arguments.get("purpose") or "Using the keyboard").strip()
            return purpose.rstrip(".") + "…"
        if name in {"desktop_type_text", "vdi_type_text", "desktop_type"}:
            purpose = str(arguments.get("purpose") or "Entering information").strip()
            return purpose.rstrip(".") + "…"
        if name == "desktop_refresh_view":
            return "Checking the current screen…"
        if name == "goal_remember":
            return "Keeping the verified task details in memory…"
        if name == "vdi_vpn_connect":
            return "Preparing the secure connection…"
        if name == "vdi_vpn_disconnect":
            return "Closing the secure connection…"
        if name == "vdi_desktop_state":
            return "Checking the desktop connection…"
        if name == "security_vault_status":
            return "Checking the secure credential vault…"
        if name == "security_vault_search":
            return "Finding the requested credential securely…"
        if name == "security_vault_type_username":
            return "Entering the username securely…"
        if name == "security_vault_type_password":
            return "Entering the password securely…"
        if name == "security_vault_type_totp":
            return "Generating and submitting the verification code securely…"
        return None

    def progress_for_tool(self, name: str, arguments: dict[str, Any]) -> None:
        text = self.action_from_tool(name, arguments)
        if text:
            self.progress(text)

    def final_answer(self, text: str) -> None:
        text = self._strip_private_reasoning(text).strip()
        self.debug(f"[Assistant final] {text}")
        self.stop_working()
        if self.demo:
            self.console.print()
            self.console.print(Text("Assistant", style="bold cyan"))
            self.console.print(Markdown(text or "Done."))
            self.console.print()

    def friendly_error(self, message: str = "I couldn't complete that step. Please try again.") -> None:
        self.stop_working()
        if self.demo:
            self.console.print(Text("Something went wrong", style="bold red"))
            self.console.print(message)

    def prompt(self) -> str:
        self.stop_working()
        if self.demo:
            self.console.print(Text("You › ", style="bold green"), end="")
            value = input().strip()
        else:
            value = input("\nYou: ").strip()
        if value:
            self.debug(f"You: {value}")
        return value

    def goodbye(self) -> None:
        self.stop_working()
        if self.demo:
            self.console.print("Goodbye!", style="dim")


__all__ = ["DemoUI"]
