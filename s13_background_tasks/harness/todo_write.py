"""TodoWrite state and validation."""

from __future__ import annotations

from typing import Any

from .models import TodoItem


class TodoManager:
    def __init__(self) -> None:
        self.todos: list[TodoItem] = []

    # TodoWrite 本质上也是一个工具：update 以 todo_write 的 handler 身份注册进
    # 父 handler 表（agent_loop.py L94），同样经 ToolExecutor 的
    # Permission / Hook 管线调用，校验失败也只返回 Error 文本而不抛异常。
    # 它与文件类工具的差别在于副作用落在 Harness 自身状态：父循环据此
    # 清零 rounds_since_todo（agent_loop.py L260）；SUB_TOOLS 不含该工具，
    # 所以 SubAgent 没有自己的计划状态。
    def update(self, todos: list[dict[str, Any]]) -> str:
        if not isinstance(todos, list):
            return "Error: todos must be a list"
        # 先在临时列表中完成全部校验，任何一项失败都不污染现有 Todo。
        validated: list[TodoItem] = []
        in_progress = 0
        for index, item in enumerate(todos):
            if not isinstance(item, dict) or set(item) != {"content", "status"}:
                return f"Error: todo {index} must contain only content and status"
            content = item.get("content")
            status = item.get("status")
            if not isinstance(content, str) or not content.strip():
                return f"Error: todo {index} content must be non-empty"
            if status not in {"pending", "in_progress", "completed"}:
                return f"Error: invalid todo status: {status!r}"
            in_progress += status == "in_progress"
            validated.append({"content": content.strip(), "status": status})
        if in_progress > 1:
            return "Error: at most one todo may be in_progress"
        # 全部不变量通过后再一次性替换状态。
        self.todos[:] = validated
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        rendered = "\n".join(
            f"{markers[item['status']]} {item['content']}" for item in self.todos
        )
        print(f"\n{rendered}")
        return f"Updated {len(self.todos)} todos.\n{rendered}"
