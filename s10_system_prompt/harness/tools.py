"""Built-in file, shell, todo, skill, and control tools."""

from __future__ import annotations

import glob as glob_module
import subprocess
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .models import TodoItem
from .skills import SkillLoader

MAX_OUTPUT_CHARS = 50_000


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

PARENT_TOOLS = [
    *BASE_TOOLS,
    TODO_TOOL,
    SUBAGENT_TOOL,
    LOAD_SKILL_TOOL,
    COMPACT_TOOL,
]
SUB_TOOLS = [*BASE_TOOLS, LOAD_SKILL_TOOL]


class BuiltinTools:
    """Stateful handlers for the non-task built-ins."""

    def __init__(self, settings: Settings, skills: SkillLoader):
        self.settings = settings
        self.skills = skills
        self.todos: list[TodoItem] = []

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

    def run_todo_write(self, todos: list[dict[str, Any]]) -> str:
        if not isinstance(todos, list):
            return "Error: todos must be a list"
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
        self.todos[:] = validated
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        rendered = "\n".join(
            f"{markers[item['status']]} {item['content']}" for item in self.todos
        )
        print(f"\n{rendered}")
        return f"Updated {len(self.todos)} todos.\n{rendered}"

    def base_handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "bash": self.run_bash,
            "read_file": self.run_read,
            "write_file": self.run_write,
            "edit_file": self.run_edit,
            "glob": self.run_glob,
            "load_skill": self.skills.load,
        }
