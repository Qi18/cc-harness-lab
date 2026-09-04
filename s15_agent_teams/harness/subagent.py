"""Synchronous SubAgent with isolated message history."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai import OpenAI

from .config import Settings
from .provider import assistant_payload, completion_request
from .tool_use import SUB_TOOLS, ToolExecutor


MAX_SUBAGENT_ROUNDS = 30


class SubagentRunner:
    def __init__(
        self,
        client: OpenAI,
        settings: Settings,
        executor: ToolExecutor,
        handlers: dict[str, Callable[..., str]],
        prompt_supplier: Callable[[], str],
    ):
        self.client = client
        self.settings = settings
        self.executor = executor
        self.handlers = handlers
        self.prompt_supplier = prompt_supplier

    # SubAgent 本质上就是一个工具：run 以 task 的 handler 身份注册进父 handler
    # 表（agent_loop.py L95），经 ToolExecutor 与其他工具共用同一条
    # Permission / Hook 管线。对父 Agent 而言它只是 description 进、观察字符串出：
    # 内部多轮循环与消息历史均不可见，因此参数错误和轮数超限也返回
    # Error 文本而不抛异常，保持与其他 handler 一致的同步调用契约。
    def run(self, description: str) -> str:
        if not isinstance(description, str) or not description.strip():
            return "Error: description must be a non-empty string"
        description = description.strip()
        print(f"\n\033[35m[Subagent spawned] {description}\033[0m")
        # 新消息列表就是隔离边界：父对话、Todo 和压缩状态
        # 都不会复制到委派任务中。
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt_supplier()},
            {"role": "user", "content": description},
        ]
        for _ in range(MAX_SUBAGENT_ROUNDS):
            response = self.client.chat.completions.create(
                **completion_request(self.settings, messages, SUB_TOOLS)
            )
            assistant = response.choices[0].message
            messages.append(assistant_payload(assistant))
            if not assistant.tool_calls:
                print("\033[35m[Subagent done]\033[0m")
                return assistant.content or ""
            for tool_call in assistant.tool_calls:
                result = self.executor.execute(
                    tool_call.function.name,
                    tool_call.function.arguments,
                    self.handlers,
                    display_prefix="[sub] ",
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
        return f"Error: subagent exceeded {MAX_SUBAGENT_ROUNDS} rounds"
