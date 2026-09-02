"""s12 累计 Harness 的装配入口、错误恢复与持久任务编排。"""

from __future__ import annotations

import copy
import random
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from . import memory
from .context_compact import (
    CompactToolController,
    ContextCompactor,
    latest_user_request,
)
from .config import Settings
from .error_recovery import (
    CONTINUATION_PROMPT,
    ESCALATED_MAX_TOKENS,
    MAX_CONTINUATIONS,
    RecoveryState,
    format_unrecoverable_error,
    is_output_truncated,
    is_prompt_too_long_error,
    with_retry,
)
from .hooks import HookManager, install_default_hooks
from .system_prompt import (
    PARENT_GUIDANCE,
    PARENT_IDENTITY,
    SUBAGENT_GUIDANCE,
    SUBAGENT_IDENTITY,
    SystemPromptAssembler,
    registered_tool_names,
)
from .provider import assistant_payload, completion_request
from .skill_loading import SkillLoader
from .subagent import SubagentRunner
from .task_system import TaskManager
from .todo_write import TodoManager
from .tool_use import BuiltinTools, PARENT_TOOLS, SUB_TOOLS, ToolExecutor


# 连续多轮没有更新 Todo 时，主动向模型补一条提醒。
TODO_REMINDER_ROUNDS = 3


class AgentHarness:
    """组合 s01–s12 能力，并驱动带任务图和错误恢复的父循环。"""

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
        self._sleep_fn = sleep_fn
        self._random_fn = random_fn
        # 累计式 Harness 只在这里完成装配；各课程模块不会反向导入
        # AgentHarness，因此依赖图保持无环。这里保存的是编排所需对象，
        # 具体能力实现仍留在各自课程模块中。

        # 1. 创建有状态能力：Skill 注册表、会话 Todo、持久 Task Board
        # 和基础工具 handler。Task 目录先经过工作区边界校验，模型工具不能
        # 借自定义 CC_TASKS_DIR 把任务记录写到项目之外。
        self.skills = SkillLoader(settings.skills_dir)
        self.todo = TodoManager()
        tasks_dir = settings.assert_inside_workdir(
            settings.tasks_dir, "tasks directory"
        )
        self.tasks = TaskManager(tasks_dir)
        self.builtins = BuiltinTools(settings, self.skills)

        # 2. Hook 必须先于执行器创建，因为所有普通工具都通过同一条 Hook 管线。
        self.hooks = HookManager()
        install_default_hooks(self.hooks, settings)
        self.executor = ToolExecutor(self.hooks)

        # 3. 手动 compact 的控制决策与真正的压缩算法分成两个对象。
        self.compact_tool = CompactToolController(self.hooks)
        self.compactor = ContextCompactor(
            client,
            settings.model,
            settings.transcript_dir,
            settings.tool_results_dir,
        )
        # 4. Memory 当前使用模块级配置，这里把它绑定到本 Harness 的路径和模型。
        memory.configure(settings)

        # 5. 父子 Agent 身份不同，因此各自维护 Prompt assembler 和缓存。
        self.parent_prompt = SystemPromptAssembler(
            PARENT_IDENTITY, PARENT_GUIDANCE
        )
        self.sub_prompt = SystemPromptAssembler(
            SUBAGENT_IDENTITY, SUBAGENT_GUIDANCE
        )
        self.system_prompt = ""
        self.sub_system_prompt = ""
        self.refresh_system_prompts()

        # 父 Agent 和 SubAgent 通过不同的 handler 与 schema 集合隔离，
        # 但有意共享 Hook 和 ToolExecutor。
        sub_handlers = self.builtins.handlers()
        self.subagent = SubagentRunner(
            client=client,
            settings=settings,
            executor=self.executor,
            handlers=sub_handlers,
            prompt_supplier=self._subagent_system_prompt,
        )
        # 父 handler 表仅服务于本类的工具批次分发，不作为 Harness 公共 API。
        self._parent_handlers: dict[str, Callable[..., str]] = {
            **self.builtins.handlers(),
            **self.tasks.handlers(),
            "todo_write": self.todo.update,
            "task": self.subagent.run,
        }

    def _prompt_context(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        # Prompt 只依赖可观察运行态；传入不同工具集即可生成父或子 Agent 上下文。
        # Skill 与 Memory 此处只放目录，不把完整正文永久塞进 System Prompt。
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
        # 每轮重扫 Skill，允许 Agent 刚创建的 SKILL.md 在下一次模型调用前生效。
        self.skills.registry = self.skills.scan()

        # 父子 Agent 使用同一组运行态来源，但工具注册表不同，所以分别组装。
        self.system_prompt = self.parent_prompt.get(
            self._prompt_context(PARENT_TOOLS)
        )
        self.sub_system_prompt = self.sub_prompt.get(
            self._prompt_context(SUB_TOOLS)
        )
        if messages is None:
            return

        # 传入消息列表时只更新首条 System Prompt，不改写历史 user/tool 内容。
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self.system_prompt
        else:
            messages.insert(0, {"role": "system", "content": self.system_prompt})

    def _subagent_system_prompt(self) -> str:
        # SubAgent 启动时再取 Prompt，避免构造时缓存了过期的 Skill/Memory 目录。
        self.refresh_system_prompts()
        return self.sub_system_prompt

    @staticmethod
    def _visible_parent_tools(
        already_compacted: bool,
    ) -> list[dict[str, Any]]:
        """返回当前模型调用可见的父 Agent 工具集合。"""

        # compact 每个 user turn 最多成功触发一次。压缩完成后从 schema
        # 注册表中移除它，比只在 Prompt 中要求模型不要重试更可靠。
        if not already_compacted:
            return PARENT_TOOLS
        return [
            tool
            for tool in PARENT_TOOLS
            if tool["function"]["name"] != "compact"
        ]

    # 普通工具的真正执行逻辑属于 ToolExecutor；这里仅选择父 Agent handler 表。
    def _execute_parent_tool(self, name: str, arguments: str) -> str:
        """使用父 handler 表执行一个普通工具调用。"""
        return self.executor.execute(name, arguments, self._parent_handlers)

    def _execute_tool_batch(
        self,
        tool_calls: list[Any],
        messages: list[dict[str, Any]],
        extraction_messages: list[dict[str, Any]],
        already_compacted: bool,
    ) -> tuple[bool, bool]:
        """执行完整工具批次，并返回 Todo/Compact 两个控制信号。"""

        used_todo = False
        compact_requested = False
        for tool_call in tool_calls:
            name = tool_call.function.name
            used_todo = used_todo or name == "todo_write"

            # compact 是控制工具：它决定批次结束后是否改写整个消息历史，
            # 因此不能进入只负责普通 handler 的 ToolExecutor。
            if name == "compact":
                result, allowed = self.compact_tool.request(
                    tool_call.function.arguments,
                    already_compacted=already_compacted or compact_requested,
                )
                compact_requested = compact_requested or allowed
                print(result)
            else:
                result = self._execute_parent_tool(
                    name,
                    tool_call.function.arguments,
                )

            # OpenAI 工具协议要求每个 tool_call_id 都有且只有一个 role=tool
            # 结果。先补齐整个批次，调用方才能安全地执行历史压缩。
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
            messages.append(tool_message)
            extraction_messages.append(copy.deepcopy(tool_message))
        return used_todo, compact_requested

    @staticmethod
    def _append_failure_result(
        messages: list[dict[str, Any]],
        extraction_messages: list[dict[str, Any]],
        error: Exception,
    ) -> str:
        """把不可恢复错误收口为最终 assistant 消息，而不是让 CLI 崩溃。"""

        answer = format_unrecoverable_error(error)
        item = {"role": "assistant", "content": answer}
        messages.append(item)
        extraction_messages.append(copy.deepcopy(item))
        print(f"\033[31m[recovery exhausted] {answer}\033[0m")
        return answer

    def agent_loop(
        self,
        messages: list[dict[str, Any]],
        active_request: str | None = None,
        compactor: ContextCompactor | None = None,
    ) -> str:
        """运行一个父 Agent turn，直到得到无需工具的最终文本。"""

        # 阶段 1：固定当前请求。后续即使压缩历史，也要把任务目标带进摘要。
        active_request = active_request or latest_user_request(messages)

        # 阶段 2：为 Memory 提取保存独立快照。主消息列表之后可能被压缩，
        # 但 turn 结束时仍应从较完整的原始交互中提取长期事实。
        extraction_messages = copy.deepcopy(messages[-12:])

        # 阶段 3：刷新 Prompt，再根据最近用户请求按需召回完整 Memory 正文。
        self.refresh_system_prompts(messages)
        recalled = memory.load_memories(self.client, messages)
        memory.inject_recalled_memories(messages, recalled)
        if recalled:
            print("\033[90m[Memory: recalled relevant records]\033[0m")
        # 测试可注入替代 compactor；正常运行使用 Harness 自带实例。
        compactor = compactor or self.compactor

        # 这些计数器只在当前 user turn 内有效，不跨 turn 累积。
        rounds_since_todo = 0
        manual_compactions = 0
        recovery = RecoveryState(current_model=self.settings.model)

        # 每次循环对应一次模型响应；有工具调用就执行并进入下一轮。
        while True:
            # 模型连续多轮不维护 Todo 时，以普通 user reminder 拉回计划状态。
            if rounds_since_todo >= TODO_REMINDER_ROUNDS and messages:
                messages.append(
                    {
                        "role": "user",
                        "content": "<reminder>Update your todos before continuing.</reminder>",
                    }
                )
                rounds_since_todo = 0

            # 顺序不能颠倒：先写入最新 System Prompt，再按上下文预算压缩消息。
            self.refresh_system_prompts(messages)
            messages[:] = compactor.prepare(messages, active_request)
            tools = self._visible_parent_tools(manual_compactions > 0)
            # Provider 统一补齐模型专属参数。瞬态 429/529 在同一份消息历史上
            # 重试；RecoveryState 同时记录 fallback 模型和当前输出预算。
            try:
                response = with_retry(
                    lambda model: self.client.chat.completions.create(
                        **completion_request(
                            self.settings,
                            messages,
                            tools,
                            model=model,
                            max_tokens=recovery.max_tokens,
                        )
                    ),
                    recovery,
                    self.settings.fallback_model,
                    sleep_fn=self._sleep_fn,
                    random_fn=self._random_fn,
                )
            except Exception as exc:
                # prompt too long 与普通瞬态错误不同：第一次强制压缩后原地
                # 重试；同一 turn 再次溢出说明自动恢复已无更多安全动作。
                if (
                    is_prompt_too_long_error(exc)
                    and not recovery.has_attempted_reactive_compact
                ):
                    messages[:] = compactor.reactive_compact(
                        messages, active_request
                    )
                    recovery.has_attempted_reactive_compact = True
                    print(
                        "\033[33m[prompt too long] "
                        "reactive compact completed; retrying\033[0m"
                    )
                    continue
                return self._append_failure_result(
                    messages, extraction_messages, exc
                )

            choice = response.choices[0]
            assistant = choice.message

            # 首次输出截断只提升预算并重放原请求，不能把不完整 assistant
            # 写进历史。64K 仍截断时才保存片段并追加 continuation user 消息。
            if is_output_truncated(getattr(choice, "finish_reason", None)):
                if not recovery.has_escalated:
                    previous = recovery.max_tokens
                    recovery.max_tokens = ESCALATED_MAX_TOKENS
                    recovery.has_escalated = True
                    print(
                        "\033[33m[max_tokens] escalating "
                        f"{previous} -> {ESCALATED_MAX_TOKENS}; retrying\033[0m"
                    )
                    continue

                partial = assistant.content or ""
                if partial:
                    partial_item = {"role": "assistant", "content": partial}
                    messages.append(partial_item)
                    extraction_messages.append(copy.deepcopy(partial_item))

                # 三次续写机会已经全部用完时，保留最后一段可见输出并停止。
                if recovery.continuation_count >= MAX_CONTINUATIONS:
                    answer = partial or (
                        "Error: output remained truncated after "
                        f"{MAX_CONTINUATIONS} continuation attempts"
                    )
                    if not partial:
                        item = {"role": "assistant", "content": answer}
                        messages.append(item)
                        extraction_messages.append(copy.deepcopy(item))
                    print(
                        "\033[31m[max_tokens] continuation limit reached; "
                        "returning final partial output\033[0m"
                    )
                    return answer

                continuation = {
                    "role": "user",
                    "content": CONTINUATION_PROMPT,
                }
                messages.append(continuation)
                extraction_messages.append(copy.deepcopy(continuation))
                recovery.continuation_count += 1
                print(
                    "\033[33m[max_tokens] requesting continuation "
                    f"{recovery.continuation_count}/{MAX_CONTINUATIONS}\033[0m"
                )
                continue

            # 完整 assistant 消息必须先写回历史，后续 role=tool 才能按 ID 配对。
            payload = assistant_payload(assistant)
            messages.append(payload)
            extraction_messages.append(copy.deepcopy(payload))

            # 没有工具调用表示模型准备结束当前 turn。
            if not assistant.tool_calls:
                answer = assistant.content or ""

                # Stop Hook 可以返回一条新的 user 消息要求继续；非 None 才真正续写。
                continuation = self.hooks.trigger("Stop", messages)
                if continuation is not None:
                    item = {"role": "user", "content": continuation}
                    messages.append(item)
                    extraction_messages.append(copy.deepcopy(item))
                    continue

                # 只有确认停止后才提取和整理 Memory，避免把中间答案当成最终事实。
                memory.extract_memories(self.client, extraction_messages)
                memory.consolidate_memories(self.client)
                return answer

            # 批次方法只执行协议与分发，并把影响循环状态的事实作为信号返回。
            # 历史压缩和 Todo 轮次仍由父状态机统一决定。
            used_todo, compact_requested = self._execute_tool_batch(
                assistant.tool_calls,
                messages,
                extraction_messages,
                already_compacted=manual_compactions > 0,
            )
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
            if compact_requested:
                # 等整批工具结果写完后再压缩，保证 assistant/tool 协议组不被拆断。
                messages[:] = compactor.compact_history(messages, active_request)
                manual_compactions += 1
