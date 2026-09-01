"""Tool schemas, handlers, and the shared execution pipeline."""

from __future__ import annotations

import glob as glob_module
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .hooks import HookManager
from .models import ToolRequest
from .skill_loading import SkillLoader


# s02 在一个文件中依次呈现模型契约、调用分发和 handler，便于连续阅读。
# 三部分仍保持对象边界：修改 schema 不会悄悄改变运行行为，
# 修改 handler 也不会自动向模型暴露新能力。
def function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


BASE_TOOLS = [
    function_tool(
        "bash",
        "Run a Bash command in the agent working directory.",
        {"command": {"type": "string"}},
        ["command"],
    ),
    function_tool(
        "read_file",
        "Read a UTF-8 text file inside the working directory.",
        {
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        ["path"],
    ),
    function_tool(
        "write_file",
        "Write complete UTF-8 content to a file in the working directory.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    function_tool(
        "edit_file",
        "Replace the first exact occurrence of text in a UTF-8 file.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        ["path", "old_text", "new_text"],
    ),
    function_tool(
        "glob",
        "Find paths in the working directory using a glob pattern.",
        {"pattern": {"type": "string"}},
        ["pattern"],
    ),
]

TODO_TOOL = function_tool(
    "todo_write",
    (
        "Create or replace the current execution checklist. Keep at most one "
        "item in_progress and mark completed work before the final answer."
    ),
    {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content", "status"],
                "additionalProperties": False,
            },
        }
    },
    ["todos"],
)
SUBAGENT_TOOL = function_tool(
    "task",
    "Launch a synchronous subagent with isolated messages for one subtask.",
    {"description": {"type": "string", "minLength": 1}},
    ["description"],
)
LOAD_SKILL_TOOL = function_tool(
    "load_skill",
    "Load complete SKILL.md instructions by registered name.",
    {"name": {"type": "string", "minLength": 1}},
    ["name"],
)
COMPACT_TOOL = function_tool(
    "compact",
    "Summarize and archive conversation history to free context space.",
    {},
)

# 注册表与 handler 表是两件事：注册表是随每次请求发给模型的 JSON Schema，
# 决定模型允许请求什么，也是 System Prompt 里 tools section 的数据源；
# handler 表（agent_loop.py L92 与下方 BuiltinTools.handlers）是本地真正
# 执行的 Python 可调用对象，决定请求最终跑什么代码。
# 两表刻意不完全对齐：compact 只有 schema 而没有 handler，因为它要替换
# 整个 messages，不满足 handler 返回字符串的契约，由父循环内联拦截；
# SUB_TOOLS 特意排除了 todo_write、task 和 compact，即使模型凭记忆硬造调用，
# handler 表也只会返回 unknown tool。
# 所以能力隔离由注册表和 handler 表共同保证，而不只依赖 SubAgent Prompt。
PARENT_TOOLS = [
    *BASE_TOOLS,
    TODO_TOOL,
    SUBAGENT_TOOL,
    LOAD_SKILL_TOOL,
    COMPACT_TOOL,
]
SUB_TOOLS = [*BASE_TOOLS, LOAD_SKILL_TOOL]


# 父 Agent 和 SubAgent 的所有普通工具调用都经过这条共享管线。
class ToolExecutor:
    """Parse arguments, run hooks, and dispatch one tool call."""

    def __init__(self, hooks: HookManager):
        self.hooks = hooks

    def execute(
        self,
        name: str,
        arguments: str,
        handlers: dict[str, Callable[..., str]],
        display_prefix: str = "",
    ) -> str:
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return f"Error: invalid tool arguments: {exc}"
        if not isinstance(payload, dict):
            return "Error: tool arguments must be a JSON object"
        print(
            f"\033[33m{display_prefix}{name} "
            f"{json.dumps(payload, ensure_ascii=False)}\033[0m"
        )
        request = ToolRequest(name=name, arguments=payload)
        blocked = self.hooks.trigger("PreToolUse", request)
        if blocked is not None:
            print(blocked)
            return str(blocked)
        handler = handlers.get(name)
        if handler is None:
            result = f"Error: unknown tool {name!r}"
        else:
            try:
                result = handler(**payload)
            except TypeError as exc:
                result = f"Error: invalid arguments for {name}: {exc}"
        result = str(result)
        self.hooks.trigger("PostToolUse", request, result)
        print(result if len(result) <= 500 else result[:500] + "\n...")
        return result


# handler 是运行时实现，上面的 schema 是模型能力契约。execute() 用
# handler(**payload) 直接展开 JSON 参数，因此 schema 的属性名必须与 handler
# 形参名逐字一致；只改一边不会有静态报错，只会在运行时变成
# invalid arguments 错误。
MAX_OUTPUT_CHARS = 50_000


# 这里的 builtin 指由 Harness 自己实现、父子 Agent 共用的基础工具，
# 对应 schema 侧的 BASE_TOOLS + LOAD_SKILL_TOOL（即 SUB_TOOLS 全集）。
# todo_write 与 task 由 TodoManager、SubagentRunner 提供，只注册给父 Agent。
class BuiltinTools:
    """Handlers that operate on the workspace or Skill registry."""

    def __init__(self, settings: Settings, skills: SkillLoader):
        self.settings = settings
        self.skills = skills

    def safe_path(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(path).expanduser()
        candidate = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.settings.workdir / candidate).resolve()
        )
        try:
            candidate.relative_to(self.settings.workdir)
        except ValueError as exc:
            raise ValueError(f"path escapes working directory: {path}") from exc
        return candidate

    @staticmethod
    def clip(output: str) -> str:
        if len(output) <= MAX_OUTPUT_CHARS:
            return output
        return output[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"

    def run_bash(self, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            return "Error: command must be a non-empty string"
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.settings.workdir,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 120 seconds"
        output = (completed.stdout + completed.stderr).strip() or "(no output)"
        return self.clip(output)

    def run_read(self, path: str, limit: int | None = None) -> str:
        try:
            lines = self.safe_path(path).read_text(encoding="utf-8").splitlines()
            if limit is not None:
                if not isinstance(limit, int) or limit < 1:
                    return "Error: limit must be a positive integer"
                if len(lines) > limit:
                    lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return self.clip("\n".join(lines))
        except (OSError, UnicodeError, ValueError) as exc:
            return f"Error: {exc}"

    def run_write(self, path: str, content: str) -> str:
        try:
            target = self.safe_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            return f"Error: {exc}"

    def run_edit(self, path: str, old_text: str, new_text: str) -> str:
        try:
            target = self.safe_path(path)
            text = target.read_text(encoding="utf-8")
            occurrences = text.count(old_text)
            if occurrences == 0:
                return f"Error: old_text not found in {path}"
            if occurrences > 1:
                return f"Error: old_text is not unique in {path} ({occurrences} matches)"
            target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except (OSError, TypeError, UnicodeError, ValueError) as exc:
            return f"Error: {exc}"

    def run_glob(self, pattern: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip():
            return "Error: pattern must be a non-empty string"
        matches = []
        for match in glob_module.glob(
            pattern, root_dir=self.settings.workdir, recursive=True
        ):
            try:
                (self.settings.workdir / match).resolve().relative_to(
                    self.settings.workdir
                )
            except ValueError:
                continue
            matches.append(match)
        return self.clip("\n".join(sorted(matches)) if matches else "(no matches)")

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "bash": self.run_bash,
            "read_file": self.run_read,
            "write_file": self.run_write,
            "edit_file": self.run_edit,
            "glob": self.run_glob,
            "load_skill": self.skills.load,
        }
