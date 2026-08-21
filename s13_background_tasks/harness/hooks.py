"""Lifecycle hooks and the permission pipeline."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ToolRequest


HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
HookCallback = Callable[..., str | None]
LARGE_OUTPUT_CHARS = 10_000
DENY_LIST = ("sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/")


class HookManager:
    def __init__(self):
        self.callbacks: dict[str, list[HookCallback]] = {
            event: [] for event in HOOK_EVENTS
        }

    def register(self, event: str, callback: HookCallback) -> None:
        if event not in self.callbacks:
            raise ValueError(f"Unknown hook event: {event}")
        self.callbacks[event].append(callback)

    def trigger(self, event: str, *args: Any) -> str | None:
        if event not in self.callbacks:
            raise ValueError(f"Unknown hook event: {event}")
        for callback in self.callbacks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None


class PermissionPolicy:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def deny_reason(command: str) -> str | None:
        normalized = command.lower()
        if re.search(r"(?:^|[;&|]\s*)rm\s+-rf\s+/(?=$|\s|[;&|])", normalized):
            return "Blocked: 'rm -rf /' is on the deny list"
        for pattern in DENY_LIST:
            if pattern in normalized:
                return f"Blocked: {pattern!r} is on the deny list"
        return None

    def path_outside_workdir(self, value: object) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        raw = Path(value).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (
            self.settings.workdir / raw
        ).resolve()
        try:
            candidate.relative_to(self.settings.workdir)
        except ValueError:
            return True
        return False

    def ask_reason(self, request: ToolRequest) -> str | None:
        if request.name in {"write_file", "edit_file"} and self.path_outside_workdir(
            request.arguments.get("path")
        ):
            return "Writing outside the working directory"
        if request.name == "bash":
            command = request.arguments.get("command", "")
            if isinstance(command, str) and any(
                marker in command.lower()
                for marker in ("rm ", "> /etc/", "chmod 777")
            ):
                return "Potentially destructive command"
        return None

    @staticmethod
    def ask_user(request: ToolRequest, reason: str) -> bool:
        print(f"\n\033[33m⚠ {reason}\033[0m")
        print(
            f"  Tool: {request.name}"
            f"({json.dumps(request.arguments, ensure_ascii=False)})"
        )
        try:
            choice = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return choice in {"y", "yes"}

    def check(self, request: ToolRequest) -> str | None:
        if request.name == "bash":
            command = request.arguments.get("command", "")
            reason = self.deny_reason(command if isinstance(command, str) else "")
            if reason:
                print(f"\n\033[31m⛔ {reason}\033[0m")
                return f"Permission denied: {reason}"
        reason = self.ask_reason(request)
        if reason and not self.ask_user(request, reason):
            return f"Permission denied: {reason}"
        return None


def install_default_hooks(
    hooks: HookManager,
    settings: Settings,
) -> PermissionPolicy:
    policy = PermissionPolicy(settings)

    def context_hook(query: str) -> None:
        print(f"\033[90m[HOOK] UserPromptSubmit: working in {settings.workdir}\033[0m")

    def log_hook(request: ToolRequest) -> None:
        payload = json.dumps(request.arguments, ensure_ascii=False)
        print(f"\033[90m[HOOK] PreToolUse: {request.name}({payload})\033[0m")

    def output_hook(request: ToolRequest, output: str) -> None:
        size = len(str(output))
        print(
            f"\033[90m[HOOK] PostToolUse: {request.name} returned {size} chars\033[0m"
        )
        if size > LARGE_OUTPUT_CHARS:
            print(
                f"\033[90m[HOOK] PostToolUse: large output from {request.name}\033[0m"
            )

    def stop_hook(messages: list[dict[str, Any]]) -> None:
        count = sum(message.get("role") == "tool" for message in messages)
        print(f"\033[90m[HOOK] Stop: session used {count} tool calls\033[0m")

    hooks.register("UserPromptSubmit", context_hook)
    hooks.register("PreToolUse", policy.check)
    hooks.register("PreToolUse", log_hook)
    hooks.register("PostToolUse", output_hook)
    hooks.register("Stop", stop_hook)
    return policy
