"""Lifecycle hook registration and dispatch."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .config import Settings
from .models import ToolRequest
from .permission import PermissionPolicy


HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
HookCallback = Callable[..., str | None]
LARGE_OUTPUT_CHARS = 10_000


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

    # 注册顺序会影响行为：权限检查可在记录日志和执行前短路，
    # PostToolUse 与 Stop 则继续承担观察和续写职责。
    hooks.register("UserPromptSubmit", context_hook)
    hooks.register("PreToolUse", policy.check)
    hooks.register("PreToolUse", log_hook)
    hooks.register("PostToolUse", output_hook)
    hooks.register("Stop", stop_hook)
    return policy
