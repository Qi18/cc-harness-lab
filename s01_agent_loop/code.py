#!/usr/bin/env python3
"""s01 - one agent loop and one Bash tool, powered by Alibaba Cloud Bailian."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
MAX_OUTPUT_CHARS = 50_000

WORKDIR = Path(os.getenv("CC_WORKDIR", os.getcwd())).expanduser().resolve()
MODEL = os.getenv("MODEL_ID", DEFAULT_MODEL)

SYSTEM_PROMPT = (
    f"You are a coding agent working in {WORKDIR}. "
    "Use the bash tool to inspect and modify the project when needed. "
    "Act on the task, verify your work, then answer concisely."
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
    }
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


def run_bash(command: str) -> str:
    """Execute one Bash command and return an observation for the model."""
    blocked_fragments = (
        "rm -rf /",
        "rm -rf ~",
        "sudo ",
        "shutdown",
        "reboot",
        "> /dev/",
    )
    if any(fragment in command for fragment in blocked_fragments):
        return "Error: dangerous command blocked"

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

    return "\n".join(parts)[:MAX_OUTPUT_CHARS]


def execute_tool(name: str, arguments: str) -> str:
    """Dispatch a model tool call."""
    if name != "bash":
        return f"Error: unknown tool {name!r}"

    try:
        payload: dict[str, Any] = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return f"Error: invalid tool arguments: {exc}"

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return "Error: bash.command must be a non-empty string"

    print(f"\033[33m$ {command}\033[0m")
    result = run_bash(command)
    preview = result if len(result) <= 500 else result[:500] + "\n..."
    print(preview)
    return result


def agent_loop(
    client: OpenAI,
    messages: list[dict[str, Any]],
) -> str:
    """Call the model until it stops requesting tools."""
    while True:
        request: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
        }
        if MODEL == "kimi/kimi-k3":
            request["extra_body"] = {"reasoning_effort": "max"}

        response = client.chat.completions.create(
            **request,
        )
        assistant_message = response.choices[0].message
        assistant_payload = assistant_message.model_dump(exclude_none=True)
        reasoning_content = getattr(
            assistant_message,
            "reasoning_content",
            None,
        )
        if reasoning_content:
            assistant_payload["reasoning_content"] = reasoning_content
        messages.append(assistant_payload)

        if not assistant_message.tool_calls:
            return assistant_message.content or ""

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

    print("s01: Agent Loop (Alibaba Cloud Bailian)")
    print(f"model={MODEL} workdir={WORKDIR}")
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.strip().lower() in {"", "q", "exit"}:
            break

        messages.append({"role": "user", "content": query})
        answer = agent_loop(client, messages)
        if answer:
            print(answer)
        print()


if __name__ == "__main__":
    main()
