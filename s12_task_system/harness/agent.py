"""Cumulative Agent Loop with prompt assembly, recovery, and durable tasks."""

from __future__ import annotations

import copy
import json
import random
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from . import memory
from .compaction import ContextCompactor, latest_user_request
from .config import Settings
from .hooks import HookManager, install_default_hooks
from .models import ToolRequest
from .prompt import (
    PARENT_GUIDANCE,
    PARENT_IDENTITY,
    SUBAGENT_GUIDANCE,
    SUBAGENT_IDENTITY,
    SystemPromptAssembler,
    registered_tool_names,
)
from .recovery import (
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_CONTINUATIONS,
    RecoveryState,
    is_output_truncated,
    is_prompt_too_long_error,
    with_retry,
)
from .skills import SkillLoader
from .tasks import TaskManager, TaskStore
from .tools import PARENT_TOOLS, SUB_TOOLS, BuiltinTools


TODO_REMINDER_ROUNDS = 3
MAX_SUBAGENT_ROUNDS = 30


class AgentHarness:
    """One cumulative harness with prompt assembly and error recovery."""

    def __init__(
        self,
        client: OpenAI,
        settings: Settings,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
    ):
        self.client = client
        self.settings = settings
        self.sleep_fn = sleep_fn
        self.random_fn = random_fn
        self.skills = SkillLoader(settings.skills_dir)
        self.builtins = BuiltinTools(settings, self.skills)
        self.task_store = TaskStore(settings.tasks_dir, settings.workdir)
        self.tasks = TaskManager(self.task_store)
        self.hooks = HookManager()
        install_default_hooks(self.hooks, settings)
        self.compactor = ContextCompactor(
            client,
            settings.model,
            settings.transcript_dir,
            settings.tool_results_dir,
        )
        memory.configure(settings)

        base_handlers = self.builtins.base_handlers()
        self.sub_handlers: dict[str, Callable[..., str]] = dict(base_handlers)
        self.parent_handlers: dict[str, Callable[..., str]] = {
            **base_handlers,
            "todo_write": self.builtins.run_todo_write,
            "task": self.spawn_subagent,
            "create_task": self.tasks.run_create,
            "update_task": self.tasks.run_update,
            "list_tasks": self.tasks.run_list,
            "get_task": self.tasks.run_get,
            "claim_task": self.tasks.run_claim,
            "complete_task": self.tasks.run_complete,
        }
        self.parent_prompt = SystemPromptAssembler(
            PARENT_IDENTITY, PARENT_GUIDANCE
        )
        self.sub_prompt = SystemPromptAssembler(
            SUBAGENT_IDENTITY, SUBAGENT_GUIDANCE
        )
        self.system_prompt = ""
        self.sub_system_prompt = ""
        self.refresh_system_prompts()

    def _prompt_context(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "enabled_tools": registered_tool_names(tools),
            "workspace": str(self.settings.workdir),
            "skill_catalog": (
                self.skills.catalog() if self.skills.registry else ""
            ),
            "memory_catalog": memory.read_memory_index(),
        }

    def refresh_system_prompts(
        self, messages: list[dict[str, Any]] | None = None
    ) -> None:
        self.skills.registry = self.skills.scan()
        self.system_prompt = self.parent_prompt.get(
            self._prompt_context(PARENT_TOOLS)
        )
        self.sub_system_prompt = self.sub_prompt.get(
            self._prompt_context(SUB_TOOLS)
        )
        if messages is None:
            return
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self.system_prompt
        else:
            messages.insert(0, {"role": "system", "content": self.system_prompt})

    def execute_with_handlers(
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

    def execute_tool(self, name: str, arguments: str) -> str:
        return self.execute_with_handlers(name, arguments, self.parent_handlers)

    def execute_sub_tool(self, name: str, arguments: str) -> str:
        return self.execute_with_handlers(
            name, arguments, self.sub_handlers, display_prefix="[sub] "
        )

    def spawn_subagent(self, description: str) -> str:
        if not isinstance(description, str) or not description.strip():
            return "Error: description must be a non-empty string"
        description = description.strip()
        self.refresh_system_prompts()
        print(f"\n\033[35m[Subagent spawned] {description}\033[0m")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.sub_system_prompt},
            {"role": "user", "content": description},
        ]
        state = RecoveryState(self.settings.model)
        for _ in range(MAX_SUBAGENT_ROUNDS):
            request: dict[str, Any] = {
                "messages": messages,
                "tools": SUB_TOOLS,
                "max_tokens": DEFAULT_MAX_TOKENS,
            }
            if self.settings.model == "kimi/kimi-k3":
                request["extra_body"] = {"reasoning_effort": "max"}
            response = with_retry(
                lambda model: self.client.chat.completions.create(
                    model=model, **request
                ),
                state,
                self.settings.fallback_model,
                sleep_fn=self.sleep_fn,
                random_fn=self.random_fn,
            )
            assistant = response.choices[0].message
            payload = assistant.model_dump(exclude_none=True)
            reasoning = getattr(assistant, "reasoning_content", None)
            if reasoning:
                payload["reasoning_content"] = reasoning
            messages.append(payload)
            if not assistant.tool_calls:
                print("\033[35m[Subagent done]\033[0m")
                return assistant.content or ""
            for tool_call in assistant.tool_calls:
                result = self.execute_sub_tool(
                    tool_call.function.name, tool_call.function.arguments
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
        return f"Error: subagent exceeded {MAX_SUBAGENT_ROUNDS} rounds"

    def request_manual_compact(
        self, arguments: str, already_compacted: bool
    ) -> tuple[str, bool]:
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return f"Error: invalid tool arguments: {exc}", False
        if payload != {}:
            return "Error: compact does not accept arguments", False
        request = ToolRequest(name="compact", arguments={})
        blocked = self.hooks.trigger("PreToolUse", request)
        if blocked is not None:
            return str(blocked), False
        if already_compacted:
            result = (
                "Compaction already completed for this user turn. "
                "Continue without requesting compact again."
            )
            self.hooks.trigger("PostToolUse", request, result)
            return result, False
        result = "Compaction requested after this tool batch."
        self.hooks.trigger("PostToolUse", request, result)
        return result, True

    @staticmethod
    def _record_error(messages: list[dict[str, Any]], error: Exception | str) -> str:
        text = str(error)
        answer = f"Error: {text[:300]}"
        messages.append({"role": "assistant", "content": answer})
        return answer

    def agent_loop(
        self,
        messages: list[dict[str, Any]],
        active_request: str | None = None,
        compactor: ContextCompactor | None = None,
    ) -> str:
        active_request = active_request or latest_user_request(messages)
        extraction_messages = copy.deepcopy(messages[-12:])
        self.refresh_system_prompts(messages)
        recalled = memory.load_memories(self.client, messages)
        memory.inject_recalled_memories(messages, recalled)
        if recalled:
            print("\033[90m[Memory: recalled relevant records]\033[0m")
        compactor = compactor or self.compactor
        rounds_since_todo = 0
        manual_compactions = 0
        state = RecoveryState(self.settings.model)
        max_tokens = DEFAULT_MAX_TOKENS

        while True:
            if rounds_since_todo >= TODO_REMINDER_ROUNDS and messages:
                reminder = "<reminder>Update your todos before continuing.</reminder>"
                messages.append({"role": "user", "content": reminder})
                rounds_since_todo = 0
            self.refresh_system_prompts(messages)
            messages[:] = compactor.prepare(messages, active_request)
            request: dict[str, Any] = {
                "messages": messages,
                "tools": [
                    tool
                    for tool in PARENT_TOOLS
                    if not (
                        manual_compactions
                        and tool["function"]["name"] == "compact"
                    )
                ],
                "max_tokens": max_tokens,
            }
            if state.current_model == "kimi/kimi-k3":
                request["extra_body"] = {"reasoning_effort": "max"}
            try:
                response = with_retry(
                    lambda model: self.client.chat.completions.create(
                        model=model, **request
                    ),
                    state,
                    self.settings.fallback_model,
                    sleep_fn=self.sleep_fn,
                    random_fn=self.random_fn,
                )
            except Exception as error:
                if is_prompt_too_long_error(error):
                    if not state.has_attempted_reactive_compact:
                        messages[:] = compactor.reactive_compact(
                            messages, active_request
                        )
                        state.has_attempted_reactive_compact = True
                        continue
                    return self._record_error(
                        messages, "context still too large after reactive compact"
                    )
                return self._record_error(
                    messages, f"{type(error).__name__}: {error}"
                )

            choice = response.choices[0]
            assistant = choice.message
            payload = assistant.model_dump(exclude_none=True)
            reasoning = getattr(assistant, "reasoning_content", None)
            if reasoning:
                payload["reasoning_content"] = reasoning

            if is_output_truncated(getattr(choice, "finish_reason", None)):
                if not state.has_escalated:
                    max_tokens = ESCALATED_MAX_TOKENS
                    state.has_escalated = True
                    continue
                messages.append(payload)
                extraction_messages.append(copy.deepcopy(payload))
                if state.continuation_count < MAX_CONTINUATIONS:
                    continuation = {
                        "role": "user",
                        "content": CONTINUATION_PROMPT,
                    }
                    messages.append(continuation)
                    extraction_messages.append(copy.deepcopy(continuation))
                    state.continuation_count += 1
                    continue
                return assistant.content or "Error: output recovery limit reached"

            messages.append(payload)
            extraction_messages.append(copy.deepcopy(payload))

            if not assistant.tool_calls:
                answer = assistant.content or ""
                continuation = self.hooks.trigger("Stop", messages)
                if continuation is not None:
                    item = {"role": "user", "content": continuation}
                    messages.append(item)
                    extraction_messages.append(copy.deepcopy(item))
                    continue
                memory.extract_memories(self.client, extraction_messages)
                memory.consolidate_memories(self.client)
                return answer

            used_todo = any(
                call.function.name == "todo_write" for call in assistant.tool_calls
            )
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
            compact_requested = False
            for tool_call in assistant.tool_calls:
                if tool_call.function.name == "compact":
                    result, allowed = self.request_manual_compact(
                        tool_call.function.arguments,
                        already_compacted=manual_compactions > 0,
                    )
                    compact_requested = compact_requested or allowed
                    print(result)
                else:
                    result = self.execute_tool(
                        tool_call.function.name, tool_call.function.arguments
                    )
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
                messages.append(tool_message)
                extraction_messages.append(copy.deepcopy(tool_message))
            if compact_requested:
                messages[:] = compactor.compact_history(messages, active_request)
                manual_compactions += 1
