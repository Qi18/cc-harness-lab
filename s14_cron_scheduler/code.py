#!/usr/bin/env python3
"""s14 CLI 入口：运行累计 Harness，并启用本地 Cron 调度。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s14_cron_scheduler.harness.agent_loop import AgentHarness  # noqa: E402
from s14_cron_scheduler.harness.config import Settings  # noqa: E402
from s14_cron_scheduler.harness.provider import create_client  # noqa: E402
from s14_cron_scheduler.harness.system_prompt import SystemPromptAssembler  # noqa: E402
from s14_cron_scheduler.harness.tool_use import (  # noqa: E402
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

    def print_scheduled_answer(answer: str) -> None:
        print(f"\n\033[35m[scheduled answer]\033[0m\n{answer}\n")

    print("s14: Cron Scheduler (Alibaba Cloud Bailian)")
    print(
        f"model={settings.model} "
        f"fallback={settings.fallback_model or '(none)'} "
        f"workdir={settings.workdir} "
        f"sections={','.join(harness.parent_prompt.last_sections)}"
    )
    print("输入任务，回车发送；输入 q、exit 或空行退出。\n")

    harness.start_cron_runtime(messages, print_scheduled_answer)
    try:
        while True:
            try:
                query = input("\033[36ms14 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if query.strip().lower() in {"", "q", "exit"}:
                break
            harness.hooks.trigger("UserPromptSubmit", query)
            messages.append({"role": "user", "content": query})
            answer = harness.run_turn(messages, active_request=query)
            if answer:
                print(answer)
            print()
    finally:
        harness.stop_cron_runtime()


if __name__ == "__main__":
    main()
