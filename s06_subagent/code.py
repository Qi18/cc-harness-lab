#!/usr/bin/env python3
"""s06 - synchronous subagents with isolated message histories."""

from __future__ import annotations

import glob as glob_module
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
MAX_OUTPUT_CHARS = 50_000
LARGE_OUTPUT_CHARS = 10_000
TODO_REMINDER_ROUNDS = 3
MAX_SUBAGENT_ROUNDS = 30

WORKDIR = Path(os.getenv("CC_WORKDIR", os.getcwd())).expanduser().resolve()
MODEL = os.getenv("MODEL_ID", DEFAULT_MODEL)
CURRENT_TODOS: list[TodoItem] = []
ACTIVE_CLIENT: OpenAI | None = None

DENY_LIST = (
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/",
)

HOOK_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)
HookCallback = Callable[..., str | None]
HOOKS: dict[str, list[HookCallback]] = {event: [] for event in HOOK_EVENTS}


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]


class TodoItem(TypedDict):
    content: str
    status: str


SYSTEM_PROMPT = (
    f"You are a coding agent working in {WORKDIR}. "
    "Use dedicated file tools for reading, writing, editing, and finding files. "
    "Use bash for shell operations that the dedicated tools cannot perform. "
    "Use task to delegate a complex, self-contained subtask when isolated context helps. "
    "Check the subagent's conclusion and verify any filesystem changes yourself. "
    "For multi-step tasks, use todo_write before acting. Before working on a step, "
    "mark exactly that step in_progress; after finishing it, mark it completed. "
    "Before your final answer, ensure every finished task is completed. "
    "Act on the task, verify your work, then answer concisely."
)
SUB_SYSTEM = (
    f"You are a focused coding subagent working in {WORKDIR}. "
    "Complete the assigned subtask directly using the available tools. "
    "Do not delegate the work. Verify important results, then return only a concise "
    "conclusion for the parent agent."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a Bash command in the agent working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Bash command to execute.",
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the working directory.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional maximum number of lines to return.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write complete UTF-8 content to a file in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the working directory.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete content to write.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace the first exact occurrence of text in a UTF-8 file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the working directory.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to find.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find paths in the working directory using a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Relative glob pattern, for example **/*.py.",
                    }
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Create or replace the current task list for a multi-step task. "
                "Before acting, mark exactly one pending task in_progress. "
                "Mark it completed when done and keep at most one task in_progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Complete replacement task list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Concrete task description.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                    ],
                                },
                            },
                            "required": ["content", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
        },
    },
]

TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Launch a synchronous subagent for a complex, self-contained subtask. "
            "The subagent has isolated context and returns only its final conclusion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Complete subtask and expected result.",
                }
            },
            "required": ["description"],
            "additionalProperties": False,
        },
    },
}
TOOLS.append(TASK_TOOL)
SUB_TOOLS = [
    tool
    for tool in TOOLS
    if tool["function"]["name"] not in {"todo_write", "task"}
]


def create_client() -> OpenAI:
    """Create a Bailian OpenAI-compatible client without exposing the API key."""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing DASHSCOPE_API_KEY. Export it or place it in a local .env file."
        )

    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
        timeout=150.0,
    )


def safe_path(path: str) -> Path:
    """Resolve a path and ensure its real location stays under WORKDIR."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")

    raw_path = Path(path).expanduser()
    candidate = raw_path.resolve() if raw_path.is_absolute() else (WORKDIR / raw_path).resolve()
    try:
        candidate.relative_to(WORKDIR)
    except ValueError as exc:
        raise ValueError(f"path escapes working directory: {path}") from exc
    return candidate


def clip_output(output: str) -> str:
    """Bound observations before adding them to model context."""
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    return output[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"


def run_bash(command: str) -> str:
    """Execute one Bash command and return an observation for the model."""
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except OSError as exc:
        return f"Error: {exc}"

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    parts = [f"exit_code={completed.returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if len(parts) == 1:
        parts.append("(no output)")
    return clip_output("\n".join(parts))


def run_read(path: str, limit: int | None = None) -> str:
    """Read a UTF-8 text file with an optional line limit."""
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        return "Error: limit must be a positive integer"

    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: {exc}"

    if limit is not None:
        lines = lines[:limit]
    return clip_output("\n".join(lines))


def run_write(path: str, content: str) -> str:
    """Write complete UTF-8 content to a file."""
    try:
        target = safe_path(path)
        target.write_text(content, encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: {exc}"
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """Replace exactly the first occurrence of old_text in a UTF-8 file."""
    if not old_text:
        return "Error: old_text must not be empty"

    try:
        target = safe_path(path)
        text = target.read_text(encoding="utf-8")
        if old_text not in text:
            return "Error: text not found"
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return f"Error: {exc}"
    return f"Edited {path}"


def run_glob(pattern: str) -> str:
    """Return sorted paths matching a relative glob pattern."""
    pattern_path = Path(pattern)
    if not pattern or pattern_path.is_absolute() or ".." in pattern_path.parts:
        return "Error: glob pattern must stay inside the working directory"

    matches = glob_module.glob(pattern, root_dir=WORKDIR, recursive=True)
    safe_matches: list[str] = []
    for match in sorted(matches):
        try:
            safe_path(match)
        except ValueError:
            continue
        safe_matches.append(match)
    return clip_output("\n".join(safe_matches))


def run_todo_write(todos: list[dict[str, Any]]) -> str:
    """Validate and replace the in-memory task list."""
    global CURRENT_TODOS

    if not isinstance(todos, list):
        return "Error: todos must be a list"

    normalized: list[TodoItem] = []
    in_progress_count = 0
    valid_statuses = {"pending", "in_progress", "completed"}
    for index, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return f"Error: todo {index} must be an object"
        if set(todo) != {"content", "status"}:
            return f"Error: todo {index} must contain only content and status"

        content = todo.get("content")
        status = todo.get("status")
        if not isinstance(content, str) or not content.strip():
            return f"Error: todo {index} content must be a non-empty string"
        if status not in valid_statuses:
            return f"Error: todo {index} has invalid status {status!r}"
        if status == "in_progress":
            in_progress_count += 1
        normalized.append({"content": content.strip(), "status": status})

    if in_progress_count > 1:
        return "Error: at most one todo may be in_progress"

    CURRENT_TODOS = normalized
    icons = {"pending": " ", "in_progress": "▸", "completed": "✓"}
    lines = ["\n## Current Tasks"]
    if not CURRENT_TODOS:
        lines.append("  (empty)")
    for todo in CURRENT_TODOS:
        lines.append(f"  [{icons[todo['status']]}] {todo['content']}")
    print("\n".join(lines))

    completed = sum(todo["status"] == "completed" for todo in CURRENT_TODOS)
    result = f"Updated {len(CURRENT_TODOS)} tasks ({completed} completed)"
    has_pending = any(todo["status"] == "pending" for todo in CURRENT_TODOS)
    if has_pending and in_progress_count == 0:
        result += "; mark one pending task in_progress before acting"
    return result


SUB_TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


def spawn_subagent(description: str) -> str:
    """Run one isolated child loop and return only its final conclusion."""
    if not isinstance(description, str) or not description.strip():
        return "Error: description must be a non-empty string"
    if ACTIVE_CLIENT is None:
        return "Error: no active model client for subagent"

    description = description.strip()
    print(f"\n\033[35m[Subagent spawned] {description}\033[0m")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUB_SYSTEM},
        {"role": "user", "content": description},
    ]

    for _ in range(MAX_SUBAGENT_ROUNDS):
        request: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "tools": SUB_TOOLS,
        }
        if MODEL == "kimi/kimi-k3":
            request["extra_body"] = {"reasoning_effort": "max"}

        response = ACTIVE_CLIENT.chat.completions.create(**request)
        assistant_message = response.choices[0].message
        assistant_payload = assistant_message.model_dump(exclude_none=True)
        reasoning_content = getattr(assistant_message, "reasoning_content", None)
        if reasoning_content:
            assistant_payload["reasoning_content"] = reasoning_content
        messages.append(assistant_payload)

        if not assistant_message.tool_calls:
            conclusion = assistant_message.content or ""
            print("\033[35m[Subagent done]\033[0m")
            return conclusion

        for tool_call in assistant_message.tool_calls:
            result = execute_sub_tool(
                tool_call.function.name,
                tool_call.function.arguments,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )

    print("\033[35m[Subagent done: round limit reached]\033[0m")
    return f"Error: subagent exceeded {MAX_SUBAGENT_ROUNDS} rounds"


TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    **SUB_TOOL_HANDLERS,
    "todo_write": run_todo_write,
    "task": spawn_subagent,
}


def check_deny_list(command: str) -> str | None:
    """Return a reason when a Bash command is forbidden unconditionally."""
    normalized = command.lower()
    root_delete = re.search(
        r"(?:^|[;&|]\s*)rm\s+-rf\s+/(?=$|\s|[;&|])",
        normalized,
    )
    if root_delete:
        return "Blocked: 'rm -rf /' is on the deny list"

    for pattern in DENY_LIST[1:]:
        if pattern in normalized:
            return f"Blocked: {pattern!r} is on the deny list"
    return None


def path_is_outside_workdir(path: object) -> bool:
    """Check a proposed path without granting access to it."""
    if not isinstance(path, str) or not path.strip():
        return False
    raw_path = Path(path).expanduser()
    candidate = (
        raw_path.resolve()
        if raw_path.is_absolute()
        else (WORKDIR / raw_path).resolve()
    )
    try:
        candidate.relative_to(WORKDIR)
    except ValueError:
        return True
    return False


def check_rules(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return why a tool call requires an explicit user decision."""
    if tool_name in {"write_file", "edit_file"}:
        if path_is_outside_workdir(args.get("path")):
            return "Writing outside the working directory"

    if tool_name == "bash":
        command = args.get("command", "")
        if isinstance(command, str) and any(
            marker in command.lower()
            for marker in ("rm ", "> /etc/", "chmod 777")
        ):
            return "Potentially destructive command"
    return None


def ask_user(tool_name: str, args: dict[str, Any], reason: str) -> str:
    """Ask for a one-time approval, defaulting safely to deny."""
    print(f"\n\033[33m⚠ {reason}\033[0m")
    print(f"  Tool: {tool_name}({json.dumps(args, ensure_ascii=False)})")
    try:
        choice = input("  Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "deny"
    return "allow" if choice in {"y", "yes"} else "deny"


def check_permission(
    tool_name: str,
    args: dict[str, Any],
) -> tuple[bool, str | None]:
    """Route one tool request through deny, ask, then allow."""
    if tool_name == "bash":
        command = args.get("command", "")
        reason = check_deny_list(command if isinstance(command, str) else "")
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False, reason

    reason = check_rules(tool_name, args)
    if reason and ask_user(tool_name, args, reason) == "deny":
        return False, reason
    return True, None


def register_hook(event: str, callback: HookCallback) -> None:
    """Register one callback for a known lifecycle event."""
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event}")
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args: Any) -> str | None:
    """Run callbacks in registration order and stop on a non-None result."""
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event}")
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def context_inject_hook(query: str) -> None:
    """Show where the submitted prompt will execute."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def permission_hook(request: ToolRequest) -> str | None:
    """Adapt the s03 permission pipeline to a PreToolUse hook."""
    allowed, reason = check_permission(request.name, request.arguments)
    if allowed:
        return None
    return f"Permission denied: {reason}"


def log_hook(request: ToolRequest) -> None:
    """Log tool requests that passed earlier PreToolUse hooks."""
    payload = json.dumps(request.arguments, ensure_ascii=False)
    print(f"\033[90m[HOOK] PreToolUse: {request.name}({payload})\033[0m")
    return None


def large_output_hook(request: ToolRequest, output: str) -> None:
    """Log observation size and warn when it will consume substantial context."""
    output_size = len(str(output))
    print(
        f"\033[90m[HOOK] PostToolUse: {request.name} returned {output_size} chars\033[0m"
    )
    if output_size > LARGE_OUTPUT_CHARS:
        print(f"\033[90m[HOOK] PostToolUse: large output from {request.name}\033[0m")
    return None


def summary_hook(messages: list[dict[str, Any]]) -> None:
    """Print tool usage before the agent loop returns its final answer."""
    tool_count = sum(message.get("role") == "tool" for message in messages)
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


def install_default_hooks() -> None:
    """Attach cross-cutting behavior without changing the core loop."""
    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)


install_default_hooks()


def execute_tool_with_handlers(
    name: str,
    arguments: str,
    handlers: dict[str, Callable[..., str]],
    display_prefix: str = "",
) -> str:
    """Parse a tool call, run hooks, then dispatch through selected handlers."""
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
    tool_request = ToolRequest(name=name, arguments=payload)
    blocked = trigger_hooks("PreToolUse", tool_request)
    if blocked is not None:
        result = str(blocked)
        print(result)
        return result

    handler = handlers.get(name)
    if handler is None:
        result = f"Error: unknown tool {name!r}"
        trigger_hooks("PostToolUse", tool_request, result)
        return result

    try:
        result = handler(**payload)
    except TypeError as exc:
        result = f"Error: invalid arguments for {name}: {exc}"
    trigger_hooks("PostToolUse", tool_request, result)
    preview = result if len(result) <= 500 else result[:500] + "\n..."
    print(preview)
    return result


def execute_tool(name: str, arguments: str) -> str:
    """Execute a parent-agent tool call."""
    return execute_tool_with_handlers(name, arguments, TOOL_HANDLERS)


def execute_sub_tool(name: str, arguments: str) -> str:
    """Execute a child tool call without exposing parent-only tools."""
    return execute_tool_with_handlers(
        name,
        arguments,
        SUB_TOOL_HANDLERS,
        display_prefix="[sub] ",
    )


def agent_loop(client: OpenAI, messages: list[dict[str, Any]]) -> str:
    """Call the model, nudging it when a plan has gone stale."""
    global ACTIVE_CLIENT
    ACTIVE_CLIENT = client
    rounds_since_todo = 0
    while True:
        if rounds_since_todo >= TODO_REMINDER_ROUNDS and messages:
            reminder = "<reminder>Update your todos before continuing.</reminder>"
            messages.append({"role": "user", "content": reminder})
            print(
                "\033[90m[REMINDER] Three rounds without todo_write; "
                "asking for an update.\033[0m"
            )
            rounds_since_todo = 0

        request: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
        }
        if MODEL == "kimi/kimi-k3":
            request["extra_body"] = {"reasoning_effort": "max"}

        response = client.chat.completions.create(**request)
        assistant_message = response.choices[0].message
        assistant_payload = assistant_message.model_dump(exclude_none=True)
        reasoning_content = getattr(assistant_message, "reasoning_content", None)
        if reasoning_content:
            assistant_payload["reasoning_content"] = reasoning_content
        messages.append(assistant_payload)

        if not assistant_message.tool_calls:
            answer = assistant_message.content or ""
            force_continuation = trigger_hooks("Stop", messages)
            if force_continuation is not None:
                messages.append({"role": "user", "content": force_continuation})
                continue
            return answer

        used_todo_write = any(
            tool_call.function.name == "todo_write"
            for tool_call in assistant_message.tool_calls
        )
        rounds_since_todo = 0 if used_todo_write else rounds_since_todo + 1

        for tool_call in assistant_message.tool_calls:
            result = execute_tool(
                tool_call.function.name,
                tool_call.function.arguments,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )


def main() -> None:
    client = create_client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]

    print("s06: Subagent (Alibaba Cloud Bailian)")
    print(f"model={MODEL} workdir={WORKDIR}")
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.strip().lower() in {"", "q", "exit"}:
            break

        trigger_hooks("UserPromptSubmit", query)
        messages.append({"role": "user", "content": query})
        answer = agent_loop(client, messages)
        if answer:
            print(answer)
        print()


if __name__ == "__main__":
    main()
