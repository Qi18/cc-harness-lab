"""Permission decisions for tool requests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings
from .models import ToolRequest


DENY_LIST = ("sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/")


class PermissionPolicy:
    """Deny forbidden actions and ask before sensitive ones."""

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
        # 硬拒绝命令不会询问用户；边界不那么确定的敏感操作走交互确认，
        # 从而区分 deny 和 ask 两种权限语义。
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
