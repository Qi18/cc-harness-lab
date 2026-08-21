"""Small shared data contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]


class TodoItem(TypedDict):
    content: str
    status: str


class SkillRecord(TypedDict):
    name: str
    description: str
    content: str
    path: str
