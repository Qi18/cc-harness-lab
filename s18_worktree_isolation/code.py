#!/usr/bin/env python3
"""s18 CLI 入口：运行累计 Harness，并启用 Worktree Isolation。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s18_worktree_isolation.harness.agent_loop import AgentHarness  # noqa: E402
from s18_worktree_isolation.harness.config import Settings  # noqa: E402
from s18_worktree_isolation.harness.provider import create_client  # noqa: E402
from s18_worktree_isolation.harness.system_prompt import SystemPromptAssembler  # noqa: E402
from s18_worktree_isolation.harness.tool_use import (  # noqa: E402
    PARENT_TOOLS as TOOLS,
    SUB_TOOLS,
)


def main() -> None:
    settings = Settings.from_env()
    client = create_client(settings)
    harness = AgentHarness(client, settings)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": harness.system_prompt}
    ]

    # 定时 turn 没有对应的提示符，因此给它一个带标记的回调输出。
    def print_scheduled_answer(answer: str) -> None:
        print(f"\n\033[35m[scheduled answer]\033[0m\n{answer}\n")

    print("s18: Worktree Isolation (Alibaba Cloud Bailian)")
    print(
        f"model={settings.model} "
        f"fallback={settings.fallback_model or '(none)'} "
        f"workdir={settings.workdir} "
        f"sections={','.join(harness.parent_prompt.last_sections)}"
    )
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    # 轮询线程与 Queue Processor 都共用这一份 messages，因此一开始就传入它；
    # finally 里停掉两个线程，退出时不留下仍在轮询的后台线程。
    harness.start_cron_runtime(messages, print_scheduled_answer)
    try:
        while True:
            try:
                query = input("\033[36ms18 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if query.strip().lower() in {"", "q", "exit"}:
                break
            harness.hooks.trigger("UserPromptSubmit", query)
            messages.append({"role": "user", "content": query})
            # run_turn 而不是 agent_loop：前台输入要与定时投递串行。
            answer = harness.run_turn(messages, active_request=query)
            if answer:
                print(answer)
            print()
    finally:
        harness.stop_cron_runtime()


if __name__ == "__main__":
    main()
